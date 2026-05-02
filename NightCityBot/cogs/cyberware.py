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

    def preview_weekly_cost(self, member: discord.Member) -> Optional[Dict]:
        """Return the upcoming-Monday cyberware med preview for a member.

        Returns ``None`` ONLY when the member has no Medium/High/Extreme
        cyberware role at all (nothing to preview). LOA and Ripperdoc
        exemptions still suppress the actual charge but the preview will
        return the cyber data plus an ``exempt_reason`` so the caller can
        explain WHY there's no charge instead of leaving the user guessing.

        Result dict fields:

        - ``level``: "medium" / "high" / "extreme"
        - ``has_checkup``: bool, True if they currently have the checkup role
          (meaning they would be charged this Monday)
        - ``current_streak``: int, weeks already missed in the DB
        - ``upcoming_weeks``: int, the streak count after Monday's run
        - ``cost``: int, dollars they will owe THIS Monday (0 if no checkup
          role currently — they'll just be flagged; also 0 if exempt)
        - ``next_charge_cost``: int, projected cost the next time the system
          actually charges them (i.e. once they have the checkup role active
          on a Monday). Always > 0 for Medium/High/Extreme members so the
          preview shows a meaningful "you will eventually owe this" number.
        - ``next_charge_weeks``: int, the streak the projected charge applies
          to (current_streak + 1 if has_checkup, otherwise 1 because the
          streak gets reset when the role is freshly assigned).
        - ``exempt_reason``: Optional[str], either ``"loa"``, ``"ripperdoc"``
          or ``None`` if not exempt. When set, no charge will be applied by
          the weekly task.
        """
        guild = member.guild
        if guild is None:
            return None
        # Build role-ID set from BOTH the raw payload (`Member._roles`) AND
        # the resolved `Member.roles` property. Either source can be lossy
        # in different scenarios:
        #   - `member.roles` is a property that filters every role through
        #     `guild.get_role()` and silently drops any ID the guild's role
        #     cache doesn't currently know about (briefly stale right after
        #     a gateway resume or restart).
        #   - `member._roles` is the raw snowflake list from the payload but
        #     can be missing/empty on partial Member objects (e.g. ones built
        #     from message references rather than gateway/interaction data).
        # Unioning both gives us the most accurate picture in every code
        # path that calls this preview, including button interactions.
        member_role_ids: set[int] = set()
        raw_role_ids = getattr(member, "_roles", None)
        if raw_role_ids is not None:
            try:
                member_role_ids.update(int(r) for r in raw_role_ids)
            except (TypeError, ValueError):
                pass
        for r in getattr(member, "roles", []) or []:
            rid = getattr(r, "id", None)
            if rid is not None:
                try:
                    member_role_ids.add(int(rid))
                except (TypeError, ValueError):
                    pass

        if config.CYBER_EXTREME_ROLE_ID in member_role_ids:
            level = "extreme"
        elif config.CYBER_HIGH_ROLE_ID in member_role_ids:
            level = "high"
        elif config.CYBER_MEDIUM_ROLE_ID in member_role_ids:
            level = "medium"
        else:
            return None

        # Determine exemption AFTER detecting the cyber level so the caller
        # can show "you have High Cyberware but you're exempt because X".
        exempt_reason: Optional[str] = None
        if config.LOA_ROLE_ID in member_role_ids:
            exempt_reason = "loa"
        elif config.RIPPERDOC_ROLE_ID in member_role_ids:
            exempt_reason = "ripperdoc"

        has_checkup = config.CYBER_CHECKUP_ROLE_ID in member_role_ids

        entry = self.data.get(str(member.id), {"weeks": 0, "last": None})
        current_streak = int(entry.get("weeks", 0) or 0)

        if has_checkup:
            upcoming = current_streak + 1
            raw_cost = self.calculate_cost(level, upcoming)
            cost = 0 if exempt_reason else raw_cost
            next_charge_cost = raw_cost
            next_charge_weeks = upcoming
        else:
            upcoming = current_streak
            cost = 0
            # When the system runs and the member has no checkup role, the role
            # is freshly assigned and the streak is reset to 0 in the DB.
            # The first time they actually get charged after that, the streak
            # will be 1 (one week with the role active).
            next_charge_weeks = 1
            next_charge_cost = self.calculate_cost(level, next_charge_weeks)

        return {
            "level": level,
            "has_checkup": has_checkup,
            "current_streak": current_streak,
            "upcoming_weeks": upcoming,
            "cost": cost,
            "exempt_reason": exempt_reason,
            "next_charge_cost": next_charge_cost,
            "next_charge_weeks": next_charge_weeks,
        }

    async def _notify_member_checkup_due(self, member: discord.Member) -> None:
        """DM a member that they have a cyberware checkup due (no charge yet)."""
        try:
            await member.send(
                "🩺 **Cyberware Checkup Due**\n"
                "You've been flagged for your weekly cyberware checkup. "
                "No money was deducted this week — visit a Ripperdoc to clear the checkup. "
                "If you don't, medication costs will start accruing next week."
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Could not DM checkup-due notice to %s", member.id)

    async def _notify_member_charged(
        self,
        member: discord.Member,
        cost: int,
        weeks: int,
        level: str,
        cash_deduct: int,
        bank_deduct: int,
    ) -> None:
        """DM a member confirming a successful cyberware medication deduction."""
        breakdown_parts = []
        if cash_deduct > 0:
            breakdown_parts.append(f"${cash_deduct:,} from cash")
        if bank_deduct > 0:
            breakdown_parts.append(f"${bank_deduct:,} from bank")
        breakdown = " + ".join(breakdown_parts) if breakdown_parts else "$0"
        try:
            await member.send(
                f"💊 **Cyberware Medication Charged**\n"
                f"You were just charged **${cost:,}** for your weekly cyberware meds "
                f"({level} level, week {weeks} of missed checkups).\n"
                f"Breakdown: {breakdown}.\n"
                f"Visit a Ripperdoc and get a checkup to reset your streak before costs grow further."
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Could not DM charge notice to %s", member.id)

    async def _notify_member_payment_failed(
        self, member: discord.Member, cost: int, weeks: int, level: str
    ) -> None:
        """DM a member that a cyberware medication payment failed (insufficient funds)."""
        try:
            await member.send(
                f"❌ **Cyberware Medication Payment Failed**\n"
                f"You were due **${cost:,}** for your weekly cyberware meds "
                f"({level} level, week {weeks} of missed checkups), "
                f"but your balance was insufficient or the payment could not be processed.\n"
                f"Top up and visit a Ripperdoc as soon as possible — unpaid weeks compound."
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Could not DM payment-failed notice to %s", member.id)

    def _build_weekly_summary_embed(
        self, results: Dict[str, List], run_at: datetime
    ) -> discord.Embed:
        """Build the weekly cyberware summary embed for the cyberware-logs channel."""
        details = results.get("details", {}) if isinstance(results, dict) else {}
        paid = details.get("paid", []) or []
        unpaid = details.get("unpaid", []) or []
        checkup = details.get("checkup", []) or []

        total_charged = sum(int(e.get("cost", 0)) for e in paid)
        total_due_unpaid = sum(int(e.get("cost", 0)) for e in unpaid)

        embed = discord.Embed(
            title="📊 Weekly Cyberware Run",
            description=(
                f"**Charged:** {len(paid)} (${total_charged:,} collected)\n"
                f"**Payment Failed:** {len(unpaid)} (${total_due_unpaid:,} outstanding)\n"
                f"**Checkup Notices:** {len(checkup)}"
            ),
            color=discord.Color.teal(),
            timestamp=run_at,
        )

        # Discord allows 25 fields per embed. Reserve a budget per category so
        # one runaway list cannot starve the others, and append a "+N more"
        # tail line when truncated.
        MAX_FIELDS_PER_CATEGORY = 7

        def _add_chunked(name: str, lines: List[str]) -> None:
            if not lines:
                return
            chunks: List[str] = []
            cur = ""
            for line in lines:
                addition = (line if not cur else "\n" + line)
                if len(cur) + len(addition) > 1024:
                    if cur:
                        chunks.append(cur)
                    cur = line[:1024]
                else:
                    cur += addition
            if cur:
                chunks.append(cur)
            if len(chunks) > MAX_FIELDS_PER_CATEGORY:
                kept = chunks[:MAX_FIELDS_PER_CATEGORY]
                dropped_lines = sum(c.count("\n") + 1 for c in chunks[MAX_FIELDS_PER_CATEGORY:])
                tail = f"\n…and {dropped_lines} more"
                if len(kept[-1]) + len(tail) <= 1024:
                    kept[-1] = kept[-1] + tail
                else:
                    kept[-1] = kept[-1][: 1024 - len(tail)] + tail
                chunks = kept
            for i, chunk in enumerate(chunks):
                field_name = name if i == 0 else f"{name} (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)

        _add_chunked(
            "💊 Charged",
            [
                f"<@{e['id']}> — ${int(e['cost']):,} ({e['level']}, week {e['weeks']})"
                for e in paid
            ],
        )
        _add_chunked(
            "❌ Payment Failed",
            [
                f"<@{e['id']}> — ${int(e['cost']):,} ({e['level']}, week {e['weeks']})"
                for e in unpaid
            ],
        )
        _add_chunked(
            "🩺 Checkup Notices",
            [f"<@{e['id']}> ({e['level']})" for e in checkup],
        )

        if not (paid or unpaid or checkup):
            embed.add_field(
                name="Result",
                value="No members required action this week.",
                inline=False,
            )
        return embed

    async def _post_weekly_summary(
        self, results: Dict[str, List], run_at: datetime
    ) -> None:
        """Post the weekly cyberware summary embed to the cyberware-logs channel."""
        ch_id = getattr(config, "CYBERWARE_LOG_CHANNEL_ID", 0)
        if not ch_id:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        log_ch = None
        if guild:
            log_ch = guild.get_channel(ch_id)
        if log_ch is None:
            try:
                log_ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                logger.warning(
                    "Could not fetch CYBERWARE_LOG_CHANNEL_ID for weekly summary",
                    exc_info=True,
                )
                return
        try:
            embed = self._build_weekly_summary_embed(results, run_at)
            await log_ch.send(embed=embed)
        except Exception:
            logger.warning("Failed to post weekly cyberware summary", exc_info=True)

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
        try:
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

            await self._post_weekly_summary(results, run_at)

            summary = "\n".join(logs) if logs else "✅ No actions performed."
            if notify_user:
                try:
                    await notify_user.send(
                        f"✅ Weekly cyberware processing complete:\n{summary}"
                    )
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
        except Exception:
            logger.error("weekly_check failed", exc_info=True)

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
            return {
                "checkup": [], "paid": [], "unpaid": [],
                "details": {"checkup": [], "paid": [], "unpaid": []},
            }

        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return {
                "checkup": [], "paid": [], "unpaid": [],
                "details": {"checkup": [], "paid": [], "unpaid": []},
            }

        checkup_role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        medium_role = guild.get_role(config.CYBER_MEDIUM_ROLE_ID)
        high_role = guild.get_role(config.CYBER_HIGH_ROLE_ID)
        extreme_role = guild.get_role(config.CYBER_EXTREME_ROLE_ID)
        loa_role = guild.get_role(config.LOA_ROLE_ID)
        ripper_role = guild.get_role(config.RIPPERDOC_ROLE_ID)
        log_channel = guild.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)

        results: Dict[str, List] = {
            "checkup": [],
            "paid": [],
            "unpaid": [],
            "details": {
                "checkup": [],
                "paid": [],
                "unpaid": [],
            },
        }

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
                results["details"]["checkup"].append({
                    "id": member.id,
                    "level": role_level,
                })
                if not dry_run:
                    self.data[user_id] = {"weeks": 0, "last": None}
                    await self._notify_member_checkup_due(member)
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
                        cash_deduct = min(cost, safe_cash)
                        bank_deduct = max(0, cost - safe_cash)
                        ok = await self.unbelievaboat.update_balance(
                            member.id,
                            {"cash": -cash_deduct, "bank": -bank_deduct},
                            reason=f"Cyberware meds week {weeks}",
                        )
                        if ok:
                            if log is not None:
                                log.append(
                                    f"✅ Deducted ${cost} from <@{member.id}> for cyberware meds."
                                )
                            results["paid"].append(member.id)
                            results["details"]["paid"].append({
                                "id": member.id,
                                "cost": cost,
                                "weeks": weeks,
                                "level": role_level,
                                "cash": cash_deduct,
                                "bank": bank_deduct,
                            })
                            await self._notify_member_charged(
                                member, cost, weeks, role_level, cash_deduct, bank_deduct
                            )
                        else:
                            if log is not None:
                                log.append(
                                    f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                                )
                            results["unpaid"].append(member.id)
                            results["details"]["unpaid"].append({
                                "id": member.id,
                                "cost": cost,
                                "weeks": weeks,
                                "level": role_level,
                            })
                            await self._notify_member_payment_failed(member, cost, weeks, role_level)
                    else:
                        if log is not None:
                            log.append(
                                f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                            )
                        results["unpaid"].append(member.id)
                        results["details"]["unpaid"].append({
                            "id": member.id,
                            "cost": cost,
                            "weeks": weeks,
                            "level": role_level,
                        })
                        await self._notify_member_payment_failed(member, cost, weeks, role_level)
                else:
                    if log is not None:
                        log.append(
                            f"❌ Could not deduct ${cost} from <@{member.id}> for cyberware meds."
                        )
                    results["unpaid"].append(member.id)
                    results["details"]["unpaid"].append({
                        "id": member.id,
                        "cost": cost,
                        "weeks": weeks,
                        "level": role_level,
                    })
                    await self._notify_member_payment_failed(member, cost, weeks, role_level)

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
