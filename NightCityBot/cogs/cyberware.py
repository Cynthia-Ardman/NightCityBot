import discord
from discord.ext import commands, tasks
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Optional, List, Any
from pathlib import Path
import os

import config
from NightCityBot.utils.helpers import (
    load_json_file,
    save_json_file,
    append_json_file,
    get_tz_now,
)
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
from NightCityBot.utils.permissions import is_ripperdoc, is_fixer

MAX_COST = {
    "medium": 2000,
    "high": 5000,
    "extreme": 10000,
}
BASE_FACTOR = {k: v / 128 for k, v in MAX_COST.items()}


class CyberwareManager(commands.Cog):
    """Handle weekly cyberware check-ups and medication costs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.unbelievaboat = UnbelievaBoatAPI(config.UNBELIEVABOAT_API_TOKEN)
        self.data: Dict[str, Dict[str, Optional[str] | int]] = {}
        self.last_run: Optional[datetime] = None
        self.bot.loop.create_task(self.load_data())
        self.weekly_check.start()

    async def load_data(self):
        path = Path(config.CYBERWARE_LOG_FILE)
        raw = await load_json_file(path, default={})
        if isinstance(raw, dict):
            ts = raw.get("_last_run")
            if ts:
                try:
                    self.last_run = datetime.fromisoformat(ts)
                except Exception:
                    self.last_run = None
            self.data = {}
            for k, v in raw.items():
                if k == "_last_run":
                    continue
                if isinstance(v, dict):
                    self.data[k] = {
                        "weeks": int(v.get("weeks", 0)),
                        "last": v.get("last"),
                    }
                else:
                    self.data[k] = {"weeks": int(v), "last": None}
        else:
            self.data = {}

    def cog_unload(self):
        self.weekly_check.cancel()

    def calculate_cost(self, level: str, weeks: int) -> int:
        """Return the medication cost for a given cyberware level and streak."""
        base = BASE_FACTOR[level]
        cost = int(base * (2 ** (weeks - 1)))
        return min(cost, MAX_COST[level])

    def _week_increment(self) -> int:
        """Return how many weeks have passed since the last full run."""
        if self.last_run:
            delta = datetime.utcnow() - self.last_run
            inc = delta.days // 7
            return inc if inc >= 1 else 1
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
                pass

        logs: List[str] = []
        results = await self.process_week(log=logs)

        await append_json_file(
            Path(config.CYBERWARE_WEEKLY_FILE),
            {
                "timestamp": datetime.utcnow().isoformat(),
                "checkup": results.get("checkup", []),
                "paid": results.get("paid", []),
                "unpaid": results.get("unpaid", []),
            },
        )

        summary = "\n".join(logs) if logs else "✅ No actions performed."
        if notify_user:
            try:
                await notify_user.send(
                    f"✅ Weekly cyberware processing complete:\n{summary}"
                )
            except Exception:
                pass

        wholesaler = self.bot.get_cog("WholesalerCog")
        if wholesaler and hasattr(wholesaler, "auto_refresh_weekly_after_cyberware"):
            try:
                refreshed = await wholesaler.auto_refresh_weekly_after_cyberware()
                if notify_user:
                    await notify_user.send(
                        "📦 Weekly wholesaler refresh complete."
                        if refreshed
                        else "⚠️ Weekly wholesaler refresh skipped/failed."
                    )
            except Exception:
                if notify_user:
                    try:
                        await notify_user.send("❌ Weekly wholesaler refresh errored.")
                    except Exception:
                        pass

    async def process_week(
        self,
        *,
        dry_run: bool = False,
        log: Optional[List[str]] = None,
        target_member: Optional[discord.Member] = None,
    ) -> Dict[str, List[int]]:
        """Apply weekly check-up logic and deduct medication costs.

        Returns a mapping with keys ``checkup`` (players who did a checkup),
        ``paid`` (kept the role and paid) and ``unpaid`` (kept the role but
        couldn't pay).
        """
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

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
        today = get_tz_now().date()
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
                    delta = datetime.utcnow() - datetime.fromisoformat(last_ts)
                    inc = delta.days // 7
                    if inc > 0:
                        member_inc = max(member_inc, inc)
                except Exception:
                    pass

            has_checkup = checkup_role in member.roles if checkup_role else False

            if role_level is None:
                # Keep streak data if the user temporarily loses the role
                continue

            if not has_checkup:
                # Give the checkup role without charging
                if checkup_role:
                    if not dry_run:
                        await member.add_roles(
                            checkup_role, reason="Weekly cyberware check"
                        )
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
                log.append(f"Processing <@{member.id}> — week {weeks} cost ${cost}")
            balance = await self.unbelievaboat.get_balance(member.id)
            if not balance:
                if log_channel and not dry_run:
                    await log_channel.send(
                        f"⚠️ Could not fetch balance for <@{member.id}> to process cyberware meds."
                    )
                if log is not None:
                    if dry_run:
                        log.append(f"Would notify missing balance for <@{member.id}>")
                    else:
                        log.append(f"⚠️ Could not fetch balance for <@{member.id}>")
                continue

            if dry_run:
                check = await self.unbelievaboat.verify_balance_ops(member.id)
                if log is not None:
                    log.append(
                        "🔄 Balance check passed."
                        if check
                        else "⚠️ Balance update check failed."
                    )

            total = balance.get("cash", 0) + balance.get("bank", 0)
            if total < cost:
                if log_channel and not dry_run:
                    await log_channel.send(
                        f"🚨 <@{member.id}> cannot pay ${cost} for immunosuppressants and is in danger of cyberpsychosis."
                    )
                if log is not None:
                    if dry_run:
                        log.append(
                            f"Would warn insufficient funds for <@{member.id}> (${cost})"
                        )
                    else:
                        log.append(
                            f"🚨 <@{member.id}> cannot pay ${cost} for immunosuppressants"
                        )
                results["unpaid"].append(member.id)
            else:
                success = True
                if not dry_run:
                    success = await self.unbelievaboat.update_balance(
                        member.id, {"cash": -cost}, reason="Cyberware medication"
                    )
                if log_channel:
                    if success and not dry_run:
                        await log_channel.send(
                            f"✅ Deducted ${cost} for cyberware meds from <@{member.id}> (week {weeks})."
                        )
                    elif not success and not dry_run:
                        await log_channel.send(
                            f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                        )
                if log is not None:
                    if dry_run:
                        log.append(
                            f"✅ Would deduct ${cost} from <@{member.id}> for cyberware meds (week {weeks})."
                        )
                    else:
                        if success:
                            log.append(
                                f"✅ Deducted ${cost} from <@{member.id}> for cyberware meds (week {weeks})."
                            )
                            results["paid"].append(member.id)
                        else:
                            log.append(
                                f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                            )
                            results["unpaid"].append(member.id)

            if not dry_run:
                self.data[user_id] = {
                    "weeks": weeks,
                    "last": datetime.utcnow().isoformat(),
                }
                if log is not None:
                    log.append(f"Streak is now {weeks} week(s) for <@{member.id}>")
            elif log is not None:
                log.append(f"Streak would become {weeks} week(s) for <@{member.id}>")
        if not dry_run:
            if target_member is None:
                self.last_run = datetime.utcnow()
            save_payload = {**self.data}
            if self.last_run:
                save_payload["_last_run"] = self.last_run.isoformat()
            await save_json_file(Path(config.CYBERWARE_LOG_FILE), save_payload)
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
        *args: str,
    ):
        """Simulate weekly cyberware costs.

        With no arguments, performs a full dry-run of the weekly process. When
        ``member`` and ``weeks`` are provided, it simply calculates how much that
        user would owe on the given week.
        """

        member: Optional[str] = None
        weeks: Optional[int] = None
        verbose = False
        remaining = []
        for arg in args:
            if arg.lower() in {"-v", "--verbose", "verbose"}:
                verbose = True
            elif arg.isdigit() and weeks is None:
                weeks = int(arg)
            else:
                remaining.append(arg)
        if remaining:
            member = remaining[0]

        resolved_member: Optional[discord.Member] = None
        if member:
            try:
                resolved_member = await commands.MemberConverter().convert(ctx, member)
            except commands.BadArgument:
                await ctx.send("❌ Could not resolve user.")
                return
        if resolved_member and not any(
            r.id == config.APPROVED_ROLE_ID for r in resolved_member.roles
        ):
            await ctx.send(
                f"⏭️ {resolved_member.display_name} has no approved character."
            )
            return

        # Specific user/week cost preview
        if resolved_member and weeks is not None:
            guild = ctx.guild
            medium_role = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
            high_role = guild.get_role(config.CYBER_HIGH_ROLE_ID)
            extreme_role = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
            level = None
            if extreme_role and extreme_role in resolved_member.roles:
                level = "extreme"
            elif high_role and high_role in resolved_member.roles:
                level = "high"
            elif medium_role and medium_role in resolved_member.roles:
                level = "medium"

            if level is None:
                await ctx.send(f"{resolved_member.display_name} has no cyberware role.")
                return

            cost = self.calculate_cost(level, weeks)
            await ctx.send(
                f"💊 {resolved_member.display_name} would pay ${cost} for week {weeks}."
            )
            return

        # Global dry-run simulation or single-user when member is provided
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

        await member.remove_roles(role, reason="Cyberware check-up completed")
        await ctx.send(f"✅ Removed checkup role from {member.display_name}.")

        log_channel = ctx.guild.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                f"Ripperdoc {ctx.author.display_name} did a checkup on {member.display_name}"
            )

        self.data[str(member.id)] = {"weeks": 0, "last": None}
        payload = {**self.data}
        if self.last_run:
            payload["_last_run"] = self.last_run.isoformat()
        await save_json_file(Path(config.CYBERWARE_LOG_FILE), payload)

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
                await m.add_roles(checkup_role, reason="Checkup role assign")
                count += 1

        await ctx.send(f"✅ Gave the checkup role to {count} member(s).")

    @commands.command(name="checkup_report", aliases=["cu_report", "cur"])
    @commands.check_any(
        is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True)
    )
    async def checkup_report(self, ctx: commands.Context) -> None:
        """Show who did a checkup and who paid or failed to pay this week."""
        data = await load_json_file(Path(config.CYBERWARE_WEEKLY_FILE), default=[])
        if not data:
            await ctx.send("❌ No weekly data recorded yet.")
            return

        last = data[-1]
        guild = ctx.guild

        def mention_list(ids: List[int]) -> str:
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
        """Manually collect cyberware medication from ``member``.

        The command is ignored if the user already paid or had a checkup in the
        latest weekly entry. Without ``-v`` only the final few log lines are
        shown. Use ``-v`` for the complete processing log.
        """

        verbose = any(a.lower() in {"-v", "--verbose", "verbose"} for a in args)

        if not any(r.id == config.APPROVED_ROLE_ID for r in ctx.author.roles):
            await ctx.send("⏭️ You have no approved character.")
            return

        if not any(r.id == config.APPROVED_ROLE_ID for r in member.roles):
            await ctx.send(f"⏭️ {member.display_name} has no approved character.")
            return

        weekly_data = await load_json_file(
            Path(config.CYBERWARE_WEEKLY_FILE), default=[]
        )
        last = weekly_data[-1] if weekly_data else None
        if last and (
            member.id in last.get("checkup", [])
            or member.id in last.get("paid", [])
        ):
            await ctx.send("⏭️ Member already processed this week.")
            return

        log_lines: List[str] = [f"💊 Manual cyberware collection for <@{member.id}>"]

        # Report whether the member already exists in cyberware_log.json
        user_key = str(member.id)
        if user_key in self.data:
            msg = f"Found existing entry for {member.display_name} ({member.id})"
        else:
            msg = f"No entry found for {member.display_name} ({member.id}) – will add"
        print(f"[collect_cyberware] {msg}")
        log_lines.append(msg)

        result = await self.process_week(log=log_lines, target_member=member)

        if not weekly_data:
            weekly_data.append(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "checkup": [],
                    "paid": [],
                    "unpaid": [],
                }
            )

        last = weekly_data[-1]
        paid_set = set(map(int, last.get("paid", [])))
        unpaid_set = set(map(int, last.get("unpaid", [])))
        if member.id in result.get("paid", []):
            paid_set.add(member.id)
            unpaid_set.discard(member.id)
        elif member.id in result.get("unpaid", []):
            unpaid_set.add(member.id)
        last["paid"] = list(paid_set)
        last["unpaid"] = list(unpaid_set)
        await save_json_file(Path(config.CYBERWARE_WEEKLY_FILE), weekly_data)

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
    async def cyberware_status(self, ctx: commands.Context) -> None:
        """Display the current week status for all cyberware users."""

        weekly_data = await load_json_file(
            Path(config.CYBERWARE_WEEKLY_FILE), default=[]
        )
        if weekly_data:
            last = weekly_data[-1]
            timestamp = last.get("timestamp")
            checkup_set = set(map(int, last.get("checkup", [])))
            paid_set = set(map(int, last.get("paid", [])))
            unpaid_set = set(map(int, last.get("unpaid", [])))
        else:
            last = {}
            timestamp = None
            checkup_set: set[int] = set()
            paid_set: set[int] = set()
            unpaid_set: set[int] = set()
            # Fall back to the last_run value so the status output is still
            # meaningful even before the first weekly entry is created.
            if self.last_run:
                timestamp = self.last_run.isoformat()
        if timestamp is None:
            timestamp = "N/A"

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
            status: str
            if member.id in checkup_set:
                status = "checkup"
            elif member.id in paid_set:
                status = "paid"
            elif member.id in unpaid_set:
                status = "unpaid"
            else:
                status = "pending"

            weeks = self.data.get(str(member.id), {}).get("weeks", 0)
            lines.append(f"{member.display_name}: {status} (week {weeks})")

        await ctx.send("\n".join(lines))

    @commands.command(name="paycyberware", aliases=["pay_cyberware"])
    async def pay_cyberware(self, ctx: commands.Context, *args: str) -> None:
        """Pay your cyberware medication cost manually.

        Works like ``!collect_cyberware`` but applies only to you. Use ``-v``
        for a full processing log.
        """

        verbose = any(a.lower() in {"-v", "--verbose", "verbose"} for a in args)

        weekly_data = await load_json_file(
            Path(config.CYBERWARE_WEEKLY_FILE), default=[]
        )
        last = weekly_data[-1] if weekly_data else None
        if last and (
            ctx.author.id in last.get("checkup", [])
            or ctx.author.id in last.get("paid", [])
        ):
            await ctx.send("⏭️ You already processed your cyberware this week.")
            return

        log_lines: List[str] = [
            f"💊 Manual cyberware collection for <@{ctx.author.id}>"
        ]
        result = await self.process_week(log=log_lines, target_member=ctx.author)

        if not weekly_data:
            weekly_data.append(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "checkup": [],
                    "paid": [],
                    "unpaid": [],
                }
            )

        last = weekly_data[-1]
        paid_set = set(map(int, last.get("paid", [])))
        unpaid_set = set(map(int, last.get("unpaid", [])))
        if ctx.author.id in result.get("paid", []):
            paid_set.add(ctx.author.id)
            unpaid_set.discard(ctx.author.id)
        elif ctx.author.id in result.get("unpaid", []):
            unpaid_set.add(ctx.author.id)
        last["paid"] = list(paid_set)
        last["unpaid"] = list(unpaid_set)
        await save_json_file(Path(config.CYBERWARE_WEEKLY_FILE), weekly_data)

        summary = "\n".join(log_lines) if log_lines else "✅ Completed."
        display = summary if verbose else "\n".join(log_lines[-3:])
        await ctx.send(display)
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog:
            await admin_cog.log_audit(ctx.author, summary)
