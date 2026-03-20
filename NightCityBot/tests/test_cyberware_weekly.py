from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config
from NightCityBot.utils.constants import ROLE_COSTS_BUSINESS, ROLE_COSTS_HOUSING

async def run(suite, ctx) -> List[str]:
    """Simulate the weekly checkup task."""
    logs: List[str] = []
    manager = suite.bot.get_cog('CyberwareManager')
    if not manager:
        logs.append("❌ CyberwareManager cog not loaded")
        return logs

    control = suite.bot.get_cog('SystemControl')
    if control:
        await control.set_status('cyberware', True)

    guild = MagicMock()

    approved = MagicMock(spec=discord.Role)
    approved.id = config.APPROVED_ROLE_ID
    approved.name = "Approved Character"
    check = MagicMock(spec=discord.Role)
    check.id = config.CYBER_CHECKUP_ROLE_ID
    check.name = "Cyberware Checkup"
    medium = MagicMock(spec=discord.Role)
    medium.id = config.CYBER_MEDIUM_ROLE_ID
    medium.name = "Cyberware Medium"
    loa = MagicMock(spec=discord.Role)
    loa.id = config.LOA_ROLE_ID
    loa.name = "LOA"
    ripper = MagicMock(spec=discord.Role)
    ripper.id = getattr(config, "RIPPERDOC_ROLE_ID", 0)
    ripper.name = "Ripperdoc"

    role_map = {
        config.CYBER_CHECKUP_ROLE_ID: check,
        config.CYBER_MEDIUM_ROLE_ID: medium,
        config.LOA_ROLE_ID: loa,
        getattr(config, "RIPPERDOC_ROLE_ID", 0): ripper,
        getattr(config, "CYBER_HIGH_ROLE_ID", 0): None,
        getattr(config, "CYBER_EXTREME_ROLE_ID", 0): None,
    }
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    member_a = MagicMock(spec=discord.Member)
    member_a.id = 1
    member_a.roles = [approved, medium]
    member_a.add_roles = AsyncMock()

    member_b = MagicMock(spec=discord.Member)
    member_b.id = 2
    member_b.roles = [approved, medium, check]
    member_b.add_roles = AsyncMock()

    guild.members = [member_a, member_b]
    log_channel = MagicMock()
    log_channel.send = AsyncMock()
    guild.get_channel.return_value = log_channel

    with (
        patch.object(suite.bot, "get_guild", return_value=guild),
        patch.object(manager.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 5000, "bank": 0})),
        patch.object(manager.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)),
        patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock()),
        patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock()),
    ):
        await manager.process_week()
    suite.assert_send(logs, member_a.add_roles, "add_roles")
    suite.assert_send(logs, log_channel.send, "log_channel.send")
    return logs
