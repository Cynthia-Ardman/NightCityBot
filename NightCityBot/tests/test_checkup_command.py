from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config
from NightCityBot.utils.constants import ROLE_COSTS_BUSINESS, ROLE_COSTS_HOUSING

async def run(suite, ctx) -> List[str]:
    """Run the ripperdoc checkup command."""
    control = suite.bot.get_cog('SystemControl')
    if control:
        await control.set_status('cyberware', True)
    logs = []
    cyber = suite.bot.get_cog('CyberwareManager')
    if not cyber:
        logs.append("❌ CyberwareManager cog not loaded")
        return logs

    checkup_role = MagicMock(spec=discord.Role)
    checkup_role.id = config.CYBER_CHECKUP_ROLE_ID
    checkup_role.name = "Cyberware Checkup"

    member = MagicMock(spec=discord.Member)
    member.id = 99999
    member.display_name = "TestUser"
    member.roles = [checkup_role]
    member.remove_roles = AsyncMock()

    log_channel = MagicMock()
    log_channel.send = AsyncMock()
    ctx.send = AsyncMock()

    with (
        patch.object(ctx.guild, "get_role", return_value=checkup_role),
        patch.object(ctx.guild, "get_channel", return_value=log_channel),
        patch("NightCityBot.cogs.cyberware.db_save", new=AsyncMock()),
    ):
        await cyber.checkup.callback(cyber, ctx, member)
    suite.assert_send(logs, member.remove_roles, "remove_roles")
    suite.assert_send(logs, log_channel.send, "log_channel.send")
    entry = cyber.data.get(str(member.id))
    if isinstance(entry, dict) and entry.get("weeks") == 0:
        logs.append("✅ checkup streak reset")
    else:
        logs.append("❌ checkup streak not reset")
    return logs
