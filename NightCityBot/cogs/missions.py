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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from openpyxl import load_workbook

from NightCityBot.utils.db import (
    mission_log_get,
    mission_log_record,
)
from NightCityBot.utils.permissions import is_fixer

logger = logging.getLogger(__name__)

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

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
