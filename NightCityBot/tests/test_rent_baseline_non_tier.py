from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config
from NightCityBot.utils.constants import BASELINE_LIVING_COST

async def run(suite, ctx) -> List[str]:
    """Ensure baseline rent is deducted for verified members without Tier roles."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    user = await suite.get_test_user(ctx)

    verified = MagicMock(spec=discord.Role)
    verified.name = 'Verified'
    verified.id = config.VERIFIED_ROLE_ID
    approved = MagicMock(spec=discord.Role)
    approved.name = 'Approved Character'
    approved.id = config.APPROVED_ROLE_ID
    user.roles = [verified, approved]
    ctx.guild.members = [user]
    ctx.send = AsyncMock()

    with (
        patch.object(economy.unbelievaboat, 'get_balance', new=AsyncMock(return_value={'cash': BASELINE_LIVING_COST, 'bank': 0})),
        patch.object(economy.unbelievaboat, 'update_balance', new=AsyncMock(return_value=True)) as mock_update,
        patch.object(economy, 'backup_balances', new=AsyncMock()),
        patch('NightCityBot.cogs.economy.db_load', new=AsyncMock(return_value={})),
        patch('NightCityBot.cogs.economy.db_save', new=AsyncMock()),
    ):
        await economy.collect_rent(ctx, target_user=user)
        suite.assert_called(logs, mock_update, 'update_balance')
        args = mock_update.await_args_list[0].args
        if args[0] == user.id and args[1].get('cash') == -BASELINE_LIVING_COST:
            logs.append('✅ baseline deducted for non-tier user')
        else:
            logs.append('❌ baseline deduction not applied correctly')

    return logs
