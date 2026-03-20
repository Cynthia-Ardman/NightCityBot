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
        patch('NightCityBot.cogs.cyberware.cyberware_weekly_get_last_row', new=AsyncMock(return_value=(None, {}))),
        patch('NightCityBot.cogs.cyberware.cyberware_weekly_insert_empty', new=AsyncMock(return_value=1)) as mock_insert,
        patch('NightCityBot.cogs.cyberware.cyberware_weekly_update_row', new=AsyncMock(return_value=True)),
        patch('NightCityBot.cogs.cyberware.cyberware_status_upsert_many', new=AsyncMock()),
        patch('NightCityBot.cogs.cyberware.cyberware_last_run_set', new=AsyncMock()),
        patch.object(cyber.unbelievaboat, 'get_balance', new=AsyncMock(return_value={"cash": 500, "bank": 0})),
        patch.object(cyber.unbelievaboat, 'update_balance', new=AsyncMock(return_value=True)),
    ):
        await cyber.collect_cyberware.callback(cyber, ctx, user)
        suite.assert_called(logs, mock_insert, 'cyberware_weekly_insert_empty')
        if mock_insert.called:
            logs.append('✅ weekly entry created')
        else:
            logs.append('❌ weekly entry not created')
    ctx.author = original_author
    return logs
