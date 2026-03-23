import logging
import os
import json
from datetime import datetime, timedelta, time as dtime
import asyncio
from typing import Optional, List, Dict, Callable, Awaitable, Any
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from pathlib import Path
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils import config_loader as _cfg
from NightCityBot.utils import helpers
from NightCityBot.utils.db import (
    db_load, db_save,
    attendance_get_user, attendance_append,
    open_log_exists_today, open_log_count_month, open_log_add, open_log_get_all,
    last_payment_get, last_payment_set, last_payment_get_with_ts,
    payment_label_set, payment_label_get_ts,
    rent_run_get_last, rent_run_record,
    warn_db_failure,
)

safe_filename = helpers.safe_filename

# Expose helper functions for tests that patch them directly (backup/restore still file-backed)
load_json_file = helpers.load_json_file
save_json_file = helpers.save_json_file
append_json_file = helpers.append_json_file
import config
from NightCityBot.services.trauma_team import TraumaTeamService

logger = logging.getLogger(__name__)


class _AutoCtx:
    """Minimal ctx-like proxy used by the scheduler to drive run_rent_collection.

    Also counts charged/skipped members by matching the messages that
    ``run_rent_collection`` emits, so ``_run_auto_rent`` can post a summary.
    """

    def __init__(self, guild: discord.Guild, channel: discord.TextChannel, bot: commands.Bot):
        self.guild = guild
        self.channel = channel
        self.bot = bot
        self.author = bot.user
        self.charged_count: int = 0
        self.skipped_count: int = 0
        self.error_count: int = 0

    async def send(self, content=None, **kwargs):
        if isinstance(content, str):
            if content.startswith("✅ Completed for"):
                self.charged_count += 1
            elif content.startswith("⏭️ Skipping"):
                self.skipped_count += 1
            elif content.startswith("❌ Error processing"):
                self.error_count += 1
        try:
            await self.channel.send(content, **kwargs)
        except Exception:
            pass


