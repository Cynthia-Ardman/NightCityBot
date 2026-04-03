import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.utils.helpers import build_channel_name


class TestBuildChannelName:
    def test_short_names(self):
        result = build_channel_name([("alice", 111), ("bob", 222)])
        assert result.startswith("text-rp-")
        assert "alice" in result
        assert "bob" in result

    def test_sanitizes_special_chars(self):
        result = build_channel_name([("Alice B.", 111)])
        assert " " not in result
        assert "." not in result

    def test_truncates_long_names(self):
        long_names = [(f"user{i}", i) for i in range(20)]
        result = build_channel_name(long_names)
        assert len(result) <= 100

    def test_lowercase(self):
        result = build_channel_name([("UPPERCASE", 111)])
        assert result == result.lower()


class TestRPManagerOnMessage:
    def test_ignores_non_rp_channels(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "general"
        msg.content = "!roll 2d6"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cog.on_message(msg))
        finally:
            loop.close()

    def test_ignores_bot_messages(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        msg = MagicMock()
        msg.author = bot.user
        msg.author.bot = True

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cog.on_message(msg))
        finally:
            loop.close()


class TestEndRpSessionGuards:
    def test_rejects_non_forum_log_channel(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "text-rp-alice-111-bob-222"
        channel.guild = MagicMock()
        channel.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
        channel.send = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(cog.end_rp_session(channel))
        finally:
            loop.close()

        assert result is None
        channel.send.assert_called_once()
        assert "Cannot archive" in channel.send.call_args[0][0]


class TestCreateGroupRpChannel:
    def test_returns_none_on_forbidden(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))

        user = MagicMock(spec=discord.Member)
        user.name = "alice"
        user.id = 111

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(cog.create_group_rp_channel(guild, [user]))
        finally:
            loop.close()

        assert result is None

    def test_returns_none_on_http_exception(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "fail")
        )

        user = MagicMock(spec=discord.Member)
        user.name = "bob"
        user.id = 222

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(cog.create_group_rp_channel(guild, [user]))
        finally:
            loop.close()

        assert result is None

    def test_success_creates_channel(self):
        from NightCityBot.cogs.rp_manager import RPManager

        bot = MagicMock()
        cog = RPManager(bot)

        mock_channel = MagicMock(spec=discord.TextChannel)
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(return_value=mock_channel)

        user = MagicMock(spec=discord.Member)
        user.name = "charlie"
        user.id = 333

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(cog.create_group_rp_channel(guild, [user]))
        finally:
            loop.close()

        assert result == mock_channel
        guild.create_text_channel.assert_called_once()


class TestOnCommandError:
    def test_sends_permission_error(self):
        from NightCityBot.cogs.rp_manager import RPManager
        from discord.ext.commands import CheckFailure

        bot = MagicMock()
        cog = RPManager(bot)

        ctx = AsyncMock()
        ctx.command = "start_rp"
        ctx.send = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cog.on_command_error(ctx, CheckFailure("nope")))
        finally:
            loop.close()

        ctx.send.assert_called_once()
        assert "permission" in ctx.send.call_args[0][0].lower()
