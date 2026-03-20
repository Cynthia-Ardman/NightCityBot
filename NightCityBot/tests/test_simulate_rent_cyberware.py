from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Run simulate_rent with -cyberware flag."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    cyber = suite.bot.get_cog('CyberwareManager')
    real_user = await suite.get_test_user(ctx)

    medium = MagicMock(spec=discord.Role)
    medium.name = "Cyberware Medium"
    medium.id = config.CYBER_MEDIUM_ROLE_ID
    checkup = MagicMock(spec=discord.Role)
    checkup.name = "Cyberware Checkup"
    checkup.id = config.CYBER_CHECKUP_ROLE_ID
    approved = MagicMock(spec=discord.Role)
    approved.name = "Approved Character"
    approved.id = config.APPROVED_ROLE_ID

    user = MagicMock(spec=discord.Member)
    user.id = real_user.id
    user.display_name = real_user.display_name
    user.roles = [medium, checkup, approved]
    user.guild = ctx.guild

    from datetime import date, timedelta
    cyber.data[str(user.id)] = (date.today() - timedelta(days=7)).isoformat()
    ctx.send = AsyncMock()

    with (
        patch.object(economy.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 1000, "bank": 0})),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch.object(cyber.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 1000, "bank": 0})),
        patch.object(cyber.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock()),
        patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock()),
        patch.object(ctx.guild, "get_role", side_effect=lambda rid: {
            config.CYBER_MEDIUM_ROLE_ID: medium,
            config.CYBER_CHECKUP_ROLE_ID: checkup,
            getattr(config, "CYBER_HIGH_ROLE_ID", 0): None,
            getattr(config, "CYBER_EXTREME_ROLE_ID", 0): None,
        }.get(rid)),
    ):
        await economy.simulate_rent(ctx, "-cyberware", target_user=user)
    messages = [c.args[0] for c in ctx.send.await_args_list if c.args]
    if any("Cyberware meds week" in str(m) for m in messages):
        logs.append("✅ cyberware cost included")
    else:
        logs.append("❌ cyberware cost missing")
    return logs
