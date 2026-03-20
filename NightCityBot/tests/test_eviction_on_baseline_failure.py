from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config
from NightCityBot.utils.constants import BASELINE_LIVING_COST

async def run(suite, ctx) -> List[str]:
    """Eviction notices should post even when baseline deduction fails."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    user = await suite.get_test_user(ctx)

    role_h = MagicMock(spec=discord.Role)
    role_h.name = 'Housing Tier 2'
    role_b = MagicMock(spec=discord.Role)
    role_b.name = 'Business Tier 2'
    approved = MagicMock(spec=discord.Role)
    approved.name = 'Approved Character'
    approved.id = config.APPROVED_ROLE_ID
    user.roles = [role_h, role_b, approved]
    ctx.guild.members = [user]

    eviction_channel = ctx.guild.get_channel(config.EVICTION_CHANNEL_ID)
    eviction_channel.send = AsyncMock()

    logs.append('→ Expected: baseline failure should still trigger eviction notices.')

    with (
        patch.object(economy.unbelievaboat, 'get_balance', new=AsyncMock(return_value={'cash': BASELINE_LIVING_COST - 400, 'bank': 0})),
        patch.object(economy.unbelievaboat, 'update_balance', new=AsyncMock(return_value=True)),
        patch.object(economy, 'backup_balances', new=AsyncMock()),
        patch.object(economy.trauma_service, 'process_trauma_team_payment', new=AsyncMock()),
        patch('NightCityBot.cogs.economy.db_load', new=AsyncMock(return_value={})),
        patch('NightCityBot.cogs.economy.db_save', new=AsyncMock()),
    ):
        await economy.collect_rent(ctx, target_user=user)
        eviction_calls = eviction_channel.send.await_args_list
        if len(eviction_calls) >= 3:
            logs.append('✅ eviction notices sent for baseline, housing and business')
        else:
            logs.append('❌ eviction notices missing')
    return logs
