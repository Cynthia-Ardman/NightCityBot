"""Mission tracking cog.

Fixer-only commands for tracking which players have been on missions
and how often, so the GM team can keep mission invites fair.

Commands:
  !mission_check <users...>
      Report most-recent mission date, days since, and total count
      for each user.

  !mission_record <users...> [date=YYYY-MM-DD | MM/DD/YYYY]
      Record a mission for each user. Date defaults to today (UTC).

``parse_mission_sheet`` is retained as a module-level helper for any
future one-shot roster imports, but no Discord command exposes it now —
the original roster sheet was imported directly via a dev script.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from openpyxl import load_workbook

from NightCityBot.utils.db import (
    mission_log_get,
    mission_log_record,
    mission_event_list_due,
    mission_event_mark_paid,
)
from NightCityBot.utils.permissions import is_fixer

logger = logging.getLogger(__name__)

# Auto-payout fires at midnight US Eastern on the day AFTER the mission
# starts (per user spec). Using America/New_York handles EST/EDT automatically.
PAYOUT_TZ = ZoneInfo("America/New_York")


def compute_payout_ts(start_utc: datetime) -> datetime:
    """Return the UTC timestamp of the next 00:00 America/New_York strictly
    after ``start_utc``.

    Example: start = 2026-05-22 20:00 UTC (16:00 ET on May 22) →
    payout = 2026-05-23 00:00 ET = 2026-05-23 04:00 UTC.
    """
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    start_et = start_utc.astimezone(PAYOUT_TZ)
    next_day_et = (start_et + timedelta(days=1)).date()
    midnight_et = datetime(
        next_day_et.year, next_day_et.month, next_day_et.day,
        0, 0, 0, tzinfo=PAYOUT_TZ,
    )
    return midnight_et.astimezone(timezone.utc)

_MENTION_RE = re.compile(r"^<@!?(\d+)>$")
_ID_RE = re.compile(r"^\d{15,22}$")
_DATE_SLASH_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")
_DATE_DASH_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


def _parse_date_token(token: str) -> Optional[date]:
    """Parse a YYYY-MM-DD or M/D/YY(YY) date token."""
    if not token:
        return None
    m = _DATE_DASH_RE.match(token)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_SLASH_RE.match(token)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            return date(yr, mo, da)
        except ValueError:
            return None
    return None


def _parse_sheet_dates(cell: object) -> list[date]:
    """Parse the comma-separated date list from sheet column D."""
    if cell is None:
        return []
    if isinstance(cell, datetime):
        return [cell.date()]
    if isinstance(cell, date):
        return [cell]
    text = str(cell).strip()
    if not text:
        return []
    out: list[date] = []
    for raw in text.split(","):
        d = _parse_date_token(raw)
        if d is not None:
            out.append(d)
    return out


def parse_mission_sheet(xlsx_path: Path | str) -> list[dict]:
    """Parse the mission roster XLSX into normalized rows.

    Returns a list of dicts with keys ``user_id``, ``username``,
    ``mission_count``, ``mission_dates``. Skips rows whose column A
    doesn't look like a Discord snowflake.
    """
    wb = load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        out: list[dict] = []
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            a = row[0]
            if a is None:
                continue
            raw_id = str(a).strip()
            if not raw_id.isdigit() or not (15 <= len(raw_id) <= 22):
                # Header rows or junk rows.
                continue
            username = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
            try:
                count = int(row[2]) if len(row) > 2 and row[2] not in (None, "") else 0
            except (TypeError, ValueError):
                count = 0
            dates = _parse_sheet_dates(row[3]) if len(row) > 3 else []
            # If count is missing/zero but we parsed dates, infer count.
            if count <= 0 and dates:
                count = len(dates)
            out.append({
                "user_id": raw_id,
                "username": username,
                "mission_count": count,
                "mission_dates": dates,
            })
        return out
    finally:
        wb.close()


async def _resolve_target(ctx: commands.Context, token: str) -> tuple[str, str, Optional[discord.abc.User]]:
    """Resolve one CLI token into (user_id, username, user_obj_or_None).

    Accepts:
      - Discord mention (<@123> or <@!123>)
      - Raw snowflake id (digits only)
      - Plain username / display name (searched against the guild)
    Falls back to (token, token, None) when nothing matches so the caller
    can still record / look up by raw id.
    """
    token = token.strip()
    if not token:
        return ("", "", None)

    async def _fetch(uid_str: str):
        member = ctx.guild.get_member(int(uid_str)) if ctx.guild else None
        if member is None and ctx.bot is not None:
            try:
                member = await ctx.bot.fetch_user(int(uid_str))
            except Exception:
                member = None
        return member

    # Mention?
    m = _MENTION_RE.match(token)
    if m:
        uid = m.group(1)
        member = await _fetch(uid)
        uname = getattr(member, "display_name", None) or getattr(member, "name", "") or uid
        return (uid, uname, member)

    # Raw id?
    if _ID_RE.match(token):
        uid = token
        member = await _fetch(uid)
        uname = getattr(member, "display_name", None) or getattr(member, "name", "") or uid
        return (uid, uname, member)

    # Plain username — search the guild member cache.
    if ctx.guild is not None:
        token_l = token.lower()
        for mem in getattr(ctx.guild, "members", []) or []:
            if (
                (getattr(mem, "name", "") or "").lower() == token_l
                or (getattr(mem, "display_name", "") or "").lower() == token_l
                or (getattr(mem, "global_name", "") or "").lower() == token_l
            ):
                return (str(mem.id), mem.display_name, mem)

    # Unresolvable — return the raw token as both id and name so the
    # caller can still emit a helpful error.
    return (token, token, None)


def _format_check_line(token: str, row: Optional[dict], today: date) -> str:
    if not row:
        return f"• **{token}** — no mission record."
    name = row.get("username") or token
    count = int(row.get("mission_count") or 0)
    dates = list(row.get("mission_dates") or [])
    if not dates:
        return f"• **{name}** — {count} mission(s), but no dates on file."
    most_recent = max(dates)
    delta = (today - most_recent).days
    if delta < 0:
        when = f"future date {most_recent.isoformat()}"
    elif delta == 0:
        when = "today"
    elif delta == 1:
        when = "1 day ago"
    else:
        when = f"{delta} days ago"
    return (
        f"• **{name}** — {count} mission(s); last on **{most_recent.isoformat()}** ({when})."
    )


class MissionsCog(commands.Cog):
    """Mission tracking for the GM team."""

    PAYOUT_REASON = "NCRP Mission payout"

    def __init__(self, bot: commands.Bot, unbelievaboat=None):
        self.bot = bot
        self.unbelievaboat = unbelievaboat if unbelievaboat is not None else getattr(bot, "unbelievaboat", None)

    async def cog_load(self) -> None:
        self._payout_loop.start()

    async def cog_unload(self) -> None:
        self._payout_loop.cancel()

    @tasks.loop(minutes=5)
    async def _payout_loop(self) -> None:
        """Every 5 minutes, pay out any mission whose payout_ts has passed."""
        try:
            now_utc = datetime.now(timezone.utc)
            due = await mission_event_list_due(now_utc)
            for row in due:
                await self._process_mission_payout(row)
        except Exception:
            logger.error("mission _payout_loop iteration failed", exc_info=True)

    @_payout_loop.before_loop
    async def _before_payout_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_mission_payout(self, row: dict) -> None:
        mission_id = row.get("mission_id")
        mission_name = row.get("mission_name") or "Mission"
        pay = int(row.get("pay_per_player") or 0)
        attendees = list(row.get("attendee_ids") or [])
        guild_id = row.get("guild_id")
        channel_id = row.get("channel_id")
        start_ts = row.get("start_ts")
        mission_date = start_ts.date() if isinstance(start_ts, datetime) else datetime.now(timezone.utc).date()

        paid_lines: list[str] = []
        failed_lines: list[str] = []

        for uid in attendees:
            display = uid
            try:
                user = await self.bot.fetch_user(int(uid))
                display = getattr(user, "display_name", None) or getattr(user, "name", uid)
            except Exception:
                user = None

            ub_ok = True
            if pay > 0 and self.unbelievaboat is not None:
                try:
                    ub_ok = await self.unbelievaboat.update_balance(
                        int(uid),
                        {"cash": 0, "bank": pay},
                        reason=f"{self.PAYOUT_REASON}: {mission_name}",
                    )
                except Exception:
                    logger.error("UB payout failed for %s on mission %s", uid, mission_id, exc_info=True)
                    ub_ok = False

            log_result = await mission_log_record(str(uid), str(display)[:128], mission_date)

            if ub_ok and log_result is not None:
                paid_lines.append(f"• **{display}** — +¥{pay:,} bank (total {int(log_result.get('mission_count') or 0)} mission(s))")
            else:
                bits = []
                if not ub_ok:
                    bits.append("UB payout failed")
                if log_result is None:
                    bits.append("mission log update failed")
                failed_lines.append(f"• **{display}** — {', '.join(bits) or 'unknown failure'}")

        # Mark paid even on partial failures so we don't double-pay.
        # Failures are surfaced in the summary message and the log.
        await mission_event_mark_paid(str(mission_id))

        # Post a summary back to the channel where the mission was created.
        if guild_id and channel_id:
            try:
                guild = self.bot.get_guild(int(guild_id))
                ch = guild.get_channel(int(channel_id)) if guild else None
                if ch is not None:
                    embed = discord.Embed(
                        title=f"💰 Mission Payout — {mission_name}",
                        color=discord.Color.gold(),
                    )
                    if paid_lines:
                        embed.add_field(
                            name=f"Paid ({len(paid_lines)})",
                            value="\n".join(paid_lines)[:1024],
                            inline=False,
                        )
                    if failed_lines:
                        embed.add_field(
                            name=f"Failed ({len(failed_lines)})",
                            value="\n".join(failed_lines)[:1024],
                            inline=False,
                        )
                    if not paid_lines and not failed_lines:
                        embed.description = "No attendees on this mission — nothing to pay."
                    await ch.send(embed=embed)
            except Exception:
                logger.error("Failed to post mission payout summary for %s", mission_id, exc_info=True)

    @commands.command(name="mission_check", aliases=["mission_viability"])
    @is_fixer()
    async def mission_check(self, ctx: commands.Context, *targets: str):
        """Show mission history for each given user (mention, ID, or username)."""
        if not targets:
            await ctx.reply(
                "Usage: `!mission_check @user [@user2 …]` — accepts mentions, "
                "Discord IDs, or usernames.",
                mention_author=False,
            )
            return
        today = datetime.now(timezone.utc).date()
        lines: list[str] = []
        for tok in targets:
            uid, uname, _user_obj = await _resolve_target(ctx, tok)
            if not uid:
                continue
            row = await mission_log_get(uid)
            display_token = uname or tok
            lines.append(_format_check_line(display_token, row, today))
        if not lines:
            await ctx.reply("No valid targets given.", mention_author=False)
            return
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="mission_record")
    @is_fixer()
    async def mission_record(self, ctx: commands.Context, *args: str):
        """Record a mission for each user. Optional ``date=YYYY-MM-DD``."""
        targets: list[str] = []
        mission_date = datetime.now(timezone.utc).date()
        explicit_date = False
        for arg in args:
            if arg.lower().startswith("date="):
                parsed = _parse_date_token(arg.split("=", 1)[1])
                if parsed is None:
                    await ctx.reply(
                        f"Couldn't parse date `{arg}`. Use `date=YYYY-MM-DD` "
                        "or `date=M/D/YYYY`.",
                        mention_author=False,
                    )
                    return
                mission_date = parsed
                explicit_date = True
            else:
                targets.append(arg)
        if not targets:
            await ctx.reply(
                "Usage: `!mission_record @user [@user2 …] [date=YYYY-MM-DD]`",
                mention_author=False,
            )
            return
        recorded: list[str] = []
        failed: list[str] = []
        for tok in targets:
            uid, uname, _user_obj = await _resolve_target(ctx, tok)
            if not uid:
                continue
            result = await mission_log_record(uid, uname or "", mission_date)
            if result is None:
                failed.append(tok)
                continue
            recorded.append(
                f"• **{result.get('username') or uname or uid}** → "
                f"total {int(result.get('mission_count') or 0)} mission(s)"
            )
        date_label = mission_date.isoformat() + (" (today)" if not explicit_date else "")
        body = f"Recorded mission on **{date_label}**:\n" + "\n".join(recorded)
        if failed:
            body += "\nFailed: " + ", ".join(f"`{t}`" for t in failed)
        await ctx.reply(body, mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(MissionsCog(bot))
