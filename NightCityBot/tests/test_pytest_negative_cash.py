import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from discord.ext import commands
import config
from NightCityBot.cogs.economy import Economy
from NightCityBot.cogs.test_suite import TestSuite
from NightCityBot.tests.test_negative_cash import run as run_negative

class DummyBot:
    def __init__(self):
        self.cogs = {}
        self.loop = asyncio.new_event_loop()
        self.unbelievaboat = MagicMock()
    def add_cog(self, cog):
        self.cogs[cog.__class__.__name__] = cog
        for attr in dir(cog):
            cmd = getattr(cog, attr)
            if isinstance(cmd, commands.Command):
                cmd.cog = cog
    def get_cog(self, name):
        return self.cogs.get(name)

def setup_suite():
    bot = DummyBot()
    with patch("NightCityBot.services.unbelievaboat.aiohttp.ClientSession", new=MagicMock()):
        econ = Economy(bot)
    bot.add_cog(econ)
    ts = TestSuite(bot)
    bot.add_cog(ts)
    return ts

class DummyCtx:
    def __init__(self):
        self.guild = MagicMock()
        self.guild.get_member.return_value = MagicMock(id=config.TEST_USER_ID)
        self.guild.fetch_member = AsyncMock(return_value=MagicMock(id=config.TEST_USER_ID))
        self.eviction_channel = MagicMock(spec=discord.TextChannel)
        self.rent_log_channel = MagicMock(spec=discord.TextChannel)
        def channel_lookup(cid):
            if cid == config.EVICTION_CHANNEL_ID:
                return self.eviction_channel
            if cid == config.RENT_LOG_CHANNEL_ID:
                return self.rent_log_channel
            return MagicMock(spec=discord.TextChannel)
        self.guild.get_channel.side_effect = channel_lookup
        self.author = MagicMock(roles=[], display_name="Author")
        self.channel = MagicMock()
        self.send = AsyncMock()
        self.message = MagicMock(attachments=[])

def run_test(func):
    suite = setup_suite()
    ctx = DummyCtx()
    return asyncio.run(func(suite, ctx))


def test_negative_cash():
    logs = run_test(run_negative)
    assert all("❌" not in l for l in logs), f"Logs: {logs}"
