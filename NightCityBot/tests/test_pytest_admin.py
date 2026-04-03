import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.admin import _embed_len, Admin


class TestEmbedLen:
    def test_empty_embed(self):
        e = discord.Embed()
        assert _embed_len(e) == 0

    def test_embed_with_title_and_description(self):
        e = discord.Embed(title="Hello", description="World")
        assert _embed_len(e) == len("Hello") + len("World")

    def test_embed_with_fields(self):
        e = discord.Embed(title="T")
        e.add_field(name="N", value="V")
        assert _embed_len(e) == len("T") + len("N") + len("V")

    def test_embed_with_footer(self):
        e = discord.Embed(title="T")
        e.set_footer(text="Footer")
        assert _embed_len(e) == len("T") + len("Footer")


class TestEmbedToText:
    def test_flattens_all_fields(self):
        e = discord.Embed(title="Title", description="Desc")
        e.add_field(name="FName", value="FVal")
        e.set_footer(text="Foot")
        result = Admin._embed_to_text(e)
        assert "Title" in result
        assert "Desc" in result
        assert "FName" in result
        assert "FVal" in result
        assert "Foot" in result

    def test_extracts_user_ids_from_mentions(self):
        e = discord.Embed(description="User <@123456789012345678> did something")
        result = Admin._embed_to_text(e)
        assert "123456789012345678" in result

    def test_empty_embed(self):
        e = discord.Embed()
        result = Admin._embed_to_text(e)
        assert result.strip() == ""


class TestIsTicketEmbed:
    def _make_message(self, author_name="SomeBot", embed_title=None, embed_desc=None):
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = author_name
        embed = discord.Embed(title=embed_title, description=embed_desc)
        msg.embeds = [embed] if (embed_title or embed_desc) else []
        return msg

    def test_tickety_author_with_embed(self):
        msg = self._make_message(author_name="Tickety Bot", embed_title="New Ticket")
        assert Admin._is_ticket_embed(msg) is True

    def test_non_ticket_message(self):
        msg = self._make_message(author_name="RandomBot", embed_title="Hello World")
        assert Admin._is_ticket_embed(msg) is False

    def test_ticket_keyword_in_description(self):
        msg = self._make_message(
            author_name="Webhook",
            embed_title="Log",
            embed_desc="A new ticket was created",
        )
        assert Admin._is_ticket_embed(msg) is True

    def test_transcript_keyword(self):
        msg = self._make_message(
            author_name="Logger",
            embed_title="Transcript saved",
        )
        assert Admin._is_ticket_embed(msg) is True

    def test_no_embeds(self):
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "Tickety"
        msg.embeds = []
        assert Admin._is_ticket_embed(msg) is False


class TestOnCommandError:
    def _make_admin(self):
        bot = MagicMock()
        audit_ch = AsyncMock(spec=discord.TextChannel)
        bot.get_channel = MagicMock(return_value=audit_ch)
        bot.get_user = MagicMock(return_value=None)
        bot.command_prefix = "!"
        admin = Admin(bot)
        return admin

    def test_ignores_bot_errors(self):
        from discord.ext import commands as _cmds
        admin = self._make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = True
        asyncio.run(admin.on_command_error(ctx, _cmds.CommandNotFound("nope")))
        ctx.send.assert_not_called()

    def test_unknown_command_sends_message(self):
        from discord.ext import commands as _cmds
        from NightCityBot.utils import constants
        admin = self._make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.message = MagicMock()
        ctx.message.content = "!foobar"
        ctx.channel = MagicMock()
        ctx.channel.name = "general"
        ctx.channel.id = 111
        asyncio.run(admin.on_command_error(ctx, _cmds.CommandNotFound("foobar")))
        ctx.send.assert_called_once()
        assert "Unknown command" in ctx.send.call_args[0][0]

    def test_ignores_unbelievaboat_commands(self):
        from discord.ext import commands as _cmds
        from NightCityBot.utils import constants
        admin = self._make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.message = MagicMock()
        ub_cmd = list(constants.UNBELIEVABOAT_COMMANDS)[0] if constants.UNBELIEVABOAT_COMMANDS else "balance"
        ctx.message.content = f"!{ub_cmd}"
        asyncio.run(admin.on_command_error(ctx, _cmds.CommandNotFound(ub_cmd)))
        ctx.send.assert_not_called()

    def test_check_failure_sends_reason(self):
        from discord.ext import commands as _cmds
        admin = self._make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.author.id = 99
        ctx.author.__str__ = lambda self: "User"
        ctx.message = MagicMock()
        ctx.message.content = "!secret"
        asyncio.run(admin.on_command_error(ctx, _cmds.CheckFailure("Fixer role required")))
        assert "Fixer role required" in ctx.send.call_args[0][0]

    def test_user_input_error_shows_usage(self):
        from discord.ext import commands as _cmds
        admin = self._make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.command = MagicMock()
        ctx.command.signature = "@user <amount>"
        ctx.command.qualified_name = "pay"
        ctx.message = MagicMock()
        ctx.message.content = "!pay"
        asyncio.run(admin.on_command_error(ctx, _cmds.MissingRequiredArgument(MagicMock(name="user"))))
        text = ctx.send.call_args[0][0]
        assert "Usage" in text


class TestLogAudit:
    def test_sends_embed_to_audit_channel(self):
        bot = MagicMock()
        audit_ch = AsyncMock(spec=discord.TextChannel)
        bot.get_channel = MagicMock(return_value=audit_ch)
        admin = Admin(bot)

        user = MagicMock()
        user.id = 12345
        user.__str__ = lambda self: "TestUser"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(admin.log_audit(user, "Test action"))
        finally:
            loop.close()

        audit_ch.send.assert_called_once()
        embed = audit_ch.send.call_args[1]["embed"]
        assert embed.title == "\U0001f4dd Audit Log"

    def test_chunks_long_action_desc(self):
        bot = MagicMock()
        audit_ch = AsyncMock(spec=discord.TextChannel)
        bot.get_channel = MagicMock(return_value=audit_ch)
        admin = Admin(bot)

        user = MagicMock()
        user.id = 12345
        user.__str__ = lambda self: "TestUser"

        long_action = "x" * 2500
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(admin.log_audit(user, long_action))
        finally:
            loop.close()

        embed = audit_ch.send.call_args[1]["embed"]
        assert len(embed.fields) >= 3

    def test_warns_when_channel_not_text(self):
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=None)
        admin = Admin(bot)

        user = MagicMock()
        user.id = 99
        user.__str__ = lambda self: "Nobody"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(admin.log_audit(user, "action"))
        finally:
            loop.close()
