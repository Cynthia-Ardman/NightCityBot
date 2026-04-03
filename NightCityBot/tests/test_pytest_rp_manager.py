import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from typing import cast

import discord
import pytest

from NightCityBot.utils.helpers import build_channel_name
from NightCityBot.cogs.rp_manager import RPManager


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


def _make_cog():
    bot = MagicMock()
    return RPManager(bot)


class TestRPManagerOnMessage:
    def test_ignores_non_rp_channels(self):
        cog = _make_cog()
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "general"
        msg.content = "!roll 2d6"
        asyncio.run(cog.on_message(msg))

    def test_ignores_bot_messages(self):
        cog = _make_cog()
        msg = MagicMock()
        msg.author = cog.bot.user
        msg.author.bot = True
        asyncio.run(cog.on_message(msg))

    def test_ignores_non_command_in_rp_channel(self):
        cog = _make_cog()
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "text-rp-alice-bob"
        msg.content = "Hello, what do you think?"
        asyncio.run(cog.on_message(msg))
        msg.delete.assert_not_called()

    @patch("NightCityBot.cogs.rp_manager.asyncio.sleep", new_callable=AsyncMock)
    def test_deletes_command_in_rp_channel(self, mock_sleep):
        cog = _make_cog()
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=admin_cog)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "text-rp-alice-bob"
        msg.content = "!roll 2d6"
        msg.delete = AsyncMock()

        asyncio.run(cog.on_message(msg))
        msg.delete.assert_called_once()
        admin_cog.log_audit.assert_called()

    @patch("NightCityBot.cogs.rp_manager.asyncio.sleep", new_callable=AsyncMock)
    def test_handles_not_found_on_delete(self, mock_sleep):
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "text-rp-alice-bob"
        msg.content = "!end_rp"
        msg.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))

        asyncio.run(cog.on_message(msg))

    @patch("NightCityBot.cogs.rp_manager.asyncio.sleep", new_callable=AsyncMock)
    def test_handles_generic_exception_on_delete(self, mock_sleep):
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.name = "text-rp-alice-bob"
        msg.content = "!something"
        msg.delete = AsyncMock(side_effect=Exception("oops"))

        asyncio.run(cog.on_message(msg))


