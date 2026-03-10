from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import config

async def run(suite, ctx) -> List[str]:
    """Ensure collect_rent backs up member balances."""
    logs = []
    real_user = await suite.get_test_user(ctx)
    approved = MagicMock(spec=discord.Role)
    approved.name = 'Approved Character'
    approved.id = config.APPROVED_ROLE_ID
    user = MagicMock(spec=discord.Member)
    user.id = real_user.id
    user.display_name = real_user.display_name
    user.roles = [approved]
    user.guild = ctx.guild
    economy = suite.bot.get_cog('Economy')
    ctx.send = AsyncMock()
    with (
        patch.object(
            economy.unbelievaboat,
            "get_balance",
            new=AsyncMock(return_value={"cash": 10000, "bank": 5000}),
        ),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch.object(economy, "backup_balances", new=AsyncMock()) as mock_backup,
    ):
        await economy.collect_rent(ctx, target_user=user)
        suite.assert_called(logs, mock_backup, "backup_balances")
    return logs
