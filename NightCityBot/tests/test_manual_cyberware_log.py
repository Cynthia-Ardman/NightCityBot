from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Ensure manual cyberware collection creates weekly log entry."""
    logs: List[str] = []
    cyber = suite.bot.get_cog('CyberwareManager')
    if not cyber:
        logs.append('❌ CyberwareManager cog not loaded')
        return logs
    real_user = await suite.get_test_user(ctx)

    approved = MagicMock(spec=discord.Role)
    approved.name = "Approved Character"
    approved.id = config.APPROVED_ROLE_ID
    medium = MagicMock(spec=discord.Role)
    medium.name = "Cyberware Medium"
    medium.id = config.CYBER_MEDIUM_ROLE_ID
    checkup = MagicMock(spec=discord.Role)
    checkup.name = "Cyberware Checkup"
    checkup.id = config.CYBER_CHECKUP_ROLE_ID

    original_author = ctx.author
    mock_author = MagicMock(spec=discord.Member)
    mock_author.id = original_author.id
    mock_author.display_name = original_author.display_name
    mock_author.roles = [approved]
    ctx.author = mock_author

    user = MagicMock(spec=discord.Member)
    user.id = real_user.id
    user.display_name = real_user.display_name
    user.roles = [approved, medium, checkup]
    user.guild = ctx.guild
    user.add_roles = AsyncMock()
    user.remove_roles = AsyncMock()

    ctx.send = AsyncMock()
    with (
        patch('NightCityBot.cogs.cyberware.db_load', new=AsyncMock(return_value=[])),
        patch('NightCityBot.cogs.cyberware.db_save', new=AsyncMock()) as mock_save,
        patch.object(cyber.unbelievaboat, 'get_balance', new=AsyncMock(return_value={"cash": 500, "bank": 0})),
        patch.object(cyber.unbelievaboat, 'update_balance', new=AsyncMock(return_value=True)),
    ):
        await cyber.collect_cyberware.callback(cyber, ctx, user)
        suite.assert_called(logs, mock_save, 'db_save')
        saved = mock_save.await_args_list[-1].args[1]
        if isinstance(saved, list) and saved:
            logs.append('✅ weekly entry created')
        else:
            logs.append(f'❌ unexpected weekly data: {saved}')
    ctx.author = original_author
    return logs