class TestStartRp:
    @patch("NightCityBot.cogs.rp_manager.config")
    def test_no_guild(self, mock_config):
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.guild = None
        asyncio.run(cog.start_rp.callback(cog, ctx))
        ctx.send.assert_called_once()
        assert "server" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.rp_manager.config")
    def test_no_users_resolved(self, mock_config):
        cog = _make_cog()
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=admin_cog)

        ctx = AsyncMock()
        ctx.guild = MagicMock(spec=discord.Guild)
        ctx.guild.get_member = MagicMock(return_value=None)
        ctx.message = MagicMock()
        ctx.message.content = "!start_rp nobody"
        ctx.message.delete = AsyncMock()
        ctx.author = MagicMock()

        asyncio.run(cog.start_rp.callback(cog, ctx, "nobody"))
        assert any("Could not resolve" in str(c) for c in ctx.send.call_args_list)

    @patch("NightCityBot.cogs.rp_manager.config")
    def test_channel_creation_failure(self, mock_config):
        mock_config.RP_IC_CATEGORY_ID = 123
        mock_config.FIXER_ROLE_ID = 555
        cog = _make_cog()
        cog.create_group_rp_channel = AsyncMock(return_value=None)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=admin_cog)

        member = MagicMock(spec=discord.Member)
        member.name = "alice"
        member.id = 111

        ctx = AsyncMock()
        ctx.guild = MagicMock(spec=discord.Guild)
        ctx.guild.get_member = MagicMock(return_value=member)
        ctx.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.CategoryChannel))
        ctx.channel = MagicMock()
        ctx.channel.category = MagicMock(spec=discord.CategoryChannel)
        ctx.channel.category.id = 123
        ctx.message = MagicMock()
        ctx.message.content = "!start_rp <@111>"
        ctx.message.delete = AsyncMock()
        ctx.author = MagicMock(spec=discord.Member)
        ctx.author.name = "fixer"
        ctx.author.id = 999

        asyncio.run(cog.start_rp.callback(cog, ctx, "<@111>"))
        assert any("Failed" in str(c) for c in ctx.send.call_args_list)

    @patch("NightCityBot.cogs.rp_manager.config")
    def test_success_creates_channel(self, mock_config):
        mock_config.RP_IC_CATEGORY_ID = 123
        mock_config.FIXER_ROLE_ID = 555
        cog = _make_cog()

        mock_channel = AsyncMock(spec=discord.TextChannel)
        mock_channel.mention = "#text-rp-alice"
        cog.create_group_rp_channel = AsyncMock(return_value=mock_channel)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=admin_cog)

        member = MagicMock(spec=discord.Member)
        member.name = "alice"
        member.id = 111
        member.mention = "<@111>"

        fixer_role = MagicMock()
        fixer_role.mention = "<@&555>"

        ctx = AsyncMock()
        ctx.guild = MagicMock(spec=discord.Guild)
        ctx.guild.get_member = MagicMock(return_value=member)
        ctx.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.CategoryChannel))
        ctx.guild.get_role = MagicMock(return_value=fixer_role)
        ctx.channel = MagicMock()
        ctx.channel.category = MagicMock(spec=discord.CategoryChannel)
        ctx.channel.category.id = 123
        ctx.message = MagicMock()
        ctx.message.content = "!start_rp <@111>"
        ctx.message.delete = AsyncMock()
        ctx.author = MagicMock(spec=discord.Member)
        ctx.author.name = "fixer"
        ctx.author.id = 999

        result = asyncio.run(cog.start_rp.callback(cog, ctx, "<@111>"))
        mock_channel.send.assert_called_once()
        assert "RP session created" in mock_channel.send.call_args[0][0]

    @patch("NightCityBot.cogs.rp_manager.config")
    def test_resolves_user_by_id(self, mock_config):
        mock_config.RP_IC_CATEGORY_ID = 123
        mock_config.FIXER_ROLE_ID = 555
        cog = _make_cog()

        mock_channel = AsyncMock(spec=discord.TextChannel)
        mock_channel.mention = "#text-rp-alice"
        cog.create_group_rp_channel = AsyncMock(return_value=mock_channel)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=admin_cog)

        member = MagicMock(spec=discord.Member)
        member.name = "alice"
        member.id = 111
        member.mention = "<@111>"

        ctx = AsyncMock()
        ctx.guild = MagicMock(spec=discord.Guild)
        ctx.guild.get_member = MagicMock(return_value=member)
        ctx.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.CategoryChannel))
        ctx.guild.get_role = MagicMock(return_value=None)
        ctx.channel = MagicMock()
        ctx.channel.category = MagicMock(spec=discord.CategoryChannel)
        ctx.channel.category.id = 123
        ctx.message = MagicMock()
        ctx.message.content = "!start_rp 111"
        ctx.message.delete = AsyncMock()
        ctx.author = MagicMock(spec=discord.Member)
        ctx.author.name = "fixer"
        ctx.author.id = 999

        asyncio.run(cog.start_rp.callback(cog, ctx, "111"))
        ctx.guild.get_member.assert_called_with(111)


class TestEndRp:
    def test_no_guild(self):
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.guild = None
        asyncio.run(cog.end_rp.callback(cog, ctx))
        ctx.send.assert_called_once()
        assert "server" in ctx.send.call_args[0][0].lower()

    def test_wrong_channel(self):
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.channel = MagicMock()
        ctx.channel.name = "general"
        asyncio.run(cog.end_rp.callback(cog, ctx))
        ctx.send.assert_called()
        assert any("RP session channel" in str(c) for c in ctx.send.call_args_list)

    def test_correct_channel_calls_end_session(self):
        cog = _make_cog()
        cog.end_rp_session = AsyncMock()
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.channel = MagicMock()
        ctx.channel.name = "text-rp-alice-bob"
        asyncio.run(cog.end_rp.callback(cog, ctx))
        cog.end_rp_session.assert_called_once_with(ctx.channel)


