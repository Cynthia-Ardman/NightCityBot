from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import config


async def run(suite, ctx) -> List[str]:
    """Run simulate_all for a single user."""
    logs: List[str] = []
    economy = suite.bot.get_cog("Economy")
    cyber = suite.bot.get_cog("CyberwareManager")
    admin = suite.bot.get_cog("Admin")
    real_user = await suite.get_test_user(ctx)

    approved = MagicMock(spec=discord.Role)
    approved.name = "Approved Character"
    approved.id = config.APPROVED_ROLE_ID
    verified = MagicMock(spec=discord.Role)
    verified.name = "Verified"
    verified.id = config.VERIFIED_ROLE_ID

    user = MagicMock(spec=discord.Member)
    user.id = real_user.id
    user.display_name = real_user.display_name
    user.roles = [approved, verified]
    user.guild = ctx.guild

    ctx.send = AsyncMock()
    ctx.guild.members = [user]
    with (
        patch.object(
            economy.unbelievaboat,
            "get_balance",
            new=AsyncMock(return_value={"cash": 1000, "bank": 0}),
        ),
        patch.object(
            economy.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)
        ),
        patch.object(
            cyber.unbelievaboat,
            "get_balance",
            new=AsyncMock(return_value={"cash": 1000, "bank": 0}),
        ),
        patch.object(
            cyber.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)
        ),
        patch.object(admin, "log_audit", new=AsyncMock()) as mock_audit,
        patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock()),
        patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock()),
    ):
        await economy.simulate_all(ctx, target_user=user)
        suite.assert_called(logs, mock_audit, "log_audit")
        messages = [c.args[0] for c in ctx.send.await_args_list if c.args]
        if any("Baseline living cost" in m or "Working on" in m for m in messages):
            logs.append("✅ baseline shown")
        else:
            logs.append("❌ baseline missing")
    return logs
