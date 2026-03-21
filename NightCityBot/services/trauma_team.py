from typing import Optional, List
import discord
from NightCityBot.utils import config_loader as _cfg
import config


class TraumaTeamService:
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _split_deduction(cash: int, amount: int) -> tuple[int, int]:
        """Return cash and bank portions ensuring negative cash is ignored."""
        cash_deduct = min(max(cash, 0), amount)
        bank_deduct = max(0, amount - cash_deduct)
        return cash_deduct, bank_deduct

    async def process_trauma_team_payment(
            self,
            member: discord.Member,
            *,
            log: Optional[List[str]] = None,
            dry_run: bool = False,
    ) -> None:
        """Process Trauma Team subscription payment for a member."""
        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('trauma_team'):
            if log is not None:
                log.append('⚠️ Trauma Team system disabled.')
            return
        if any(r.id == config.LOA_ROLE_ID for r in member.roles):
            if log is not None:
                log.append("🛑 Skipping Trauma payment due to LOA.")
            return
        trauma_channel = self.bot.get_channel(config.TRAUMA_FORUM_CHANNEL_ID)
        if not isinstance(trauma_channel, discord.ForumChannel):
            if log is not None:
                log.append("⚠️ TT forum channel not found.")
            return

        balance = await self.bot.get_cog('Economy').unbelievaboat.get_balance(member.id)
        if not balance:
            if log is not None:
                log.append("⚠️ Could not fetch balance for Trauma processing.")
            return

        cash = balance["cash"]
        bank = balance["bank"]

        trauma_costs = _cfg.get_trauma_role_costs()
        trauma_role = next(
            (r for r in member.roles if r.name in trauma_costs),
            None
        )
        if not trauma_role:
            return  # no subscription

        cost = trauma_costs[trauma_role.name]
        if log is not None:
            log.append(f"🔎 {trauma_role.name} → Subscription: ${cost}")
            log.append(f"💊 Deducting ${cost} for Trauma Team plan: {trauma_role.name}")

        async def find_thread() -> Optional[discord.Thread]:
            def matches(t: discord.Thread) -> bool:
                return t.name.rsplit(" - ", 1)[-1] == str(member.id)

            for t in trauma_channel.threads:
                if matches(t):
                    return t

            async for t in trauma_channel.archived_threads(limit=None):
                if matches(t):
                    return t

            return None

        # Find user's trauma thread
        target_thread = await find_thread()
        if not target_thread:
            if log is not None:
                log.append(
                    f"⚠️ Could not locate Trauma Team thread for <@{member.id}>."
                    " Proceeding without thread."
                )

        if cash + bank < cost:
            mention = f"<@&{config.TRAUMA_TEAM_ROLE_ID}>"
            if not dry_run and target_thread:
                await target_thread.send(
                    f"❌ Payment for **{trauma_role.name}** (${cost}) by <@{member.id}> failed.",
                    f"\n## {mention} Subscription suspended.",
                )
            if log is not None:
                log.append("❌ Insufficient funds for Trauma payment.")
            return

        cash_deduct, bank_deduct = self._split_deduction(cash, cost)
        payload = {
            "cash": -cash_deduct,
            "bank": -bank_deduct,
        }
        success = True
        economy = self.bot.get_cog("Economy")
        if not dry_run and economy:
            await economy.backup_balances([member], label="cyberware_before")
            success = await economy.unbelievaboat.update_balance(
                member.id,
                payload,
                reason="Trauma Team Subscription",
            )
            if success:
                await economy.backup_balances([member], label="cyberware_after")

        if success:
            if not dry_run and target_thread:
                await target_thread.send(
                    f"✅ **Payment Successful** — <@{member.id}> paid `${cost}` for **{trauma_role.name}** coverage."
                )
            if log is not None:
                log.append(
                    "✅ Trauma Team payment completed." if not dry_run else "✅ (Simulated) Trauma Team payment would succeed."
                )
        else:
            if not dry_run and target_thread:
                await target_thread.send(
                    f"⚠️ **Deduction failed** for <@{member.id}> despite available funds."
                )
            if log is not None:
                log.append("⚠️ PATCH failed for Trauma Team payment.")