class TestEndRpSessionGuards:
    def test_rejects_non_forum_log_channel(self):
        cog = _make_cog()
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "text-rp-alice-111-bob-222"
        channel.guild = MagicMock()
        channel.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
        channel.send = AsyncMock()

        result = asyncio.run(cog.end_rp_session(channel))
        assert result is None
        channel.send.assert_called_once()
        assert "Cannot archive" in channel.send.call_args[0][0]

    def test_rejects_none_log_channel(self):
        cog = _make_cog()
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "text-rp-alice-bob"
        channel.guild = MagicMock()
        channel.guild.get_channel = MagicMock(return_value=None)
        channel.send = AsyncMock()

        result = asyncio.run(cog.end_rp_session(channel))
        assert result is None
        channel.send.assert_called_once()
        assert "Cannot archive" in channel.send.call_args[0][0]

    def test_success_logs_and_deletes(self):
        cog = _make_cog()
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "text-rp-alice-bob"
        channel.delete = AsyncMock()

        msg1 = MagicMock()
        msg1.created_at = MagicMock()
        msg1.created_at.strftime = MagicMock(return_value="2025-01-01 12:00:00")
        msg1.author = MagicMock()
        msg1.author.display_name = "alice"
        msg1.content = "Hello there"
        msg1.attachments = []

        async def mock_history(*args, **kwargs):
            for m in [msg1]:
                yield m

        channel.history = mock_history

        log_thread = AsyncMock(spec=discord.Thread)
        log_thread.id = 5555
        log_thread.send = AsyncMock()
        created = MagicMock()
        created.thread = log_thread

        forum = MagicMock(spec=discord.ForumChannel)
        forum.create_thread = AsyncMock(return_value=created)

        channel.guild = MagicMock()
        channel.guild.get_channel = MagicMock(return_value=forum)

        result = asyncio.run(cog.end_rp_session(channel))
        assert result is not None
        channel.delete.assert_called_once()

    def test_handles_exception(self):
        cog = _make_cog()
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "text-rp-alice-bob"
        channel.send = AsyncMock()

        forum = MagicMock(spec=discord.ForumChannel)
        forum.create_thread = AsyncMock(side_effect=Exception("API error"))

        channel.guild = MagicMock()
        channel.guild.get_channel = MagicMock(return_value=forum)

        result = asyncio.run(cog.end_rp_session(channel))
        assert result is None
        assert any("Error" in str(c) for c in channel.send.call_args_list)


class TestCreateGroupRpChannel:
    def test_returns_none_on_forbidden(self):
        cog = _make_cog()
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))

        user = MagicMock(spec=discord.Member)
        user.name = "alice"
        user.id = 111

        result = asyncio.run(cog.create_group_rp_channel(guild, [user]))
        assert result is None

    def test_returns_none_on_http_exception(self):
        cog = _make_cog()
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

        result = asyncio.run(cog.create_group_rp_channel(guild, [user]))
        assert result is None

    def test_success_creates_channel(self):
        cog = _make_cog()
        mock_channel = MagicMock(spec=discord.TextChannel)
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(return_value=mock_channel)

        user = MagicMock(spec=discord.Member)
        user.name = "charlie"
        user.id = 333

        result = asyncio.run(cog.create_group_rp_channel(guild, [user]))
        assert result == mock_channel
        guild.create_text_channel.assert_called_once()

    def test_success_with_fixer_role(self):
        cog = _make_cog()
        mock_channel = MagicMock(spec=discord.TextChannel)
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        fixer_role = MagicMock(spec=discord.Role)
        guild.get_role = MagicMock(return_value=fixer_role)
        guild.create_text_channel = AsyncMock(return_value=mock_channel)

        user = MagicMock(spec=discord.Member)
        user.name = "dave"
        user.id = 444

        result = asyncio.run(cog.create_group_rp_channel(guild, [user]))
        assert result == mock_channel
        call_kwargs = guild.create_text_channel.call_args[1]
        assert fixer_role in call_kwargs["overwrites"]

    def test_with_category(self):
        cog = _make_cog()
        mock_channel = MagicMock(spec=discord.TextChannel)
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        guild.create_text_channel = AsyncMock(return_value=mock_channel)

        user = MagicMock(spec=discord.Member)
        user.name = "eve"
        user.id = 555

        category = MagicMock(spec=discord.CategoryChannel)
        result = asyncio.run(cog.create_group_rp_channel(guild, [user], category))
        assert result == mock_channel
        call_kwargs = guild.create_text_channel.call_args[1]
        assert call_kwargs["category"] == category


class TestOnCommandError:
    def test_sends_permission_error(self):
        from discord.ext.commands import CheckFailure
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.command = "start_rp"
        ctx.send = AsyncMock()
        asyncio.run(cog.on_command_error(ctx, CheckFailure("nope")))
        ctx.send.assert_called_once()
        assert "permission" in ctx.send.call_args[0][0].lower()

    def test_sends_permission_error_for_missing_perms(self):
        from discord.ext.commands import MissingPermissions
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.command = "end_rp"
        ctx.send = AsyncMock()
        asyncio.run(cog.on_command_error(ctx, MissingPermissions(["admin"])))
        ctx.send.assert_called_once()

    def test_ignores_other_errors(self):
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.send = AsyncMock()
        asyncio.run(cog.on_command_error(ctx, RuntimeError("something else")))
        ctx.send.assert_not_called()

    def test_suppresses_send_failure(self):
        from discord.ext.commands import CheckFailure
        cog = _make_cog()
        ctx = AsyncMock()
        ctx.command = "start_rp"
        ctx.send = AsyncMock(side_effect=Exception("cannot send"))
        asyncio.run(cog.on_command_error(ctx, CheckFailure("nope")))
