import logging
import discord
from discord.ext import commands, tasks
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Optional, List

import config
from NightCityBot.utils.helpers import get_tz_now
from NightCityBot.utils.db import (
    cyberware_status_get_all,
    cyberware_status_upsert,
    cyberware_status_upsert_many,
    cyberware_last_run_get,
    cyberware_last_run_set,
    cyberware_weekly_add,
    cyberware_weekly_get_all,
    cyberware_weekly_get_last_row,
    cyberware_weekly_insert_empty,
    cyberware_weekly_update_row,
    warn_db_failure,
)
from NightCityBot.utils.permissions import is_ripperdoc, is_fixer
from NightCityBot.utils import config_loader as _cfg

logger = logging.getLogger(__name__)

# Legacy module-level dicts kept as fallback defaults; runtime values come from
# config_loader so server owners can edit them in the bot_config DB table.
MAX_COST = {
    "medium": 2000,
    "high": 5000,
    "extreme": 10000,
}


class CyberwareManager(commands.Cog):
    """Handle weekly cyberware check-ups and medication costs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.unbelievaboat = bot.unbelievaboat
        self.data: Dict[str, Dict[str, Optional[str] | int]] = {}
        self.last_run: Optional[datetime] = None
        self.bot.loop.create_task(self.load_data())
        self.weekly_check.start()

    async def load_data(self):
        raw = await cyberware_status_get_all()
        self.data = {}
        for user_id, v in raw.items():
            self.data[user_id] = {
                "weeks": int(v.get("weeks", 0)),
                "last": v.get("last"),
            }
        self.last_run = await cyberware_last_run_get()

    def cog_unload(self):
        self.weekly_check.cancel()

    def calculate_cost(self, level: str, weeks: int) -> int:
        """Return the medication cost for a given cyberware level and streak."""
        max_cost_map = _cfg.get_cyber_max_cost()
        max_c = max_cost_map.get(level, MAX_COST.get(level, 2000))
        base_factor = max_c / 128
        cost = int(base_factor * (2 ** (weeks - 1)))
        return min(cost, max_c)

    def _week_increment(self) -> int:
        """Return how many weeks have passed since the last full run."""
        if self.last_run:
            try:
                lr = self.last_run
                if lr.tzinfo is None:
                    lr = lr.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - lr
                inc = delta.days // 7
                return inc if inc >= 1 else 1
            except Exception:
                logger.warning("Could not compute week increment from last_run=%s", self.last_run)
        return 1

    @tasks.loop(time=time(hour=0, tzinfo=ZoneInfo(getattr(config, "TIMEZONE", "UTC"))))
    async def weekly_check(self):
        """Run every day and trigger processing each Monday."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            return
        if get_tz_now().weekday() != 0:  # Monday
            return
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
                await notify_user.send("🚦 Weekly cyberware processing starting...")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
        logs: List[str] = []
        results = await self.process_week(log=logs)

        run_at = datetime.now(timezone.utc)
        ok = await cyberware_weekly_add(
            run_at=run_at,
            checkup_ids=[str(x) for x in results.get("checkup", [])],
            paid_ids=[str(x) for x in results.get("paid", [])],
            unpaid_ids=[str(x) for x in results.get("unpaid", [])],
        )
        if not ok:
            await warn_db_failure(
                self.bot, "cyberware_weekly_add",
                "weekly run results not persisted",
            )

        summary = "\n".join(logs) if logs else "✅ No actions performed."
        if notify_user:
            try:
                await notify_user.send(
                    f"✅ Weekly cyberware processing complete:\n{summary}"
                )
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
        wholesaler = self.bot.get_cog("GunsShopCog")
        if wholesaler and hasattr(wholesaler, "auto_refresh_weekly_after_cyberware"):
            try:
                refreshed = await wholesaler.auto_refresh_weekly_after_cyberware()
            except Exception:
                refreshed = None
                logger.exception("auto_refresh_weekly_after_cyberware errored during weekly process")
            if notify_user:
                try:
                    if refreshed is None:
                        await notify_user.send("❌ Weekly gun wholesaler refresh errored.")
                    elif refreshed:
                        await notify_user.send("📦 Weekly gun wholesaler refresh complete.")
                    else:
                        await notify_user.send("⚠️ Weekly gun wholesaler refresh skipped/failed.")
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)

        cw_shop = self.bot.get_cog("CyberwareShop")
        if cw_shop and hasattr(cw_shop, "auto_cw_restock_if_due"):
            try:
                cw_refreshed = await cw_shop.auto_cw_restock_if_due(
                    datetime.now(timezone.utc)
                )
                if notify_user:
                    try:
                        await notify_user.send(
                            "🔩 Weekly cyberware wholesale rotation complete."
                            if cw_refreshed
                            else "⚠️ Weekly cyberware wholesale rotation skipped/failed."
                        )
                    except Exception:
                        logger.warning("Suppressed exception", exc_info=True)
            except Exception:
                logger.exception("auto_cw_restock_if_due errored during weekly process")
                if notify_user:
                    try:
                        await notify_user.send("❌ Weekly cyberware wholesale rotation errored.")
                    except Exception:
                        logger.warning("Suppressed exception", exc_info=True)

    async def process_week(
        self,
        *,
        dry_run: bool = False,
        log: Optional[List[str]] = None,
        target_member: Optional[discord.Member] = None,
    ) -> Dict[str, List[int]]:
        """Apply weekly check-up logic and deduct medication costs."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            if log is not None:
                log.append("Cyberware system disabled.")
            return {"checkup": [], "paid": [], "unpaid": []}

        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return {"checkup": [], "paid": [], "unpaid": []}

        checkup_role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        medium_role = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
        high_role = guild.get_role(config.CYBER_HIGH_ROLE_ID)
        extreme_role = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
        loa_role = guild.get_role(config.LOA_ROLE_ID)
        ripper_role = guild.get_role(config.RIPPERDOC_ROLE_ID)
        log_channel = guild.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)

        results = {"checkup": [], "paid": [], "unpaid": []}

        week_inc = self._week_increment()
        members = [target_member] if target_member else guild.members
        for member in members:
            if not any(r.id == config.APPROVED_ROLE_ID for r in member.roles):
                continue
            if loa_role and loa_role in member.roles:
                continue
            if ripper_role and ripper_role in member.roles:
                continue
            role_level = None
            if extreme_role and extreme_role in member.roles:
                role_level = "extreme"
            elif high_role and high_role in member.roles:
                role_level = "high"
            elif medium_role and medium_role in member.roles:
                role_level = "medium"

            user_id = str(member.id)
            entry = self.data.get(user_id, {"weeks": 0, "last": None})
            weeks = entry.get("weeks", 0)
            last_ts = entry.get("last")
            member_inc = week_inc
            if last_ts:
                try:
                    delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_ts).replace(tzinfo=timezone.utc)
                    inc = delta.days // 7
                    if inc > 0:
                        member_inc = max(member_inc, inc)
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
            has_checkup = checkup_role in member.roles if checkup_role else False

            if role_level is None:
                continue

            if not has_checkup:
                if checkup_role:
                    if not dry_run:
                        try:
                            await member.add_roles(
                                checkup_role, reason="Weekly cyberware check"
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                    if log is not None:
                        log.append(
                            f"{'Would give' if dry_run else 'Gave'} checkup role to <@{member.id}>"
                        )
                if log_channel and not dry_run:
                    await log_channel.send(
                        f"Ripperdoc checkup on <@{member.id}>. No money deducted."
                    )
                if log is not None:
                    log.append(
                        f"Ripperdoc checkup on <@{member.id}>. No money deducted."
                    )
                results["checkup"].append(member.id)
                if not dry_run:
                    self.data[user_id] = {"weeks": 0, "last": None}
                continue

            # User kept the checkup role for another week → charge them
            weeks += member_inc
            cost = self.calculate_cost(role_level, weeks)
            if log is not None:
                log.append(
                    f"Charging <@{member.id}> ${cost} for week {weeks} ({role_level})"
                )
            if not dry_run:
                balance = await self.unbelievaboat.get_balance(member.id)
                if balance:
                    cash = int(balance.get("cash", 0))
                    bank = int(balance.get("bank", 0))
                    total = cash + bank
                    if total >= cost:
                        safe_cash = max(cash, 0)
                        ok = await self.unbelievaboat.update_balance(
                            member.id,
                            {"cash": -min(cost, safe_cash), "bank": -max(0, cost - safe_cash)},
                            reason=f"Cyberware meds week {weeks}",
                        )
                        if ok:
                            if log is not None:
                                log.append(
                                    f"✅ Deducted ${cost} from <@{member.id}> for cyberware meds."
                                )
                            results["paid"].append(member.id)
                        else:
                            if log is not None:
                                log.append(
                                    f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                                )
                            results["unpaid"].append(member.id)
                    else:
                        if log is not None:
                            log.append(
                                f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                            )
                        results["unpaid"].append(member.id)
                else:
                    if log is not None:
                        log.append(
                            f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                        )
                    results["unpaid"].append(member.id)

            if not dry_run:
                self.data[user_id] = {
                    "weeks": weeks,
                    "last": datetime.now(timezone.utc).isoformat(),
                }
                if log is not None:
                    log.append(f"Streak is now {weeks} week(s) for <@{member.id}>")
            elif log is not None:
                log.append(f"Streak would become {weeks} week(s) for <@{member.id}>")

        if not dry_run:
            if target_member is None:
                self.last_run = datetime.now(timezone.utc)
                ok = await cyberware_last_run_set(self.last_run)
                if not ok:
                    await warn_db_failure(
                        self.bot, "cyberware_last_run_set",
                        "last-run timestamp not persisted",
                    )
            ok = await cyberware_status_upsert_many(self.data)
            if not ok:
                await warn_db_failure(
                    self.bot, "cyberware_status_upsert_many",
                    "cyberware status data not persisted",
                )
            if log is not None:
                log.append("✅ Data saved.")
        elif log is not None:
            log.append("Simulation complete. No changes saved.")

        return results

    @commands.command()
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def simulate_cyberware(
        self,
        ctx,
        member: Optional[str] = None,
        weeks: Optional[int] = None,
        *args: str,
    ):
        """Simulate weekly cyberware costs."""
        verbose = False
        remaining_args = []
        for arg in args:
            if arg.lower() in {"-v", "--verbose", "verbose"}:
                verbose = True
            else:
                remaining_args.append(arg)

        resolved_member: Optional[discord.Member] = None
        member_str = member
        if member_str is None and remaining_args:
            member_str = remaining_args.pop(0)
        if member_str is not None:
            try:
                member_id = int(str(member_str).strip("<@!>"))
                resolved_member = ctx.guild.get_member(member_id)
            except (ValueError, AttributeError):
                pass
        if weeks is None and remaining_args:
            try:
                weeks = int(remaining_args[0])
            except ValueError:
                pass

        if resolved_member and weeks is not None:
            level = None
            guild = ctx.guild
            if guild.get_role(config.CYBER_EXTREME_ROLE_ID) in resolved_member.roles:
                level = "extreme"
            elif guild.get_role(config.CYBER_HIGH_ROLE_ID) in resolved_member.roles:
                level = "high"
            elif guild.get_role(config.CYBER_MEDIUM_ROLE_ID) in resolved_member.roles:
                level = "medium"
            if level is None:
                await ctx.send(f"❌ {resolved_member.display_name} has no cyberware role.")
                return

            cost = self.calculate_cost(level, weeks)
            await ctx.send(
                f"💊 {resolved_member.display_name} would pay ${cost} for week {weeks}."
            )
            return

        logs: List[str] = []
        await self.process_week(dry_run=True, log=logs, target_member=resolved_member)
        summary = "\n".join(logs) if logs else "✅ Simulation complete."
        if verbose:
            await ctx.send(summary)
        else:
            await ctx.send("✅ Simulation complete.")
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    @commands.command(aliases=["check-up", "check_up", "cu", "cup"])
    @is_ripperdoc()
    async def checkup(self, ctx, member: discord.Member):
        """Remove the weekly cyberware checkup role from a member."""
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            await ctx.send("⚠️ The cyberware system is currently disabled.")
            return
        guild = ctx.guild
        role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        if role is None:
            await ctx.send("⚠️ Checkup role is not configured.")
            return

        if role not in member.roles:
            await ctx.send(f"{member.display_name} does not have the checkup role.")
            return

        try:
            await member.remove_roles(role, reason="Cyberware check-up completed")
        except (discord.Forbidden, discord.HTTPException) as e:
            await ctx.send(f"❌ Could not remove checkup role: {e}")
            return
        await ctx.send(f"✅ Removed checkup role from {member.display_name}.")

        log_channel = ctx.guild.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                f"Ripperdoc {ctx.author.display_name} did a checkup on {member.display_name}"
            )

        self.data[str(member.id)] = {"weeks": 0, "last": None}
        await cyberware_status_upsert(str(member.id), 0, None)

    @commands.command(aliases=["weekswithoutcheckup", "wwocup", "wwc"])
    @commands.check_any(is_ripperdoc(), is_fixer())
    async def weeks_without_checkup(self, ctx, member: discord.Member):
        """Show how many weeks a member has gone without a checkup."""
        entry = self.data.get(str(member.id))
        weeks = entry.get("weeks", 0) if isinstance(entry, dict) else int(entry or 0)
        await ctx.send(
            f"{member.display_name} has gone {weeks} week(s) without a checkup."
        )

    @commands.command(
        name="give_checkup_role",
        aliases=["givecheckuprole", "givecheckups", "cuall", "checkupall"],
    )
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def give_checkup_role(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Give the checkup role to a member or everyone with cyberware."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            await ctx.send("⚠️ The cyberware system is currently disabled.")
            return

        guild = ctx.guild
        checkup_role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        medium_role = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
        high_role = guild.get_role(config.CYBER_HIGH_ROLE_ID)
        extreme_role = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
        loa_role = guild.get_role(config.LOA_ROLE_ID)
        ripper_role = guild.get_role(config.RIPPERDOC_ROLE_ID)
        if checkup_role is None:
            await ctx.send("⚠️ Checkup role is not configured.")
            return

        members = [member] if member else guild.members
        count = 0
        for m in members:
            if loa_role and loa_role in m.roles:
                continue
            if ripper_role and ripper_role in m.roles:
                continue
            has_cyber = any(
                r for r in (medium_role, high_role, extreme_role) if r and r in m.roles
            )
            if not has_cyber:
                continue
            if checkup_role not in m.roles:
                try:
                    await m.add_roles(checkup_role, reason="Checkup role assign")
                except (discord.Forbidden, discord.HTTPException):
                    continue
                count += 1

        await ctx.send(f"✅ Gave the checkup role to {count} member(s).")

    @commands.command(name="checkup_report", aliases=["cu_report", "cur"])
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def checkup_report(self, ctx: commands.Context) -> None:
        """Show who did a checkup and who paid or failed to pay this week."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        data = await cyberware_weekly_get_all()
        if not data:
            await ctx.send("❌ No weekly data recorded yet.")
            return

        last = data[-1]
        guild = ctx.guild

        def mention_list(ids: List) -> str:
            names = []
            for uid in ids:
                member = guild.get_member(int(uid))
                names.append(member.display_name if member else f"<@{uid}>")
            return ", ".join(names) if names else "None"

        lines = [f"**Cyberware Report ({last['timestamp']})**"]
        lines.append(f"Did checkup: {mention_list(last.get('checkup', []))}")
        lines.append(f"Paid meds: {mention_list(last.get('paid', []))}")
        lines.append(f"Unpaid: {mention_list(last.get('unpaid', []))}")
        await ctx.send("\n".join(lines))

    @commands.command(name="collect_cyberware", aliases=["collectcyberware"])
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def collect_cyberware(
        self, ctx: commands.Context, member: discord.Member, *args: str
    ) -> None:
        """Manually collect cyberware medication from ``member``."""
        verbose = any(a.lower() in {"-v", "--verbose", "verbose"} for a in args)

        if not any(r.id == config.APPROVED_ROLE_ID for r in member.roles):
            await ctx.send(f"⏭️ {member.display_name} has no approved character.")
            return

        last_row_id, last_entry = await cyberware_weekly_get_last_row()
        if last_entry and (
            str(member.id) in last_entry.get("checkup", [])
            or str(member.id) in last_entry.get("paid", [])
        ):
            await ctx.send("⏭️ Member already processed this week.")
            return

        log_lines: List[str] = [f"💊 Manual cyberware collection for <@{member.id}>"]

        user_key = str(member.id)
        if user_key in self.data:
            msg = f"Found existing entry for {member.display_name} ({member.id})"
        else:
            msg = f"No entry found for {member.display_name} ({member.id}) – will add"
        logger.debug("[collect_cyberware] %s", msg)
        log_lines.append(msg)

        result = await self.process_week(log=log_lines, target_member=member)

        # Ensure there's a current weekly run row to update
        if last_row_id is None:
            last_row_id = await cyberware_weekly_insert_empty()

        if last_row_id is not None:
            prior = last_entry or {}
            paid_set = set(prior.get("paid", []))
            unpaid_set = set(prior.get("unpaid", []))
            if member.id in result.get("paid", []):
                paid_set.add(str(member.id))
                unpaid_set.discard(str(member.id))
            elif member.id in result.get("unpaid", []):
                unpaid_set.add(str(member.id))
            await cyberware_weekly_update_row(last_row_id, list(paid_set), list(unpaid_set))

        summary = "\n".join(log_lines) if log_lines else "✅ Completed."
        display = summary if verbose else "\n".join(log_lines[-3:])
        await ctx.send(display)
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    @commands.command(name="cyberware_status", aliases=["cstatus", "cstat"])
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def cyberware_status_cmd(self, ctx: commands.Context) -> None:
        """Display the current week status for all cyberware users."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        _last_row_id, last_entry = await cyberware_weekly_get_last_row()
        if last_entry:
            timestamp = last_entry.get("timestamp")
            checkup_set = set(str(x) for x in last_entry.get("checkup", []))
            paid_set = set(str(x) for x in last_entry.get("paid", []))
            unpaid_set = set(str(x) for x in last_entry.get("unpaid", []))
        else:
            timestamp = self.last_run.isoformat() if self.last_run else "N/A"
            checkup_set: set[str] = set()
            paid_set: set[str] = set()
            unpaid_set: set[str] = set()

        guild = ctx.guild
        medium_role = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
        high_role = guild.get_role(config.CYBER_HIGH_ROLE_ID)
        extreme_role = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
        loa_role = guild.get_role(config.LOA_ROLE_ID)

        lines = [f"**Cyberware Status ({timestamp})**"]
        for member in guild.members:
            if loa_role and loa_role in member.roles:
                continue
            has_cyber = any(
                r
                for r in (medium_role, high_role, extreme_role)
                if r and r in member.roles
            )
            if not has_cyber:
                continue
            uid = str(member.id)
            if uid in checkup_set:
                status = "checkup"
            elif uid in paid_set:
                status = "paid"
            elif uid in unpaid_set:
                status = "unpaid"
            else:
                status = "pending"

            weeks = self.data.get(uid, {}).get("weeks", 0)
            lines.append(f"{member.display_name}: {status} (week {weeks})")

        await ctx.send("\n".join(lines))

    @commands.command(name="paycyberware", aliases=["pay_cyberware"])
    async def pay_cyberware(self, ctx: commands.Context, *args: str) -> None:
        """Pay your cyberware medication cost manually."""
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            await ctx.send("❌ This command can only be used in a server.")
            return
        verbose = any(a.lower() in {"-v", "--verbose", "verbose"} for a in args)

        last_row_id, last_entry = await cyberware_weekly_get_last_row()
        if last_entry and (
            str(ctx.author.id) in last_entry.get("checkup", [])
            or str(ctx.author.id) in last_entry.get("paid", [])
        ):
            await ctx.send("⏭️ You already processed your cyberware this week.")
            return

        log_lines: List[str] = [
            f"💊 Manual cyberware collection for <@{ctx.author.id}>"
        ]
        result = await self.process_week(log=log_lines, target_member=ctx.author)

        if last_row_id is None:
            last_row_id = await cyberware_weekly_insert_empty()

        if last_row_id is not None:
            prior = last_entry or {}
            paid_set = set(prior.get("paid", []))
            unpaid_set = set(prior.get("unpaid", []))
            if ctx.author.id in result.get("paid", []):
                paid_set.add(str(ctx.author.id))
                unpaid_set.discard(str(ctx.author.id))
            elif ctx.author.id in result.get("unpaid", []):
                unpaid_set.add(str(ctx.author.id))
            await cyberware_weekly_update_row(last_row_id, list(paid_set), list(unpaid_set))

        summary = "\n".join(log_lines) if log_lines else "✅ Completed."
        display = summary if verbose else "\n".join(log_lines[-3:])
        await ctx.send(display)
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)

    @commands.command(name="manual_cyberware_log", aliases=["manualcyberwarelog", "mcl"])
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def manual_cyberware_log(
        self, ctx: commands.Context, member: discord.Member, weeks: int
    ) -> None:
        """Manually set a member's cyberware week count."""
        if weeks < 0:
            await ctx.send("❌ Weeks cannot be negative.")
            return
        user_id = str(member.id)
        last_str = self.data.get(user_id, {}).get("last") if isinstance(self.data.get(user_id), dict) else None
        last_dt = None
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
            except Exception:
                pass
        self.data[user_id] = {"weeks": weeks, "last": last_str}
        await cyberware_status_upsert(user_id, weeks, last_dt)
        await ctx.send(f"✅ Set {member.display_name}'s cyberware streak to {weeks} week(s).")
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog:
            await admin_cog.log_audit(
                ctx.author,
                f"Manually set {member.display_name}'s cyberware streak to {weeks} week(s).",
            )
