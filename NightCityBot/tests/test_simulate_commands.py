from typing import List
from unittest.mock import AsyncMock, patch
import discord
import config

async def run(suite, ctx) -> List[str]:
    """Run rent and cyberware simulations and ensure audit logging."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    approved = discord.Object(id=config.APPROVED_ROLE_ID)
    ctx.author.roles = [approved]
    cyber = suite.bot.get_cog('CyberwareManager')
    admin = suite.bot.get_cog('Admin')
    ctx.send = AsyncMock()
    with (
        patch.object(economy.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 1000, "bank": 0})),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch.object(cyber.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 1000, "bank": 0})),
        patch.object(cyber.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch.object(admin, "log_audit", new=AsyncMock()) as mock_audit,
        patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock()),
        patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock()),
    ):
        await economy.simulate_rent(ctx, target_user=ctx.author)
        await cyber.simulate_cyberware(ctx, member=str(ctx.author.id))
        await cyber.simulate_cyberware(ctx, member=str(ctx.author.id), weeks=3)
        suite.assert_called(logs, mock_audit, "log_audit")
    return logs
