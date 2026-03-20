import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from discord.ext import commands
import config
from NightCityBot.cogs.cyberware import CyberwareManager
from NightCityBot.cogs.test_suite import TestSuite
from NightCityBot.tests.test_manual_cyberware_log import run as run_manual

class DummyBot:
    def __init__(self):
        self.cogs = {}
        self.loop = asyncio.new_event_loop()
        self.guild = MagicMock()
        self.unbelievaboat = MagicMock()
    def add_cog(self, cog):
        self.cogs[cog.__class__.__name__] = cog
        for attr in dir(cog):
            cmd = getattr(cog, attr)
            if isinstance(cmd, commands.Command):
                cmd.cog = cog
    def get_cog(self, name):
        return self.cogs.get(name)
    def get_guild(self, gid):
        return self.guild

class DummyCtx:
    def __init__(self):
        self.guild = MagicMock()
        self.guild.get_member.return_value = MagicMock(id=config.TEST_USER_ID)
        self.guild.fetch_member = AsyncMock(return_value=MagicMock(id=config.TEST_USER_ID))
        self.author = MagicMock(roles=[], display_name="Author")
        self.channel = MagicMock()
        self.send = AsyncMock()
        self.message = MagicMock(attachments=[])

def setup_suite():
    bot = DummyBot()
    with (
        patch("NightCityBot.services.unbelievaboat.aiohttp.ClientSession", new=MagicMock()),
        patch("asyncio.create_task", lambda *a, **k: None),
    ):
        cyber = CyberwareManager(bot)
    bot.add_cog(cyber)
    ts = TestSuite(bot)
    bot.add_cog(ts)
    return ts

def run_test(func):
    suite = setup_suite()
    ctx = DummyCtx()
    suite.bot.guild = ctx.guild
    return asyncio.run(func(suite, ctx))

def test_manual_cyberware_log():
    logs = run_test(run_manual)
    assert all("❌" not in l for l in logs), f"Logs: {logs}"
