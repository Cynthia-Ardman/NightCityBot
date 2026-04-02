from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Ensure LOA works when different Role objects share the same ID."""
    logs: List[str] = []
    loa = suite.bot.get_cog('LOA')
    if not loa:
        logs.append("❌ LOA cog not loaded")
        return logs
    original_author = ctx.author
    mock_author = MagicMock(spec=discord.Member)
    mock_author.id = original_author.id
    mock_author.roles = []
    ctx.author = mock_author
    fixer = MagicMock()
    fixer.id = config.FIXER_ROLE_ID
    ctx.author.roles.append(fixer)
    target = MagicMock(spec=discord.Member)
    target.roles = []
    target.add_roles = AsyncMock()
    target.remove_roles = AsyncMock()
    loa_role1 = discord.Object(id=config.LOA_ROLE_ID)
    loa_role1.id = config.LOA_ROLE_ID
    loa_role2 = discord.Object(id=config.LOA_ROLE_ID)
    loa_role2.id = config.LOA_ROLE_ID
    with patch('discord.Guild.get_role', side_effect=[loa_role1, loa_role2]):
        await loa.start_loa.callback(loa, ctx, target)
        target.roles.append(loa_role1)
        await loa.end_loa.callback(loa, ctx, target)
    suite.assert_send(logs, target.add_roles, "add_roles")
    suite.assert_send(logs, target.remove_roles, "remove_roles")
    ctx.author.roles.remove(fixer)
    ctx.author = original_author
    return logs
