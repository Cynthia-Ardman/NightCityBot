from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Check that list_deficits reports users with short funds."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    cyber = suite.bot.get_cog('CyberwareManager')
    if not economy or not cyber:
        logs.append('❌ required cogs not loaded')
        return logs

    real_user = await suite.get_test_user(ctx)
    role_h = MagicMock(spec=discord.Role)
    role_h.name = 'Housing Tier 1'
    role_h.id = 1
    role_b = MagicMock(spec=discord.Role)
    role_b.name = 'Business Tier 1'
    role_b.id = 2
    medium = MagicMock(spec=discord.Role)
    medium.name = 'Cyberware Medium'
    medium.id = config.CYBER_MEDIUM_ROLE_ID
    checkup = MagicMock(spec=discord.Role)
    checkup.name = 'Cyberware Checkup'
    checkup.id = config.CYBER_CHECKUP_ROLE_ID
    verified = MagicMock(spec=discord.Role)
    verified.name = 'Verified'
    verified.id = config.VERIFIED_ROLE_ID

    user = MagicMock(spec=discord.Member)
    user.id = real_user.id
    user.display_name = real_user.display_name
    user.roles = [role_h, role_b, medium, checkup, verified]
    user.guild = ctx.guild

    from datetime import date, timedelta
    cyber.data[str(user.id)] = (date.today() - timedelta(days=7)).isoformat()
    ctx.guild.members = [user]
    ctx.send = AsyncMock()

    with patch.object(economy.unbelievaboat, 'get_balance', new=AsyncMock(return_value={'cash': 500, 'bank': 0})):
        await economy.list_deficits(ctx)
        suite.assert_send(logs, ctx.send, 'ctx.send')
        messages = [c.args[0] for c in ctx.send.await_args_list if c.args]
        combined = ' '.join(messages)
        if (
            'Housing Tier 1' in combined
            or 'Business Tier 1' in combined
            or 'short by' in combined
        ):
            logs.append('✅ unpaid items listed')
        else:
            logs.append(f'❌ unexpected messages: {combined}')
    return logs