class Economy(commands.Cog):
    """Cog managing player economy and automated rent."""

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the economy cog."""
        self.bot = bot
        self.unbelievaboat = bot.unbelievaboat
        self.trauma_service = TraumaTeamService(bot)
        self.open_log_lock = asyncio.Lock()
        self.attend_lock = asyncio.Lock()
        self.event_expires_at: Optional[datetime] = None
        self.event_started_at: Optional[datetime] = None
        self._startup_catchup_done: bool = False

    async def cog_load(self) -> None:
        """Start the monthly auto-rent scheduler when this cog is loaded."""
        self.auto_rent_loop.start()

    async def cog_unload(self) -> None:
        """Cancel the monthly auto-rent scheduler when this cog is unloaded."""
        self.auto_rent_loop.cancel()

    @tasks.loop(
        time=dtime(
            hour=getattr(config, "RENT_COLLECTION_HOUR", 0),
            minute=getattr(config, "RENT_COLLECTION_MINUTE", 0),
            tzinfo=ZoneInfo(getattr(config, "TIMEZONE", "UTC")),
        )
    )
    async def auto_rent_loop(self) -> None:
        """Fire rent collection automatically on the 1st of each calendar month."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("auto_collect_rent"):
            return
        now = helpers.get_tz_now()
        if now.day != 1:
            return
        if await self._auto_rent_ran_this_month():
            return
        await self._run_auto_rent(triggered_by="scheduler")

    @auto_rent_loop.before_loop
    async def _before_auto_rent_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._startup_catchup()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._startup_catchup()

    async def _auto_rent_ran_this_month(self) -> bool:
        """Return True if the auto_collect_rent sentinel was set this calendar month."""
        recorded_at = await payment_label_get_ts("0", "auto_collect_rent")
        if recorded_at is None:
            return False
        tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
        now = helpers.get_tz_now()
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=ZoneInfo("UTC"))
        recorded_local = recorded_at.astimezone(tz)
        return recorded_local.year == now.year and recorded_local.month == now.month

    async def _startup_catchup(self) -> None:
        """On startup: trigger if day 1-3 and not yet run; warn REPORT_USER_ID if missed past day 3."""
        if self._startup_catchup_done:
            return
        self._startup_catchup_done = True

        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("auto_collect_rent"):
            return

        now = helpers.get_tz_now()

        if await self._auto_rent_ran_this_month():
            return

        if now.day <= 3:
            logger.info(
                "Startup catch-up: triggering auto rent collection (day %s).", now.day
            )
            await self._run_auto_rent(triggered_by="startup-catchup")
        else:
            logger.warning(
                "Startup catch-up: auto rent collection appears missed for %s %s (today is day %s).",
                now.strftime("%B"),
                now.year,
                now.day,
            )
            notify_user = None
            user_id = getattr(config, "REPORT_USER_ID", 0)
            if user_id:
                notify_user = self.bot.get_user(user_id)
                if notify_user is None:
                    try:
                        notify_user = await self.bot.fetch_user(user_id)
                    except Exception:
                        notify_user = None
            if notify_user:
                try:
                    await notify_user.send(
                        f"⚠️ Auto rent collection appears to have been missed for "
                        f"{now.strftime('%B %Y')} (today is day {now.day}). "
                        f"Please run `!collect_rent` manually if needed."
                    )
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)

    async def _run_auto_rent(self, *, triggered_by: str = "auto", force: bool = False) -> None:
        """Run a full rent collection cycle using the rent log channel as output.

        After completion, records the ``auto_collect_rent`` sentinel in
        ``payment_labels`` and posts a brief charged/skipped summary to the
        rent-log channel and DMs ``REPORT_USER_ID``.
        """
        guild = self.bot.get_guild(getattr(config, "GUILD_ID", 0))
        if guild is None:
            logger.error("auto_rent: guild not found — skipping.")
            return
        channel_id = getattr(config, "RENT_LOG_CHANNEL_ID", 0)
        channel = guild.get_channel(channel_id)
        if channel is None:
            logger.error("auto_rent: RENT_LOG_CHANNEL_ID channel not found — skipping.")
            return
        logger.info("Auto rent collection starting (trigger=%s, force=%s).", triggered_by, force)
        ctx = _AutoCtx(guild, channel, self.bot)
        try:
            await channel.send(f"🤖 Auto rent collection triggered (`{triggered_by}`).")
            await self.run_rent_collection(ctx, force=force, verbose=False)
        except Exception:
            logger.exception("Auto rent collection failed (trigger=%s).", triggered_by)
            return

        await payment_label_set("0", "auto_collect_rent")

        summary = (
            f"🤖 Auto rent collection complete — "
            f"{ctx.charged_count} charged, "
            f"{ctx.skipped_count} skipped"
            + (f", {ctx.error_count} error(s)" if ctx.error_count else "")
            + f" (trigger: `{triggered_by}`)."
        )
        try:
            await channel.send(summary)
        except Exception:
            logger.warning("Suppressed exception", exc_info=True)

        notify_user = None
        user_id = getattr(config, "REPORT_USER_ID", 0)
        if user_id:
            notify_user = self.bot.get_user(user_id)
            if notify_user is None:
                try:
                    notify_user = await self.bot.fetch_user(user_id)
                except Exception:
                    notify_user = None
        if notify_user:
            try:
                await notify_user.send(summary)
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)

    @staticmethod
    def _split_deduction(cash: int, amount: int) -> tuple[int, int]:
        """Return cash/bank portions ensuring negative cash isn't double counted."""
        cash_deduct = min(max(cash, 0), amount)
        bank_deduct = max(0, amount - cash_deduct)
        return cash_deduct, bank_deduct

    @staticmethod
    def _get_cyber_weeks(entry: Any) -> int:
        """Return the medication streak weeks stored in ``entry``."""
        if isinstance(entry, dict):
            return int(entry.get("weeks", 0))
        if isinstance(entry, int):
            return entry
        if isinstance(entry, str) and entry.isdigit():
            return int(entry)
        return 0

    def event_active(self) -> bool:
        """Return ``True`` if a fixer event is currently active."""
        if self.event_expires_at is None:
            return False
        return helpers.get_tz_now() < self.event_expires_at

    def _sunday_event_start(self, now: datetime) -> datetime:
        """Return the start time of the current Sunday event in the configured timezone."""
        tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
        local_now = now.astimezone(tz)
        days_since_sun = (local_now.weekday() - 6) % 7
        sunday = (local_now - timedelta(days=days_since_sun)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        return sunday

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user or message.author.bot:
            return
        channel_id = message.channel.id
        parent_id = getattr(message.channel, "parent_id", None)
        if (
            channel_id == config.BUSINESS_ACTIVITY_CHANNEL_ID
            or parent_id == config.BUSINESS_ACTIVITY_CHANNEL_ID
        ):

            if not message.content.strip().startswith(
                ("!open_shop", "!openshop", "!os")
            ):
                try:
                    await message.delete()
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
                admin = self.bot.get_cog("Admin")
                if admin:
                    await admin.log_audit(
                        message.author,
                        f"🗑️ Deleted message in {message.channel.mention}: {message.content}",
                    )
        if (
            channel_id == config.ATTENDANCE_CHANNEL_ID
            or parent_id == config.ATTENDANCE_CHANNEL_ID
        ):

            if not message.content.strip().startswith("!attend"):
                try:
                    await message.delete()
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
                admin = self.bot.get_cog("Admin")
                if admin:
                    await admin.log_audit(
                        message.author,
                        f"🗑️ Deleted message in {message.channel.mention}: {message.content}",
                    )

    def calculate_passive_income(self, role: str, open_count: int) -> int:
        """Calculate passive income based on role and number of shop opens."""
        if role == "Business Tier 0":
            return _cfg.get_tier0_income_scale().get(open_count, 0)

        base_rent = _cfg.get_role_costs_business().get(role, 500)
        return int(base_rent * _cfg.get_open_percent().get(open_count, 0))

    async def apply_passive_income(
        self,
        member: discord.Member,
        applicable_roles: List[str],
        log: List[str],
    ) -> tuple[Optional[int], Optional[int]]:
        """Apply passive income based on business opens and roles."""
        total_income = 0

        now = helpers.get_tz_now()
        open_count = min(await open_log_count_month(str(member.id), now.year, now.month), 4)

        for role in applicable_roles:
            if "Housing Tier" in role:
                continue

            income = self.calculate_passive_income(role, open_count)
            log.append(f"💰 Passive income for {role}: ${income} ({open_count} opens)")
            total_income += income

        if total_income > 0:
            success = await self.unbelievaboat.update_balance(
                member.id, {"cash": total_income}, reason="Passive income"
            )
            if success:
                updated = await self.unbelievaboat.get_balance(member.id)
                log.append(f"➕ Added ${total_income} passive income.")
                if updated:
                    return updated["cash"], updated["bank"]

        current = await self.unbelievaboat.get_balance(member.id)
        if current:
            return current["cash"], current["bank"]
        return None, None

    @commands.command(aliases=["eventstart", "open_event", "start_event"])
    @is_fixer()
    async def event_start(self, ctx):
        """Temporarily enable !attend and !open_shop outside of Sunday."""
        if ctx.channel.id != config.ATTENDANCE_CHANNEL_ID:
            ch = ctx.guild.get_channel(config.ATTENDANCE_CHANNEL_ID)
            mention = ch.mention if ch else "#attendance"
            await ctx.send(f"❌ Please use {mention} for this command.")
            return
        now = helpers.get_tz_now()
        self.event_started_at = now
        self.event_expires_at = now + timedelta(hours=4)
        expires = self.event_expires_at.strftime("%I:%M %p %Z")
        await ctx.send(
            f"🟢 Event started! Temporary attendance and shop opens allowed until {expires}."
        )

    @commands.command(aliases=["openshop", "os"])
    @commands.has_permissions(send_messages=True)
    async def open_shop(self, ctx):
        """Log a business opening and grant income immediately."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("open_shop"):
            await ctx.send("⚠️ The open_shop system is currently disabled.")
            return
        if ctx.channel.id != config.BUSINESS_ACTIVITY_CHANNEL_ID:
            ch = ctx.guild.get_channel(config.BUSINESS_ACTIVITY_CHANNEL_ID)
            mention = ch.mention if ch else "#open_shop"
            await ctx.send(f"❌ Please use {mention} for this command.")
            return

        if not any(r.name.startswith("Business") for r in ctx.author.roles):
            await ctx.send("❌ You must have a business role to use this command.")
            return

        now = helpers.get_tz_now()
        if now.weekday() != 6 and not self.event_active():
            await ctx.send("❌ Business openings can only be logged on Sundays.")
            return

        user_id = str(ctx.author.id)
        now_str = now.isoformat()

        duplicate = False
        open_count_before = 0
        open_count_after = 0
        open_count_total = 0
        async with self.open_log_lock:
            if await open_log_exists_today(user_id):
                duplicate = True
            else:
                open_count_before = min(await open_log_count_month(user_id, now.year, now.month), 4)
                ok = await open_log_add(user_id, now)
                if not ok:
                    await warn_db_failure(
                        self.bot, "open_log_add",
                        f"user {user_id} — opening not recorded",
                    )
                open_count_total = open_count_before + 1
                open_count_after = min(open_count_total, 4)

        if duplicate:
            await ctx.send("❌ You've already logged a business opening today.")
            return

        reward = 0
        role_names = [r.name for r in ctx.author.roles]
        t0_scale = _cfg.get_tier0_income_scale()
        biz_costs = _cfg.get_role_costs_business()
        open_pct = _cfg.get_open_percent()
        for role in role_names:
            if "Business Tier" in role:
                if role == "Business Tier 0":
                    total_after = t0_scale.get(open_count_after, 0)
                    total_before = t0_scale.get(open_count_before, 0)
                else:
                    base = biz_costs.get(role, 500)
                    total_after = int(base * open_pct.get(open_count_after, 0))
                    total_before = int(base * open_pct.get(open_count_before, 0))
                reward += total_after - total_before

        if reward > 0:
            await self.unbelievaboat.update_balance(
                ctx.author.id, {"cash": reward}, reason="Business activity reward"
            )
            await ctx.send(
                f"✅ Business opening logged! You earned ${reward}. ({open_count_total} this month)"
            )
        else:
            await ctx.send(
                f"✅ Business opening logged! ({open_count_total} this month)"
            )

    @commands.command()
    async def attend(self, ctx):
        """Log attendance for players with the verified role and award cash."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("attend"):
            await ctx.send("⚠️ The attend system is currently disabled.")
            return
        if ctx.channel.id != config.ATTENDANCE_CHANNEL_ID:
            ch = ctx.guild.get_channel(config.ATTENDANCE_CHANNEL_ID)
            mention = ch.mention if ch else "#attend"
            await ctx.send(f"❌ Please use {mention} for this command.")
            return
        if not any(r.id == config.VERIFIED_ROLE_ID for r in ctx.author.roles):
            await ctx.send("❌ You must be verified to use this command.")
            return

        now = helpers.get_tz_now()
        if self.event_active():
            event_start = self.event_started_at or now
        else:
            if now.weekday() != 6:
                await ctx.send(
                    "❌ Attendance is only allowed during Sunday events (2pm to 7pm Pacific)."
                )
                return
            tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
            local_now = now.astimezone(tz)
            start = self._sunday_event_start(now)
            end = start + timedelta(hours=5)
            if not (start <= local_now <= end):
                await ctx.send(
                    "❌ Attendance is only allowed during Sunday events (2pm to 7pm Pacific)."
                )
                return
            event_start = start

        user_id = str(ctx.author.id)
        now_str = now.isoformat()

        async with self.attend_lock:
            all_logs = await attendance_get_user(user_id)
            parsed = [datetime.fromisoformat(ts) for ts in all_logs]
            if any(ts >= event_start for ts in parsed):
                await ctx.send("❌ You've already logged attendance for this event.")
                return

            ok = await attendance_append(user_id, now_str)
            if not ok:
                await warn_db_failure(
                    self.bot, "attendance_append",
                    f"user {user_id} — attendance not recorded",
                )

        reward = _cfg.get_attend_reward()
        await self.unbelievaboat.update_balance(
            ctx.author.id, {"cash": reward}, reason="Attendance reward"
        )
        await ctx.send(f"✅ Attendance logged! You received ${reward}.")

    def calculate_due(self, member: discord.Member) -> tuple[int, List[str]]:
        """Calculate upcoming rent, baseline, cyberware and subscription costs."""
        details: List[str] = []
        total = 0
        role_names = [r.name for r in member.roles]
        loa_role = member.guild.get_role(config.LOA_ROLE_ID)
        on_loa = loa_role in member.roles if loa_role else False

        if on_loa:
            details.append("LOA active: baseline, housing, and Trauma Team skipped")
        else:
            baseline = _cfg.get_baseline_living_cost()
            total += baseline
            details.append(f"Baseline living cost: ${baseline}")
            housing_costs = _cfg.get_role_costs_housing()
            for role in role_names:
                if "Housing Tier" in role:
                    amount = housing_costs.get(role, 0)
                    total += amount
                    details.append(f"{role}: ${amount}")

        biz_costs = _cfg.get_role_costs_business()
        for role in role_names:
            if "Business Tier" in role:
                amount = biz_costs.get(role, 0)
                total += amount
                details.append(f"{role}: ${amount}")

        if not on_loa:
            trauma_costs = _cfg.get_trauma_role_costs()
            trauma_role = next(
                (r for r in member.roles if r.name in trauma_costs), None
            )
            if trauma_role:
                cost = trauma_costs[trauma_role.name]
                total += cost
                details.append(f"{trauma_role.name}: ${cost}")

            cyber = self.bot.get_cog("CyberwareManager")
            if cyber:
                guild = member.guild
                checkup_role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
                medium = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
                high = guild.get_role(config.CYBER_HIGH_ROLE_ID)
                extreme = guild.get_role(config.CYBER_EXTREME_ROLE_ID)

                level = None
                if extreme and extreme in member.roles:
                    level = "extreme"
                elif high and high in member.roles:
                    level = "high"
                elif medium and medium in member.roles:
                    level = "medium"

                if level:
                    weeks = self._get_cyber_weeks(cyber.data.get(str(member.id)))
                    if checkup_role and checkup_role in member.roles:
                        upcoming = weeks + 1
                        cost = cyber.calculate_cost(level, upcoming)
                        details.append(
                            f"Cyberware meds week {upcoming}: ${cost} *(collected separately by staff — not included in total)*"
                        )
                    else:
                        details.append("Cyberware checkup due — no med cost")

        return total, details

    @commands.command(name="due")
    async def due(self, ctx, member: discord.Member | None = None) -> None:
        """Show estimated amount owed on the 1st of the month.

        Optionally provide ``member`` to check someone else's upcoming costs.
        """
        target = member or ctx.author
        logger.debug(
            "due command invoked for %s (%s) by %s (%s) in %s (%s)",
            target,
            target.id,
            ctx.author,
            ctx.author.id,
            getattr(ctx.channel, "name", ctx.channel.id),
            ctx.channel.id,
        )
        total, details = self.calculate_due(target)
        header = (
            f"💸 **Estimated Due for {target.display_name}:** ${total}"
            if member
            else f"💸 **Estimated Due:** ${total}"
        )
        lines = [header] + [f"• {d}" for d in details]

        _, paid_at = await last_payment_get_with_ts(str(target.id))
        if paid_at is not None:
            tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
            now_local = helpers.get_tz_now()
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=ZoneInfo("UTC"))
            paid_local = paid_at.astimezone(tz)
            if paid_local.year == now_local.year and paid_local.month == now_local.month:
                paid_str = paid_local.strftime("%b %d")
                lines.append(
                    f"✅ **Housing & baseline already paid this month** (recorded {paid_str})."
                    " Cyberware meds are collected separately by staff."
                )

        await ctx.send("\n".join(lines))

    @commands.command(name="last_payment")
    async def last_payment(self, ctx):
        """Show the details of your last automated payment."""
        summary = await last_payment_get(str(ctx.author.id))
        if not summary:
            await ctx.send("❌ No payment record found.")
        else:
            await ctx.send(summary)

    @commands.command(name="mark_paid")
    @commands.has_permissions(administrator=True)
    async def mark_paid(self, ctx, member: discord.Member, *, note: str = "") -> None:
        """Manually mark a user as having paid their rent obligations this month.

        Upserts a ``last_payment`` record for ``member`` with the current
        timestamp, enabling the double-charge protection and updating the
        ``!due`` "already paid" notice.  Accepts an optional free-text note.

        Usage: ``!mark_paid @User [note]``
        """
        admin_cog = self.bot.get_cog("Admin")
        summary = (
            f"[Admin mark_paid] Marked as paid this month by {ctx.author} ({ctx.author.id})"
        )
        if note:
            summary += f" — {note}"

        ok = await last_payment_set(str(member.id), summary)
        label_ok = await payment_label_set(str(member.id), "collect_rent_after")
        if ok and label_ok:
            await ctx.send(
                f"✅ <@{member.id}> marked as paid this month. "
                "`!collect_rent` will skip them for the rest of this calendar month."
            )
            if admin_cog:
                await admin_cog.log_audit(
                    ctx.author, f"!mark_paid: <@{member.id}> — {summary}"
                )
        else:
            await ctx.send(f"❌ Failed to update payment record for <@{member.id}>.")

    def _list_obligations(self, member: discord.Member) -> List[tuple[str, int]]:
        """Return a list of (name, cost) tuples for a member's upcoming fees."""
        obligations: List[tuple[str, int]] = []
        role_names = [r.name for r in member.roles]

        loa_role = member.guild.get_role(config.LOA_ROLE_ID)
        on_loa = loa_role in member.roles if loa_role else False

        if not on_loa:
            obligations.append(("Baseline living cost", _cfg.get_baseline_living_cost()))
            housing_costs = _cfg.get_role_costs_housing()
            for role in role_names:
                if "Housing Tier" in role:
                    amount = housing_costs.get(role, 0)
                    obligations.append((role, amount))

        biz_costs = _cfg.get_role_costs_business()
        for role in role_names:
            if "Business Tier" in role:
                amount = biz_costs.get(role, 0)
                obligations.append((role, amount))

        if not on_loa:
            trauma_costs = _cfg.get_trauma_role_costs()
            trauma_role = next(
                (r for r in member.roles if r.name in trauma_costs), None
            )
            if trauma_role:
                obligations.append(
                    (trauma_role.name, trauma_costs[trauma_role.name])
                )

            cyber = self.bot.get_cog("CyberwareManager")
            if cyber:
                guild = member.guild
                checkup_role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
                medium = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
                high = guild.get_role(config.CYBER_HIGH_ROLE_ID)
                extreme = guild.get_role(config.CYBER_EXTREME_ROLE_ID)

                level = None
                if extreme and extreme in member.roles:
                    level = "extreme"
                elif high and high in member.roles:
                    level = "high"
                elif medium and medium in member.roles:
                    level = "medium"

                if level:
                    weeks = self._get_cyber_weeks(cyber.data.get(str(member.id)))
                    if checkup_role and checkup_role in member.roles:
                        upcoming = weeks + 1
                        cost = cyber.calculate_cost(level, upcoming)
                        obligations.append((f"Cyberware meds week {upcoming}", cost))
        return obligations

    async def _evaluate_member_funds(
        self, member: discord.Member
    ) -> Optional[tuple[int, int, List[str], List[str]]]:
        """Return balance, deficit, payable items and unpaid items."""
        balance = await self.unbelievaboat.get_balance(member.id)
        if not balance:
            return None

        total_funds = balance.get("cash", 0) + balance.get("bank", 0)
        obligations = self._list_obligations(member)
        remaining = total_funds
        payable: List[str] = []
        unpaid: List[str] = []
        for name, cost in obligations:
            if remaining >= cost:
                remaining -= cost
                payable.append(f"{name} (${cost})")
            else:
                unpaid.append(f"{name} (${cost})")
        deficit = sum(c for _, c in obligations) - total_funds
        if deficit < 0:
            deficit = 0
        return total_funds, deficit, payable, unpaid

    async def backup_balances(
        self,
        members: List[discord.Member],
        *,
        label: str,
        balances: Optional[Dict[int, Dict[str, int]]] = None,
        progress_hook: Optional[Callable[[discord.Member, int, int], Awaitable[None]]] = None,
    ) -> None:
        """Append current balances for members to their backup files.

        ``balances`` can be supplied to avoid fetching the balance for each
        member again if it was already retrieved by the caller.

        When ``progress_hook`` is provided it will be awaited for each member
        with ``(member, index, total)`` to report progress.
        """
        backup_dir = Path(config.BALANCE_BACKUP_DIR)
        backup_dir.mkdir(exist_ok=True)
        total = len(members)
        for idx, m in enumerate(members, start=1):
            bal = balances.get(m.id) if balances else None
            if bal is None:
                bal = await self.unbelievaboat.get_balance(m.id)
            if not bal:
                continue
            file_path = backup_dir / f"balance_backup_{m.id}.json"

            prev_entries = await load_json_file(file_path, default=[])
            if not isinstance(prev_entries, list):
                prev_entries = []

            insert_index = len(prev_entries)
            if label.endswith("_after") and label.startswith("collect_"):
                before_label = label.replace("_after", "_before")
                for i in range(len(prev_entries) - 1, -1, -1):
                    if prev_entries[i].get("label") == before_label:
                        insert_index = i + 1
                        break

            if prev_entries and insert_index > 0:
                ref = prev_entries[insert_index - 1]
                prev_total = ref.get("cash", 0) + ref.get("bank", 0)
            else:
                prev_total = 0

            change = (bal.get("cash", 0) + bal.get("bank", 0)) - prev_total

            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "label": label,
                "cash": bal.get("cash", 0),
                "bank": bal.get("bank", 0),
                "change": change,
            }

            prev_entries.insert(insert_index, entry)
            await save_json_file(file_path, prev_entries)
            if progress_hook:
                await progress_hook(m, idx, total)

    async def record_last_payment(self, member: discord.Member, summary: str) -> None:
        """Store the last payment summary for a member."""
        ok = await last_payment_set(str(member.id), summary)
        if not ok:
            await warn_db_failure(
                self.bot, "last_payment_set",
                f"user {member.id} — payment summary not persisted",
            )

    async def _label_used_recently(
        self, member: discord.Member, label: str, days: int = 30
    ) -> bool:
        """Return ``True`` if the given label was used within ``days`` days.

        Uses the database ``payment_labels`` table (shared across all bot
        instances) so that dev and production bots both see the same protection
        state without relying on local balance-backup JSON files.
        """
        recorded_at = await payment_label_get_ts(str(member.id), label)
        if recorded_at is None:
            return False
        recorded_at_naive = recorded_at.replace(tzinfo=None) if recorded_at.tzinfo else recorded_at
        return datetime.utcnow() - recorded_at_naive < timedelta(days=days)

    async def _paid_this_month(self, member: discord.Member, label: str) -> bool:
        """Return ``True`` if the given label was recorded in the current calendar month.

        Uses the server's configured timezone (``config.TIMEZONE``) for the
        month/year comparison so the boundary matches the local calendar, not UTC.
        """
        recorded_at = await payment_label_get_ts(str(member.id), label)
        if recorded_at is None:
            return False
        tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
        now_local = helpers.get_tz_now()
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=ZoneInfo("UTC"))
        recorded_local = recorded_at.astimezone(tz)
        return (recorded_local.year == now_local.year
                and recorded_local.month == now_local.month)

    @commands.command(name="backup_balances")
    @commands.has_permissions(administrator=True)
    async def backup_balances_command(self, ctx):
        """Back up all member balances to a timestamped file."""
        members = ctx.guild.members
        backup_dir = Path(config.BALANCE_BACKUP_DIR)
        backup_dir.mkdir(exist_ok=True)
        filename = f"manual_{datetime.utcnow():%Y%m%d_%H%M%S}.json"
        file_path = backup_dir / filename

        data: Dict[int, Dict[str, int]] = {}
        total = len(members)
        for idx, m in enumerate(members, start=1):
            bal = await self.unbelievaboat.get_balance(m.id)
            if not bal:
                await ctx.send(
                    f"⚠️ Failed to fetch balance for {m.display_name} ({idx}/{total})"
                )
                continue
            data[m.id] = {
                "cash": bal.get("cash", 0),
                "bank": bal.get("bank", 0),
            }
            await ctx.send(f"Backed up {m.display_name} ({idx}/{total})")

        await save_json_file(file_path, {str(k): v for k, v in data.items()})
        await self.backup_balances(members, label=filename, balances=data)
        await ctx.send(f"✅ Balances backed up to `{file_path.name}`")

    @commands.command(name="backup_balance")
    @commands.has_permissions(administrator=True)
    async def backup_balance_command(self, ctx, member: discord.Member):
        """Back up a single member's balance to a timestamped file."""
        backup_dir = Path(config.BALANCE_BACKUP_DIR)
        backup_dir.mkdir(exist_ok=True)
        filename = f"manual_{datetime.utcnow():%Y%m%d_%H%M%S}.json"
        file_path = backup_dir / filename

        bal = await self.unbelievaboat.get_balance(member.id)
        if not bal:
            await ctx.send(f"⚠️ Failed to fetch balance for {member.display_name}")
            return

        data = {member.id: {"cash": bal.get("cash", 0), "bank": bal.get("bank", 0)}}

        await save_json_file(file_path, {str(member.id): data[member.id]})
        await self.backup_balances([member], label=filename, balances=data)
        await ctx.send(
            f"✅ Balance backed up to `{file_path.name}` for {member.display_name}"
        )

    @commands.command(name="restore_balances")
    @commands.has_permissions(administrator=True)
    async def restore_balances_command(self, ctx, identifier: str) -> None:
        """Restore member balances from a backup file or by label."""

        backup_dir = Path(config.BALANCE_BACKUP_DIR)

        # If the identifier looks like a filename, use the old behaviour
        if identifier.endswith(".json"):
            backup_path = backup_dir / identifier
            if not backup_path.exists():
                await ctx.send("❌ Backup file not found.")
                return

            data = await load_json_file(backup_path, default={})
            restored = 0
            for uid_str, bal in data.items():
                try:
                    uid = int(uid_str)
                except ValueError:
                    continue
                current = await self.unbelievaboat.get_balance(uid)
                if not current:
                    continue
                payload = {}
                delta_cash = bal.get("cash", 0) - current.get("cash", 0)
                delta_bank = bal.get("bank", 0) - current.get("bank", 0)
                if delta_cash:
                    payload["cash"] = delta_cash
                if delta_bank:
                    payload["bank"] = delta_bank
                if payload:
                    await self.unbelievaboat.update_balance(
                        uid, payload, reason="Balance restore"
                    )
                    restored += 1
            await ctx.send(
                f"✅ Restored balances for {restored} members from `{identifier}`"
            )
            return

        # Otherwise treat it as a label that should be searched in member logs
        label = identifier
        restored = 0
        for path in backup_dir.glob("balance_backup_*.json"):
            entries = await load_json_file(path, default=[])
            if not isinstance(entries, list):
                continue
            bal = None
            for entry in reversed(entries):
                if entry.get("label") == label:
                    bal = {"cash": entry.get("cash", 0), "bank": entry.get("bank", 0)}
                    break
            if not bal:
                continue

            try:
                uid = int(path.stem.split("_")[-1])
            except ValueError:
                continue

            current = await self.unbelievaboat.get_balance(uid)
            if not current:
                continue
            payload = {}
            delta_cash = bal.get("cash", 0) - current.get("cash", 0)
            delta_bank = bal.get("bank", 0) - current.get("bank", 0)
            if delta_cash:
                payload["cash"] = delta_cash
            if delta_bank:
                payload["bank"] = delta_bank
            if payload:
                await self.unbelievaboat.update_balance(
                    uid, payload, reason="Balance restore"
                )
                restored += 1

        await ctx.send(
            f"✅ Restored balances for {restored} members using label `{label}`"
        )

    @commands.command(name="restore_balance")
    @commands.has_permissions(administrator=True)
    async def restore_balance_command(
        self, ctx, member: discord.Member, identifier: Optional[str] = None
    ) -> None:
        """Restore a single member's balance from a backup file or label.

        If ``identifier`` is omitted (or points to the member's automatic backup
        file) the most recent entry from that file will be used. If a label is
        provided instead, the latest entry with that label will be restored.
        """

        label = None
        if identifier and not identifier.endswith(".json"):
            label = identifier
            filename = f"balance_backup_{member.id}.json"
        else:
            filename = identifier or f"balance_backup_{member.id}.json"

        backup_path = Path(config.BALANCE_BACKUP_DIR) / filename
        if not backup_path.exists():
            await ctx.send("❌ Backup file not found.")
            return

        data = await load_json_file(backup_path, default={})
        bal = None
        if label:
            if isinstance(data, list):
                for entry in reversed(data):
                    if entry.get("label") == label:
                        bal = {
                            "cash": entry.get("cash", 0),
                            "bank": entry.get("bank", 0),
                        }
                        break
                if not bal:
                    await ctx.send("❌ Label not found in backup file.")
                    return
            else:
                await ctx.send("❌ Invalid backup file format for label restore.")
                return
        elif isinstance(data, list):
            if data:
                last = data[-1]
                bal = {"cash": last.get("cash", 0), "bank": last.get("bank", 0)}
            else:
                await ctx.send("❌ No entries found in backup file.")
                return
        else:
            bal = data.get(str(member.id))
            if not bal:
                await ctx.send("❌ User not found in backup file.")
                return
        current = await self.unbelievaboat.get_balance(member.id)
        if not current:
            await ctx.send("❌ Failed to fetch current balance.")
            return
        payload = {}
        delta_cash = bal.get("cash", 0) - current.get("cash", 0)
        delta_bank = bal.get("bank", 0) - current.get("bank", 0)
        if delta_cash:
            payload["cash"] = delta_cash
        if delta_bank:
            payload["bank"] = delta_bank
        if payload:
            await self.unbelievaboat.update_balance(
                member.id, payload, reason="Balance restore"
            )
            source = label if label else filename
            await ctx.send(
                f"✅ Restored balance for {member.display_name} from `{source}`"
            )
        else:
            await ctx.send("⚠️ Balance already matches backup.")

    async def deduct_flat_fee(
        self,
        member: discord.Member,
        cash: int,
        bank: int,
        log: List[str],
        amount: int | None = None,
        *,
        dry_run: bool = False,
    ) -> tuple[bool, int, int]:
        if amount is None:
            amount = _cfg.get_baseline_living_cost()
        total = (cash or 0) + (bank or 0)
        if total < amount:
            log.append(
                f"❌ Insufficient funds for flat fee deduction (${amount}). Current balance: ${total}."
            )
            return False, cash, bank

        deduct_cash, deduct_bank = self._split_deduction(cash, amount)
        payload: Dict[str, int] = {}
        if deduct_cash > 0:
            payload["cash"] = -deduct_cash
        if deduct_bank > 0:
            payload["bank"] = -deduct_bank

        success = True
        if not dry_run:
            success = await self.unbelievaboat.update_balance(
                member.id, payload, reason="Flat Monthly Fee"
            )
        if success:
            cash -= deduct_cash
            bank -= deduct_bank
            log.append(
                f"{'💸 Would deduct' if dry_run else '💸 Deducted'} flat monthly fee of ${amount} (Cash: ${deduct_cash}, Bank: ${deduct_bank})."
            )
        else:
            log.append("❌ Failed to deduct flat monthly fee.")

        return success, cash, bank

    async def process_housing_rent(
        self,
        member: discord.Member,
        roles: List[str],
        cash: int,
        bank: int,
        log: List[str],
        rent_log_channel: Optional[discord.TextChannel],
        eviction_channel: Optional[discord.TextChannel],
        *,
        dry_run: bool = False,
    ) -> tuple[int, int]:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("housing_rent"):
            log.append("⚠️ Housing rent system disabled.")
            return cash, bank
        housing_total = 0
        housing_costs = _cfg.get_role_costs_housing()
        for role in roles:
            if "Housing Tier" in role:
                amount = housing_costs.get(role, 0)
                housing_total += amount
                log.append(f"🔎 Housing Role {role} → Rent: ${amount}")

        if housing_total == 0:
            return cash, bank

        total = (cash or 0) + (bank or 0)
        if total < housing_total:
            log.append(
                f"❌ Cannot pay housing rent of ${housing_total}. Would result in negative balance."
            )
            if eviction_channel and not dry_run:
                await eviction_channel.send(
                    f"🚨 <@{member.id}> — Housing Rent due: ${housing_total} — **FAILED** (insufficient funds) 🚨\n## You have **7 days** to pay or face eviction."
                )
            log.append(
                f"⚠️ Housing rent skipped for <@{member.id}> due to insufficient funds."
            )
            return cash, bank

        deduct_cash, deduct_bank = self._split_deduction(cash, housing_total)
        payload: Dict[str, int] = {}
        if deduct_cash > 0:
            payload["cash"] = -deduct_cash
        if deduct_bank > 0:
            payload["bank"] = -deduct_bank

        success = True
        if not dry_run:
            success = await self.unbelievaboat.update_balance(
                member.id, payload, reason="Housing Rent"
            )
        if success:
            cash -= deduct_cash
            bank -= deduct_bank
            log.append(
                f"🧮 {'Would subtract' if dry_run else 'Subtracted'} housing rent ${housing_total} — ${deduct_cash} from cash, ${deduct_bank} from bank."
            )
            log.append(
                f"📈 Balance after housing rent — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
            )
            log.append("✅ Housing Rent collection completed. Notice Sent to #rent")
            if rent_log_channel and not dry_run:
                await rent_log_channel.send(
                    f"✅ <@{member.id}> — Housing Rent paid: ${housing_total}"
                )
        else:
            log.append(
                "❌ Failed to deduct housing rent despite having sufficient funds."
            )
        return cash, bank

    async def process_business_rent(
        self,
        member: discord.Member,
        roles: List[str],
        cash: int,
        bank: int,
        log: List[str],
        rent_log_channel: Optional[discord.TextChannel],
        eviction_channel: Optional[discord.TextChannel],
        *,
        dry_run: bool = False,
    ) -> tuple[int, int]:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("business_rent"):
            log.append("⚠️ Business rent system disabled.")
            return cash, bank
        business_total = 0
        biz_costs = _cfg.get_role_costs_business()
        for role in roles:
            if "Business Tier" in role:
                amount = biz_costs.get(role, 0)
                business_total += amount
                log.append(f"🔎 Business Role {role} → Rent: ${amount}")

        if business_total == 0:
            return cash, bank

        total = (cash or 0) + (bank or 0)
        if total < business_total:
            log.append(
                f"❌ Cannot pay business rent of ${business_total}. Would result in negative balance."
            )
            if eviction_channel and not dry_run:
                await eviction_channel.send(
                    f"🚨 <@{member.id}> — Business Rent due: ${business_total} — **FAILED** (insufficient funds) 🚨\n## You have **7 days** to pay or face eviction."
                )
            log.append(
                f"⚠️ Business rent skipped for <@{member.id}> due to insufficient funds."
            )
            return cash, bank

        deduct_cash, deduct_bank = self._split_deduction(cash, business_total)
        payload: Dict[str, int] = {}
        if deduct_cash > 0:
            payload["cash"] = -deduct_cash
        if deduct_bank > 0:
            payload["bank"] = -deduct_bank

        success = True
        if not dry_run:
            success = await self.unbelievaboat.update_balance(
                member.id, payload, reason="Business Rent"
            )
        if success:
            cash -= deduct_cash
            bank -= deduct_bank
            log.append(
                f"🧮 {'Would subtract' if dry_run else 'Subtracted'} business rent ${business_total} — ${deduct_cash} from cash, ${deduct_bank} from bank."
            )
            log.append(
                f"📈 Balance after business rent — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
            )
            log.append("✅ Business Rent collection completed. Notice Sent to #rent")
            if rent_log_channel and not dry_run:
                await rent_log_channel.send(
                    f"✅ <@{member.id}> — Business Rent paid: ${business_total}"
                )
        else:
            log.append(
                "❌ Failed to deduct business rent despite having sufficient funds."
            )
        return cash, bank

    @commands.command(aliases=["collecthousing"])
    @commands.has_permissions(administrator=True)
    async def collect_housing(self, ctx, *args):
        """Manually collect housing rent from a single user.

        Use ``-force`` to ignore the this-month guard.
        """
        converter = commands.MemberConverter()
        user = None
        verbose = False
        force = False
        for arg in args:
            lower = arg.lower()
            if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                verbose = True
            elif lower in {"-force", "--force", "force", "-f"}:
                force = True
            elif user is None:
                try:
                    user = await converter.convert(ctx, arg)
                except commands.BadArgument:
                    continue
        if user is None:
            await ctx.send("❌ Could not resolve user.")
            return

        if not any(r.id == config.APPROVED_ROLE_ID for r in user.roles):
            await ctx.send(f"⏭️ <@{user.id}> has no approved character.")
            return

        if not any(r.id == config.APPROVED_ROLE_ID for r in user.roles):
            await ctx.send(f"⏭️ <@{user.id}> has no approved character.")
            return

        if not force and await self._paid_this_month(user, "collect_housing_after"):
            await ctx.send(
                "⏭️ Housing rent already collected this month. Use -force to override."
            )
            return

        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("housing_rent"):
            await ctx.send("⚠️ The housing_rent system is currently disabled.")
            return
        admin_cog = self.bot.get_cog("Admin")
        log: List[str] = [f"🏠 Manual Housing Rent Collection for <@{user.id}>"]
        rent_log_channel = ctx.guild.get_channel(config.RENT_LOG_CHANNEL_ID)
        eviction_channel = ctx.guild.get_channel(config.EVICTION_CHANNEL_ID)
        await ctx.send(f"Working on <@{user.id}>")

        role_names = [r.name for r in user.roles]
        log.append(f"🧾 Roles: {role_names}")

        balance_data = await self.unbelievaboat.get_balance(user.id)
        if not balance_data:
            log.append("❌ Could not fetch balance.")
            await ctx.send(f"⚠️ Could not fetch balance for <@{user.id}>")
            if admin_cog:
                await admin_cog.log_audit(ctx.author, "\n".join(log))
            return

        await self.backup_balances([user], label="collect_trauma_before")

        await self.backup_balances([user], label="collect_business_before")

        await self.backup_balances([user], label="collect_housing_before")

        cash = balance_data["cash"]
        bank = balance_data["bank"]
        start_cash = cash
        start_bank = bank
        total = (cash or 0) + (bank or 0)
        log.append(f"💵 Balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: ${total:,}")

        cash, bank = await self.process_housing_rent(
            user, role_names, cash, bank, log, rent_log_channel, eviction_channel
        )

        final = await self.unbelievaboat.get_balance(user.id)
        if final:
            final_cash = final.get("cash", 0)
            final_bank = final.get("bank", 0)
            final_total = final_cash + final_bank
        else:
            final_cash = cash
            final_bank = bank
            final_total = final_cash + final_bank
        log.append(
            f"📊 Final balance — Cash: ${final_cash:,}, Bank: ${final_bank:,}, Total: ${final_total:,}"
        )

        await self.backup_balances([user], label="collect_trauma_after")

        await self.backup_balances([user], label="collect_business_after")

        await self.backup_balances([user], label="collect_housing_after")

        for _lbl in ("collect_trauma_after", "collect_business_after", "collect_housing_after"):
            await payment_label_set(str(user.id), _lbl)

        summary = "\n".join(log)
        if verbose:
            await ctx.send(summary)
        else:
            await ctx.send(
                f"✅ Completed for <@{user.id}>\n"
                f"Before: Cash ${start_cash:,}, Bank ${start_bank:,}\n"
                f"After: Cash ${final_cash:,}, Bank ${final_bank:,}"
            )
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    @commands.command(aliases=["collectbusiness"])
    @commands.has_permissions(administrator=True)
    async def collect_business(self, ctx, *args):
        """Manually collect business rent from a single user.

        Use ``-force`` to ignore the this-month guard.
        """
        converter = commands.MemberConverter()
        user = None
        verbose = False
        force = False
        for arg in args:
            lower = arg.lower()
            if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                verbose = True
            elif lower in {"-force", "--force", "force", "-f"}:
                force = True
            elif user is None:
                try:
                    user = await converter.convert(ctx, arg)
                except commands.BadArgument:
                    continue
        if user is None:
            await ctx.send("❌ Could not resolve user.")
            return

        if not force and await self._paid_this_month(user, "collect_business_after"):
            await ctx.send(
                "⏭️ Business rent already collected this month. Use -force to override."
            )
            return
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("business_rent"):
            await ctx.send("⚠️ The business_rent system is currently disabled.")
            return
        admin_cog = self.bot.get_cog("Admin")
        log: List[str] = [f"🏢 Manual Business Rent Collection for <@{user.id}>"]
        rent_log_channel = ctx.guild.get_channel(config.RENT_LOG_CHANNEL_ID)
        eviction_channel = ctx.guild.get_channel(config.EVICTION_CHANNEL_ID)
        await ctx.send(f"Working on <@{user.id}>")

        role_names = [r.name for r in user.roles]
        log.append(f"🧾 Roles: {role_names}")

        balance_data = await self.unbelievaboat.get_balance(user.id)
        if not balance_data:
            log.append("❌ Could not fetch balance.")
            await ctx.send(f"⚠️ Could not fetch balance for <@{user.id}>")
            if admin_cog:
                await admin_cog.log_audit(ctx.author, "\n".join(log))
            return

        cash = balance_data["cash"]
        bank = balance_data["bank"]
        start_cash = cash
        start_bank = bank
        total = (cash or 0) + (bank or 0)
        log.append(f"💵 Balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: ${total:,}")

        cash, bank = await self.process_business_rent(
            user, role_names, cash, bank, log, rent_log_channel, eviction_channel
        )

        final = await self.unbelievaboat.get_balance(user.id)
        if final:
            final_cash = final.get("cash", 0)
            final_bank = final.get("bank", 0)
            final_total = final_cash + final_bank
        else:
            final_cash = cash
            final_bank = bank
            final_total = final_cash + final_bank
        log.append(
            f"📊 Final balance — Cash: ${final_cash:,}, Bank: ${final_bank:,}, Total: ${final_total:,}"
        )

        await payment_label_set(str(user.id), "collect_business_after")

        summary = "\n".join(log)
        if verbose:
            await ctx.send(summary)
        else:
            await ctx.send(
                f"✅ Completed for <@{user.id}>\n"
                f"Before: Cash ${start_cash:,}, Bank ${start_bank:,}\n"
                f"After: Cash ${final_cash:,}, Bank ${final_bank:,}"
            )
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    @commands.command(aliases=["collecttrauma"])
    @commands.has_permissions(administrator=True)
    async def collect_trauma(self, ctx, *args):
        """Manually collect Trauma Team subscription.

        Use ``-force`` to ignore the this-month guard.
        """
        converter = commands.MemberConverter()
        user = None
        verbose = False
        force = False
        for arg in args:
            lower = arg.lower()
            if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                verbose = True
            elif lower in {"-force", "--force", "force", "-f"}:
                force = True
            elif user is None:
                try:
                    user = await converter.convert(ctx, arg)
                except commands.BadArgument:
                    continue
        if user is None:
            await ctx.send("❌ Could not resolve user.")
            return

        if not force and await self._paid_this_month(user, "collect_trauma_after"):
            await ctx.send(
                "⏭️ Trauma subscription already processed this month. Use -force to override."
            )
            return
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("trauma_team"):
            await ctx.send("⚠️ The trauma_team system is currently disabled.")
            return
        admin_cog = self.bot.get_cog("Admin")
        log: List[str] = [
            f"💊 Manual Trauma Team Subscription Processing for <@{user.id}>"
        ]
        balance_data = await self.unbelievaboat.get_balance(user.id)
        if not balance_data:
            log.append("❌ Could not fetch balance.")
            await ctx.send(f"⚠️ Could not fetch balance for <@{user.id}>")
            if admin_cog:
                await admin_cog.log_audit(ctx.author, "\n".join(log))
            return

        cash = balance_data["cash"]
        bank = balance_data["bank"]
        total = (cash or 0) + (bank or 0)
        log.append(f"💵 Balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: ${total:,}")

        await ctx.send(f"Working on <@{user.id}>")
        await self.trauma_service.process_trauma_team_payment(user, log=log)

        final = await self.unbelievaboat.get_balance(user.id)
        if final:
            final_cash = final.get("cash", 0)
            final_bank = final.get("bank", 0)
            final_total = final_cash + final_bank
        else:
            final_cash = cash
            final_bank = bank
            final_total = final_cash + final_bank
        log.append(
            f"📊 Final balance — Cash: ${final_cash:,}, Bank: ${final_bank:,}, Total: ${final_total:,}"
        )

        await payment_label_set(str(user.id), "collect_trauma_after")

        summary = "\n".join(log)
        if verbose:
            await ctx.send(summary)
        else:
            await ctx.send(
                f"✅ Completed for <@{user.id}>\n"
                f"Before: Cash ${cash:,}, Bank ${bank:,}\n"
                f"After: Cash ${final_cash:,}, Bank ${final_bank:,}"
            )
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    async def run_rent_collection(
        self,
        ctx,
        *,
        target_user: Optional[discord.Member] = None,
        dry_run: bool = False,
        verbose: bool = False,
        force: bool = False,
        preview_dm: bool = False,
    ):
        """Internal helper for rent collection and simulation.

        When ``verbose`` is ``False`` only minimal status messages are sent.
        """
        await ctx.send(
            "🧪 Starting rent simulation..."
            if dry_run
            else "🚦 Starting rent collection..."
        )

        notify_user = None
        if not dry_run:
            user_id = getattr(config, "REPORT_USER_ID", 0)
            if user_id and hasattr(self.bot, "get_user"):
                notify_user = self.bot.get_user(user_id)
                if notify_user is None and hasattr(self.bot, "fetch_user"):
                    try:
                        notify_user = await self.bot.fetch_user(user_id)
                    except Exception:
                        notify_user = None
            if notify_user:
                try:
                    await notify_user.send("🚦 Rent collection starting...")
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
        audit_lines: List[str] = []

        if not force and not target_user:
            last_run = await rent_run_get_last()
            if last_run:
                _tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
                _now = helpers.get_tz_now()
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                _last_local = last_run.astimezone(_tz)
                if _last_local.year == _now.year and _last_local.month == _now.month:
                    await ctx.send(
                        "⚠️ Rent already collected this month. Use -force to override."
                    )
                    return
        if not target_user and not dry_run:
            ok = await rent_run_record(str(ctx.author))
            if not ok:
                await warn_db_failure(
                    self.bot, "rent_run_record",
                    f"initiated by {ctx.author} — rent run timestamp not persisted",
                )

        members_to_process: List[discord.Member] = []
        for m in ctx.guild.members:
            if target_user and m.id == target_user.id:
                if any(r.id == config.APPROVED_ROLE_ID for r in m.roles):
                    members_to_process = [m]
                break
            if not target_user:
                has_verified = any(r.id == config.VERIFIED_ROLE_ID for r in m.roles)
                has_tier = any("Tier" in r.name for r in m.roles)
                has_approved = any(r.id == config.APPROVED_ROLE_ID for r in m.roles)
                if (has_verified or has_tier) and has_approved:
                    members_to_process.append(m)
        if not members_to_process:
            if target_user:
                await ctx.send(
                    f"⏭️ <@{target_user.id}> has no approved character."
                )
            else:
                await ctx.send("❌ No matching members found.")
            return

        await ctx.send(f"ℹ️ {len(members_to_process)} member(s) to process.")

        if not dry_run:
            await ctx.send("💾 Backing up member balances…")

            async def progress(member: discord.Member, idx: int, total: int) -> None:
                if verbose:
                    await ctx.send(f"💾 Backed up {member.display_name} ({idx}/{total})")

            await self.backup_balances(
                members_to_process,
                label="collect_rent_before",
                progress_hook=progress if verbose else None,
            )

            await ctx.send("💾 Balance backup complete.")

        eviction_channel = ctx.guild.get_channel(config.EVICTION_CHANNEL_ID)
        rent_log_channel = ctx.guild.get_channel(config.RENT_LOG_CHANNEL_ID)
        admin_cog = self.bot.get_cog("Admin")

        async def _flush(start: int) -> None:
            if verbose:
                for line in log[start:]:
                    await ctx.send(line)

        for idx, member in enumerate(members_to_process, start=1):
            try:
                if not force:
                    paid = await self._paid_this_month(member, "collect_rent_after")
                    paid = paid or await self._paid_this_month(
                        member, "collect_housing_after"
                    )
                    paid = paid or await self._paid_this_month(
                        member, "collect_business_after"
                    )
                    paid = paid or await self._paid_this_month(
                        member, "collect_trauma_after"
                    )
                    if paid:
                        await ctx.send(
                            f"⏭️ Skipping <@{member.id}> — already paid this month."
                        )
                        continue

                if not any(r.id == config.APPROVED_ROLE_ID for r in member.roles):
                    await ctx.send(
                        f"⏭️ Skipping <@{member.id}> — no approved character."
                    )
                    continue

                progress = f"{idx}/{len(members_to_process)}"
                log: List[str] = [f"🔍 **Working on:** <@{member.id}> ({progress})"]
                await ctx.send(log[0])

                role_names = [r.name for r in member.roles]
                app_roles = [r for r in role_names if "Tier" in r]
                log.append(f"🏷️ Detected roles: {', '.join(app_roles) or 'None'}")
                await _flush(len(log) - 1)

                loa_role = member.guild.get_role(config.LOA_ROLE_ID)
                on_loa = loa_role in member.roles if loa_role else False
                if on_loa:
                    log.append("🏖️ Member is on LOA — skipping personal fees.")
                    await _flush(len(log) - 1)

                bal = await self.unbelievaboat.get_balance(member.id)
                if not bal:
                    log.append("⚠️ Could not fetch balance.")
                    summary = "\n".join(log)
                    await _flush(0)
                    if not verbose:
                        await ctx.send(f"⚠️ Could not fetch balance for <@{member.id}>")
                    if dry_run and admin_cog:
                        await admin_cog.log_audit(ctx.author, summary)
                    continue
                cash, bank = bal["cash"], bal["bank"]
                log.append(
                    f"💵 Starting balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
                )
                await _flush(len(log) - 1)

                if dry_run:
                    check = await self.unbelievaboat.verify_balance_ops(member.id)
                    log.append(
                        "🔄 Balance check passed."
                        if check
                        else "⚠️ Balance update check failed."
                    )
                    await _flush(len(log) - 1)

                if not on_loa:
                    start = len(log)
                    _baseline = _cfg.get_baseline_living_cost()
                    base_ok, cash, bank = await self.deduct_flat_fee(
                        member, cash, bank, log, _baseline, dry_run=dry_run
                    )
                    if not base_ok:
                        if eviction_channel and not dry_run:
                            await eviction_channel.send(
                                f"⚠️ <@{member.id}> could not pay baseline living cost (${_baseline})."
                            )
                        log.append(
                            "⚠️ Baseline living cost unpaid. Continuing with rent steps."
                        )
                    elif rent_log_channel and not dry_run:
                        await rent_log_channel.send(
                            f"✅ <@{member.id}> — Baseline living cost paid: ${_baseline}"
                        )
                    await _flush(start)

                start = len(log)
                cash, bank = (
                    await self.process_housing_rent(
                        member,
                        app_roles,
                        cash,
                        bank,
                        log,
                        rent_log_channel,
                        eviction_channel,
                        dry_run=dry_run,
                    )
                    if not on_loa
                    else (cash, bank)
                )
                await _flush(start)
                start = len(log)
                cash, bank = await self.process_business_rent(
                    member,
                    app_roles,
                    cash,
                    bank,
                    log,
                    rent_log_channel,
                    eviction_channel,
                    dry_run=dry_run,
                )
                await _flush(start)

                if not on_loa:
                    start = len(log)
                    await self.trauma_service.process_trauma_team_payment(
                        member, log=log, dry_run=dry_run
                    )
                    await _flush(start)

                if not dry_run:
                    final = await self.unbelievaboat.get_balance(member.id)
                    if final:
                        cash = final.get("cash", 0)
                        bank = final.get("bank", 0)

                log.append(
                    f"📊 {'Projected' if dry_run else 'Final'} balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
                )
                await _flush(len(log) - 1)

                if dry_run and preview_dm:
                    log.append("💌 Would DM summary to user.")
                    await _flush(len(log) - 1)
                    log.append("📝 Would record last_payment entry.")
                    await _flush(len(log) - 1)

                summary = "\n".join(log)
                if verbose:
                    pass
                else:
                    await ctx.send(f"✅ Completed for <@{member.id}>")
                if dry_run and admin_cog:
                    await admin_cog.log_audit(ctx.author, summary)
                if not dry_run:
                    dm_failed = False
                    try:
                        await member.send(summary)
                    except Exception:
                        log.append("⚠️ Could not send DM.")
                        dm_failed = True
                    if dm_failed:
                        summary = "\n".join(log)
                        await _flush(len(log) - 1)
                    await self.record_last_payment(member, summary)
                    await payment_label_set(str(member.id), "collect_rent_after")
                audit_lines.append(summary)

            except Exception as e:
                await ctx.send(f"❌ Error processing <@{member.id}>: `{e}`")
                if dry_run and admin_cog:
                    await admin_cog.log_audit(
                        ctx.author, f"Error processing <@{member.id}>: {e}"
                    )
                audit_lines.append(f"Error processing <@{member.id}>: {e}")

        if not dry_run:

            async def progress_after(member: discord.Member, idx: int, total: int) -> None:
                if verbose:
                    await ctx.send(f"💾 Finalised {member.display_name} ({idx}/{total})")

            await self.backup_balances(
                members_to_process,
                label="collect_rent_after",
                progress_hook=progress_after if verbose else None,
            )
        end_msg = (
            "✅ Rent simulation completed."
            if dry_run
            else "✅ Rent collection completed."
        )
        await ctx.send(end_msg)
        if dry_run and admin_cog:
            await admin_cog.log_audit(ctx.author, end_msg)
        if not dry_run:
            audit_lines.append(end_msg)
            audit_dir = Path(getattr(config, "RENT_AUDIT_DIR", "rent_audits"))
            audit_dir.mkdir(exist_ok=True)
            log_file = audit_dir / f"rent_audit_{datetime.utcnow():%B_%Y}.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n".join(audit_lines) + "\n")

            if notify_user:
                summary_lines: List[str] = []
                for m in members_to_process:
                    result = await self._evaluate_member_funds(m)
                    if not result:
                        continue
                    _total, deficit, _payable, unpaid = result
                    if deficit <= 0:
                        continue
                    baseline_only = all(
                        item.startswith("Baseline living cost") for item in unpaid
                    )
                    if baseline_only:
                        has_housing = any("Housing Tier" in r.name for r in m.roles)
                        has_business = any("Business Tier" in r.name for r in m.roles)
                        has_trauma = any(r.name in _cfg.get_trauma_role_costs() for r in m.roles)
                        if not (has_housing or has_business or has_trauma):
                            continue
                    items = [item.split(" ($")[0] for item in unpaid]
                    summary_lines.append(
                        f"{m.display_name} can't pay: {', '.join(items)}"
                    )
                summary_text = "\n".join(summary_lines) if summary_lines else "✅ Everyone paid their dues."
                try:
                    await notify_user.send(
                        f"✅ Rent collection completed.\n{summary_text}"
                    )
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
    @commands.command(aliases=["collectrent"])
    @commands.has_permissions(administrator=True)
    async def collect_rent(
        self, ctx, *args, target_user: Optional[discord.Member] = None
    ):
        """Global or per-member rent collection.

        Pass ``-v``/``--verbose`` for detailed output and ``-force`` to ignore the this-month guard.
        """
        verbose = False
        force = False
        if target_user is None:
            converter = commands.MemberConverter()
            remaining = []
            for arg in args:
                lower = arg.lower()
                if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                    verbose = True
                elif lower in {"-force", "--force", "force", "-f"}:
                    force = True
                else:
                    remaining.append(arg)
            for arg in remaining:
                try:
                    target_user = await converter.convert(ctx, arg)
                    break
                except commands.BadArgument:
                    continue
        else:
            for arg in args:
                lower = arg.lower()
                if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                    verbose = True
                elif lower in {"-force", "--force", "force", "-f"}:
                    force = True
        await self.run_rent_collection(
            ctx, target_user=target_user, dry_run=False, verbose=verbose, force=force
        )

    @commands.command(aliases=["simulaterent"])
    @commands.has_permissions(administrator=True)
    async def simulate_rent(
        self, ctx, *args, target_user: Optional[discord.Member] = None
    ):
        """Simulate rent collection without applying changes.

        Pass ``-v``/``--verbose`` to include detailed output. Use ``-cyberware``
        to also preview the upcoming cyberware medication cost for ``target_user``.
        """
        verbose = False
        include_cyber = False
        if target_user is None:
            converter = commands.MemberConverter()
            remaining = []
            for arg in args:
                lower = arg.lower()
                if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                    verbose = True
                elif lower in {"-cyberware", "--cyberware", "cyberware"}:
                    include_cyber = True
                else:
                    remaining.append(arg)
            for arg in remaining:
                try:
                    target_user = await converter.convert(ctx, arg)
                    break
                except commands.BadArgument:
                    continue
        else:
            for arg in args:
                lower = arg.lower()
                if lower in {"-v", "--verbose", "-verbose", "verbose"}:
                    verbose = True
                elif lower in {"-cyberware", "--cyberware", "cyberware"}:
                    include_cyber = True
        await self.run_rent_collection(
            ctx,
            target_user=target_user,
            dry_run=True,
            verbose=verbose,
            preview_dm=target_user is not None,
        )

        if include_cyber and target_user:
            cyber = self.bot.get_cog("CyberwareManager")
            if not cyber:
                await ctx.send("⚠️ Cyberware system not available.")
            else:
                guild = ctx.guild
                checkup = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
                medium = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
                high = guild.get_role(config.CYBER_HIGH_ROLE_ID)
                extreme = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
                level = None
                if extreme and extreme in target_user.roles:
                    level = "extreme"
                elif high and high in target_user.roles:
                    level = "high"
                elif medium and medium in target_user.roles:
                    level = "medium"

                if level:
                    weeks = self._get_cyber_weeks(cyber.data.get(str(target_user.id)))
                    if checkup and checkup in target_user.roles:
                        upcoming = weeks + 1
                        cost = cyber.calculate_cost(level, upcoming)
                        await ctx.send(f"💊 Cyberware meds week {upcoming}: ${cost}")
                    else:
                        await ctx.send("Cyberware checkup due — no med cost")
                else:
                    await ctx.send(f"{target_user.display_name} has no cyberware role.")

    @commands.command(name="paydue", aliases=["pay_due"])
    async def pay_due(self, ctx, *args: str) -> None:
        """Pay your monthly obligations early.

        Works like ``!collect_rent`` but only processes the invoking user.
        Use ``-v`` for a detailed summary.
        """
        verbose = any(a.lower() in {"-v", "--verbose", "verbose"} for a in args)
        await self.run_rent_collection(
            ctx,
            target_user=ctx.author,
            dry_run=False,
            verbose=verbose,
            force=False,
        )

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def simulate_all(
        self,
        ctx,
        *args,
        target_user: Optional[discord.Member] = None,
    ) -> None:
        """Run rent and cyberware simulations together.

        Unlike running ``simulate_rent`` and ``simulate_cyberware`` separately,
        this groups the rent and cyberware preview for each member into a single
        block so it's easier to evaluate the full monthly impact.
        """
        # Always provide detailed output for each member so the full cost
        # breakdown is easy to read. The ``-v`` flag is therefore ignored and
        # verbosity is enabled by default.
        verbose = True
        if target_user is None:
            converter = commands.MemberConverter()
            remaining = []
            for arg in args:
                lower = arg.lower()
                if lower not in {"-v", "--verbose", "-verbose", "verbose"}:
                    remaining.append(arg)
            for arg in remaining:
                try:
                    target_user = await converter.convert(ctx, arg)
                    break
                except commands.BadArgument:
                    continue
        else:
            for arg in args:
                # Legacy verbosity flags are ignored but accepted for
                # compatibility with older usage patterns.
                if arg.lower() in {"-v", "--verbose", "-verbose", "verbose"}:
                    continue

        await ctx.send("🧪 Starting combined simulation...")
        members: List[discord.Member] = []
        for m in ctx.guild.members:
            if target_user and m.id == target_user.id:
                if any(r.id == config.APPROVED_ROLE_ID for r in m.roles):
                    members = [m]
                break
            if not target_user:
                has_verified = any(r.id == config.VERIFIED_ROLE_ID for r in m.roles)
                has_tier = any("Tier" in r.name for r in m.roles)
                has_approved = any(r.id == config.APPROVED_ROLE_ID for r in m.roles)
                if (has_verified or has_tier) and has_approved:
                    members.append(m)
        if not members:
            if target_user:
                await ctx.send(
                    f"⏭️ <@{getattr(target_user, 'id', 0)}> has no approved character."
                )
            else:
                await ctx.send("❌ No matching members found.")
            return

        cyber = self.bot.get_cog("CyberwareManager")
        if not cyber:
            await ctx.send("⚠️ Cyberware system not available.")
            return

        admin_cog = self.bot.get_cog("Admin")

        for member in members:
            if not any(r.id == config.APPROVED_ROLE_ID for r in member.roles):
                await ctx.send(f"⏭️ Skipping <@{member.id}> — no approved character.")
                continue
            log: List[str] = [f"🔍 **Working on:** <@{member.id}>"]
            role_names = [r.name for r in member.roles]
            app_roles = [r for r in role_names if "Tier" in r]
            log.append(f"🏷️ Detected roles: {', '.join(app_roles) or 'None'}")

            loa_role = member.guild.get_role(config.LOA_ROLE_ID)
            on_loa = loa_role in member.roles if loa_role else False
            if on_loa:
                log.append("🏖️ Member is on LOA — skipping personal fees.")

            bal = await self.unbelievaboat.get_balance(member.id)
            if not bal:
                log.append("⚠️ Could not fetch balance.")
                summary = "\n".join(log)
                if verbose:
                    await ctx.send(summary)
                else:
                    await ctx.send(f"⚠️ Could not fetch balance for <@{member.id}>")
                if admin_cog:
                    await admin_cog.log_audit(ctx.author, summary)
                continue
            cash = bal.get("cash", 0)
            bank = bal.get("bank", 0)
            log.append(
                f"💵 Starting balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
            )

            check = await self.unbelievaboat.verify_balance_ops(member.id)
            log.append(
                "🔄 Balance check passed."
                if check
                else "⚠️ Balance update check failed."
            )

            if not on_loa:
                _ok, cash, bank = await self.deduct_flat_fee(
                    member, cash, bank, log, _cfg.get_baseline_living_cost(), dry_run=True
                )
                if not _ok:
                    log.append(
                        "⚠️ Baseline living cost unpaid. Continuing with rent steps."
                    )

            cash, bank = (
                await self.process_housing_rent(
                    member,
                    app_roles,
                    cash,
                    bank,
                    log,
                    None,
                    None,
                    dry_run=True,
                )
                if not on_loa
                else (cash, bank)
            )
            cash, bank = await self.process_business_rent(
                member,
                app_roles,
                cash,
                bank,
                log,
                None,
                None,
                dry_run=True,
            )

            if not on_loa:
                await self.trauma_service.process_trauma_team_payment(
                    member, log=log, dry_run=True
                )

            # Cyberware preview
            checkup = member.guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
            medium = member.guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
            high = member.guild.get_role(config.CYBER_HIGH_ROLE_ID)
            extreme = member.guild.get_role(config.CYBER_EXTREME_ROLE_ID)
            level = None
            if extreme and extreme in member.roles:
                level = "extreme"
            elif high and high in member.roles:
                level = "high"
            elif medium and medium in member.roles:
                level = "medium"
            if level and checkup and checkup in member.roles:
                weeks = self._get_cyber_weeks(cyber.data.get(str(member.id))) + 1
                cost = cyber.calculate_cost(level, weeks)
                log.append(f"💊 Cyberware meds week {weeks}: ${cost}")
                total = (cash or 0) + (bank or 0)
                if total >= cost:
                    deduct_cash = min(max(cash, 0), cost)
                    deduct_bank = max(0, cost - deduct_cash)
                    cash -= deduct_cash
                    bank -= deduct_bank
                    log.append(
                        f"🧮 Would subtract cyberware meds ${cost} — ${deduct_cash} from cash, {deduct_bank} from bank."
                    )
                else:
                    log.append(
                        f"❌ Cannot pay cyberware meds of ${cost}. Would result in negative balance."
                    )
            elif level:
                log.append("Cyberware checkup due — no med cost")

            log.append(
                f"📊 Projected balance — Cash: ${cash:,}, Bank: ${bank:,}, Total: {(cash or 0) + (bank or 0):,}"
            )

            summary = "\n".join(log)
            # Always send the detailed summary for each member.
            await ctx.send(summary)
            if admin_cog:
                await admin_cog.log_audit(ctx.author, summary)

        await ctx.send("✅ Simulation complete.")

    @commands.command(name="list_deficits")
    @commands.has_permissions(administrator=True)
    async def list_deficits(self, ctx) -> None:
        """Report members whose funds won't cover upcoming obligations.

        The output lists the shortfall amount and names of unpaid items for each
        affected member. Unpaid housing or business rent is marked ``(eviction)``
        and cyberware medication costs are included when relevant. Use
        ``simulate_all`` for a detailed balance preview.
        """
        await ctx.send("🔎 Checking member funds...")
        members = [
            m
            for m in ctx.guild.members
            if any("Tier" in r.name for r in m.roles)
            or any(r.id == config.VERIFIED_ROLE_ID for r in m.roles)
        ]
        failures: List[str] = []
        for m in members:
            result = await self._evaluate_member_funds(m)
            if not result:
                continue
            _total, deficit, _payable, unpaid = result
            if deficit <= 0:
                continue

            fail_items: List[str] = []
            for item in unpaid:
                name = item.split(" ($")[0]
                if "Housing Tier" in name or "Business Tier" in name:
                    fail_items.append(f"{name} (eviction)")
                else:
                    fail_items.append(name)
            fail_desc = ", ".join(fail_items) if fail_items else "None"
            failures.append(
                f"{m.display_name} short by ${deficit:,}. Can't pay: {fail_desc}."
            )

        if failures:
            for line in failures:
                await ctx.send(line)
        else:
            await ctx.send("✅ Everyone can cover their upcoming obligations.")

    @commands.command(name="trigger_auto_rent")
    @commands.has_permissions(administrator=True)
    async def trigger_auto_rent_command(self, ctx) -> None:
        """Manually trigger the auto rent collection cycle, bypassing the monthly guard.

        Unlike ``!collect_rent``, this runs immediately regardless of whether rent
        has already been collected this month — useful for testing and manual recovery.
        To suppress the automatic scheduler without disabling this command, run
        ``!disable_system auto_collect_rent``.
        """
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("auto_collect_rent"):
            await ctx.send(
                "⚠️ The `auto_collect_rent` system is disabled. "
                "Use `!enable_system auto_collect_rent` to re-enable, "
                "or run `!collect_rent -force` to collect manually."
            )
            return
        await ctx.send("🤖 Triggering auto rent collection (force mode)...")
        await self._run_auto_rent(triggered_by=f"manual/{ctx.author}", force=True)
