import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

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

    def test_includes_author_name(self):
        e = discord.Embed()
        e.set_author(name="AuthorBot")
        result = Admin._embed_to_text(e)
        assert "AuthorBot" in result


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

    def test_ticket_keyword_in_field(self):
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "SomeWebhook"
        e = discord.Embed(title="Log Entry")
        e.add_field(name="Status", value="Ticket closed by admin")
        msg.embeds = [e]
        assert Admin._is_ticket_embed(msg) is True


def _make_admin():
    bot = MagicMock()
    audit_ch = AsyncMock(spec=discord.TextChannel)
    bot.get_channel = MagicMock(return_value=audit_ch)
    bot.get_user = MagicMock(return_value=None)
    bot.command_prefix = "!"
    admin = Admin(bot)
    return admin


class TestOnCommandError:
    def test_ignores_bot_errors(self):
        from discord.ext import commands as _cmds
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = True
        asyncio.run(admin.on_command_error(ctx, _cmds.CommandNotFound("nope")))
        ctx.send.assert_not_called()

    def test_unknown_command_sends_message(self):
        from discord.ext import commands as _cmds
        admin = _make_admin()
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
        admin = _make_admin()
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
        admin = _make_admin()
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
        admin = _make_admin()
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

    def test_generic_error_sends_warning_and_alerts(self):
        from discord.ext import commands as _cmds
        admin = _make_admin()
        admin._alert_report_user = AsyncMock()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.author.id = 42
        ctx.author.__str__ = lambda self: "User42"
        ctx.message = MagicMock()
        ctx.message.content = "!broken"
        ctx.channel = MagicMock()
        ctx.channel.name = "general"
        asyncio.run(admin.on_command_error(ctx, RuntimeError("unexpected")))
        ctx.send.assert_called_once()
        assert "Error" in ctx.send.call_args[0][0]
        admin._alert_report_user.assert_called_once()

    def test_empty_command_not_found(self):
        from discord.ext import commands as _cmds
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.bot = False
        ctx.message = MagicMock()
        ctx.message.content = "!"
        asyncio.run(admin.on_command_error(ctx, _cmds.CommandNotFound("")))


class TestLogAudit:
    def test_sends_embed_to_audit_channel(self):
        bot = MagicMock()
        audit_ch = AsyncMock(spec=discord.TextChannel)
        bot.get_channel = MagicMock(return_value=audit_ch)
        admin = Admin(bot)

        user = MagicMock()
        user.id = 12345
        user.__str__ = lambda self: "TestUser"

        asyncio.run(admin.log_audit(user, "Test action"))
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
        asyncio.run(admin.log_audit(user, long_action))
        embed = audit_ch.send.call_args[1]["embed"]
        assert len(embed.fields) >= 3

    def test_warns_when_channel_not_text(self):
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=None)
        admin = Admin(bot)

        user = MagicMock()
        user.id = 99
        user.__str__ = lambda self: "Nobody"

        asyncio.run(admin.log_audit(user, "action"))


class TestIndexMessage:
    def test_skips_already_indexed(self):
        admin = _make_admin()
        admin._ticket_index_ids = {"12345"}
        msg = MagicMock()
        msg.id = 12345
        result = asyncio.run(admin._index_message(msg))
        assert result is False

    def test_skips_no_embeds(self):
        admin = _make_admin()
        msg = MagicMock()
        msg.id = 99999
        msg.embeds = []
        result = asyncio.run(admin._index_message(msg))
        assert result is False

    def test_skips_non_ticket_embed(self):
        admin = _make_admin()
        msg = MagicMock(spec=discord.Message)
        msg.id = 99999
        msg.author = MagicMock()
        msg.author.display_name = "RandomBot"
        embed = discord.Embed(title="Hello World")
        msg.embeds = [embed]
        result = asyncio.run(admin._index_message(msg))
        assert result is False

    @patch("NightCityBot.cogs.admin._db.get_pool")
    def test_indexes_ticket_embed(self, mock_get_pool):
        mock_pool = AsyncMock()
        mock_get_pool.return_value = mock_pool
        admin = _make_admin()
        msg = MagicMock(spec=discord.Message)
        msg.id = 77777
        msg.author = MagicMock()
        msg.author.display_name = "Tickety"
        msg.jump_url = "https://discord.com/msg/77777"
        msg.created_at = datetime(2025, 1, 1)
        embed = discord.Embed(title="New Ticket #42", description="User opened a ticket")
        msg.embeds = [embed]
        result = asyncio.run(admin._index_message(msg))
        assert result is True
        assert "77777" in admin._ticket_index_ids
        mock_pool.execute.assert_called_once()

    @patch("NightCityBot.cogs.admin._db.get_pool")
    def test_index_db_failure(self, mock_get_pool):
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(side_effect=Exception("DB down"))
        mock_get_pool.return_value = mock_pool
        admin = _make_admin()
        msg = MagicMock(spec=discord.Message)
        msg.id = 88888
        msg.author = MagicMock()
        msg.author.display_name = "Tickety"
        msg.jump_url = "https://discord.com/msg/88888"
        msg.created_at = datetime(2025, 1, 1)
        embed = discord.Embed(title="New Ticket", description="ticket content")
        msg.embeds = [embed]
        result = asyncio.run(admin._index_message(msg))
        assert result is False


class TestOnMessageTicketListener:
    @patch("NightCityBot.cogs.admin.config")
    def test_ignores_wrong_channel(self, mock_config):
        mock_config.TICKETY_LOG_CHANNEL_ID = 999
        admin = _make_admin()
        msg = MagicMock()
        msg.channel = MagicMock()
        msg.channel.id = 123
        asyncio.run(admin.on_message(msg))

    @patch("NightCityBot.cogs.admin.config")
    def test_ignores_non_ticket_message(self, mock_config):
        mock_config.TICKETY_LOG_CHANNEL_ID = 999
        admin = _make_admin()
        msg = MagicMock(spec=discord.Message)
        msg.channel = MagicMock()
        msg.channel.id = 999
        msg.author = MagicMock()
        msg.author.display_name = "SomeBot"
        msg.embeds = [discord.Embed(title="Hello")]
        asyncio.run(admin.on_message(msg))

    @patch("NightCityBot.cogs.admin._db.get_pool")
    @patch("NightCityBot.cogs.admin.config")
    def test_indexes_ticket_in_correct_channel(self, mock_config, mock_get_pool):
        mock_config.TICKETY_LOG_CHANNEL_ID = 999
        mock_pool = AsyncMock()
        mock_get_pool.return_value = mock_pool
        admin = _make_admin()
        msg = MagicMock(spec=discord.Message)
        msg.channel = MagicMock()
        msg.channel.id = 999
        msg.id = 55555
        msg.author = MagicMock()
        msg.author.display_name = "Tickety"
        msg.jump_url = "https://discord.com/msg/55555"
        msg.created_at = datetime(2025, 1, 1)
        embed = discord.Embed(title="New Ticket #10")
        msg.embeds = [embed]
        asyncio.run(admin.on_message(msg))
        assert "55555" in admin._ticket_index_ids


class TestBlockHelp:
    def test_sends_disabled_message(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.block_help.callback(admin, ctx))
        ctx.send.assert_called_once()
        assert "disabled" in ctx.send.call_args[0][0].lower()


class TestHelpme:
    def test_sends_embed(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.helpme.callback(admin, ctx))
        ctx.send.assert_called_once()
        embed = ctx.send.call_args[1].get("embed") or ctx.send.call_args[0][0]
        if isinstance(embed, discord.Embed):
            assert "Player Help" in embed.title


class TestHelpfixer:
    def test_sends_one_or_more_embeds(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.helpfixer.callback(admin, ctx))
        assert ctx.send.call_count >= 1


class TestHelpadmin:
    def test_sends_one_or_more_embeds(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.helpadmin.callback(admin, ctx))
        assert ctx.send.call_count >= 1


class TestHelpguns:
    def test_sends_embed(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.helpguns.callback(admin, ctx))
        ctx.send.assert_called_once()


class TestHelpcyberware:
    def test_sends_embed(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.helpcyberware.callback(admin, ctx))
        ctx.send.assert_called_once()


class TestConfigGroup:
    def test_shows_usage(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_group.callback(admin, ctx))
        ctx.send.assert_called_once()
        text = ctx.send.call_args[0][0]
        assert "Config commands" in text

    @patch("NightCityBot.cogs.admin._db.bot_config_get_all")
    def test_config_list_shows_values(self, mock_get_all):
        mock_get_all.return_value = [
            ("baseline_living_cost", "500", "Monthly cost of living"),
            ("attend_reward", "250", "Weekly attendance reward"),
        ]
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_list.callback(admin, ctx))
        ctx.send.assert_called_once()
        text = ctx.send.call_args[0][0]
        assert "baseline_living_cost" in text

    @patch("NightCityBot.cogs.admin._db.bot_config_get_all")
    def test_config_list_empty(self, mock_get_all):
        mock_get_all.return_value = []
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_list.callback(admin, ctx))
        ctx.send.assert_called_once()
        assert "No config" in ctx.send.call_args[0][0]

    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_get_found(self, mock_get):
        mock_get.return_value = "500"
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_get.callback(admin, ctx, "baseline_living_cost"))
        ctx.send.assert_called_once()
        assert "500" in ctx.send.call_args[0][0]

    @patch("NightCityBot.cogs.admin._cfg.get_all_defaults")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_get_fallback(self, mock_get, mock_defaults):
        mock_get.return_value = None
        mock_defaults.return_value = {"my_key": (42, "int", "desc")}
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_get.callback(admin, ctx, "my_key"))
        ctx.send.assert_called_once()
        assert "42" in ctx.send.call_args[0][0]

    @patch("NightCityBot.cogs.admin._cfg.get_all_defaults")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_get_not_found(self, mock_get, mock_defaults):
        mock_get.return_value = None
        mock_defaults.return_value = {}
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_get.callback(admin, ctx, "nonexistent"))
        ctx.send.assert_called_once()
        assert "not found" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_set_key_not_found(self, mock_get):
        mock_get.return_value = None
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_set.callback(admin, ctx, "bad_key", "100"))
        ctx.send.assert_called_once()
        assert "not found" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.admin._cfg.key_value_type")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_set_invalid_int(self, mock_get, mock_type):
        mock_get.return_value = "500"
        mock_type.return_value = "int"
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_set.callback(admin, ctx, "baseline_living_cost", "abc"))
        ctx.send.assert_called_once()
        assert "integer" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.admin._cfg.key_value_type")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_set_invalid_float(self, mock_get, mock_type):
        mock_get.return_value = "0.25"
        mock_type.return_value = "float"
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.config_set.callback(admin, ctx, "open_percent", "abc"))
        ctx.send.assert_called_once()
        assert "decimal" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.admin._cfg.reload_config")
    @patch("NightCityBot.cogs.admin._db.bot_config_set")
    @patch("NightCityBot.cogs.admin._cfg.key_value_type")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_set_success(self, mock_get, mock_type, mock_set, mock_reload):
        mock_get.return_value = "500"
        mock_type.return_value = "int"
        mock_set.return_value = True
        mock_reload.return_value = None
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "Admin"
        asyncio.run(admin.config_set.callback(admin, ctx, "baseline_living_cost", "600"))
        assert any("updated" in str(c).lower() for c in ctx.send.call_args_list)

    @patch("NightCityBot.cogs.admin._db.bot_config_set")
    @patch("NightCityBot.cogs.admin._cfg.key_value_type")
    @patch("NightCityBot.cogs.admin._db.bot_config_get")
    def test_config_set_db_failure(self, mock_get, mock_type, mock_set):
        mock_get.return_value = "500"
        mock_type.return_value = "int"
        mock_set.return_value = False
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "Admin"
        asyncio.run(admin.config_set.callback(admin, ctx, "baseline_living_cost", "600"))
        assert any("failed" in str(c).lower() for c in ctx.send.call_args_list)


class TestConfigReload:
    @patch("NightCityBot.cogs.admin._cfg.reload_config")
    def test_config_reload(self, mock_reload):
        mock_reload.return_value = None
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "Admin"
        asyncio.run(admin.config_reload.callback(admin, ctx))
        ctx.send.assert_called_once()
        assert "reloaded" in ctx.send.call_args[0][0].lower()

    @patch("NightCityBot.cogs.admin._cfg.reload_config")
    def test_reload_config_cmd(self, mock_reload):
        mock_reload.return_value = None
        admin = _make_admin()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "Admin"
        asyncio.run(admin.reload_config_cmd.callback(admin, ctx))
        ctx.send.assert_called_once()
        assert "reloaded" in ctx.send.call_args[0][0].lower()


class TestAlertReportUser:
    @patch("NightCityBot.cogs.admin.config")
    def test_no_report_user_configured(self, mock_config):
        mock_config.REPORT_USER_ID = 0
        admin = _make_admin()
        asyncio.run(admin._alert_report_user("test"))

    @patch("NightCityBot.cogs.admin.config")
    def test_sends_dm_to_report_user(self, mock_config):
        mock_config.REPORT_USER_ID = 12345
        admin = _make_admin()
        mock_user = AsyncMock()
        admin.bot.get_user = MagicMock(return_value=mock_user)
        asyncio.run(admin._alert_report_user("Alert message"))
        mock_user.send.assert_called_once_with("Alert message")

    @patch("NightCityBot.cogs.admin.config")
    def test_fetches_user_if_not_cached(self, mock_config):
        mock_config.REPORT_USER_ID = 12345
        admin = _make_admin()
        admin.bot.get_user = MagicMock(return_value=None)
        mock_user = AsyncMock()
        admin.bot.fetch_user = AsyncMock(return_value=mock_user)
        asyncio.run(admin._alert_report_user("Alert message"))
        admin.bot.fetch_user.assert_called_once_with(12345)
        mock_user.send.assert_called_once()

    @patch("NightCityBot.cogs.admin.config")
    def test_handles_fetch_failure(self, mock_config):
        mock_config.REPORT_USER_ID = 12345
        admin = _make_admin()
        admin.bot.get_user = MagicMock(return_value=None)
        admin.bot.fetch_user = AsyncMock(side_effect=Exception("not found"))
        asyncio.run(admin._alert_report_user("Alert message"))


class TestTicketDebug:
    def test_empty_index(self):
        admin = _make_admin()
        admin._ticket_index = []
        ctx = AsyncMock()
        asyncio.run(admin.ticket_debug.callback(admin, ctx, 0))
        ctx.send.assert_called_once()
        assert "empty" in ctx.send.call_args[0][0].lower()

    def test_index_out_of_range(self):
        admin = _make_admin()
        admin._ticket_index = [{"id": "1", "url": "u", "ts": "2025-01-01", "title": "T", "text": "t"}]
        ctx = AsyncMock()
        asyncio.run(admin.ticket_debug.callback(admin, ctx, 5))
        ctx.send.assert_called_once()
        assert "1 entries" in ctx.send.call_args[0][0]

    def test_shows_entry(self):
        admin = _make_admin()
        admin._ticket_index = [{"id": "1", "url": "https://example.com", "ts": "2025-01-01T00:00:00", "title": "Test Ticket", "text": "some body text"}]
        ctx = AsyncMock()
        asyncio.run(admin.ticket_debug.callback(admin, ctx, 0))
        ctx.send.assert_called_once()
        embed = ctx.send.call_args[1]["embed"]
        assert "Test Ticket" in embed.title


class TestSearchTickets:
    def test_empty_query(self):
        admin = _make_admin()
        ctx = AsyncMock()
        asyncio.run(admin.search_tickets.callback(admin, ctx, query="   "))
        ctx.send.assert_called_once()
        assert "provide" in ctx.send.call_args[0][0].lower()

    def test_empty_index(self):
        admin = _make_admin()
        admin._ticket_index = []
        ctx = AsyncMock()
        asyncio.run(admin.search_tickets.callback(admin, ctx, query="test"))
        ctx.send.assert_called_once()
        assert "empty" in ctx.send.call_args[0][0].lower()

    def test_no_matches(self):
        admin = _make_admin()
        admin._ticket_index = [{"id": "1", "url": "u", "ts": "2025-01-01", "title": "T", "text": "hello world"}]
        ctx = AsyncMock()
        asyncio.run(admin.search_tickets.callback(admin, ctx, query="zzzzz"))
        ctx.send.assert_called_once()
        assert "No tickets" in ctx.send.call_args[0][0]

    def test_finds_matches(self):
        admin = _make_admin()
        admin._ticket_index = [
            {"id": "1", "url": "https://example.com/1", "ts": "2025-01-01T00:00:00", "title": "Bug Report", "text": "player reported a bug with economy"},
            {"id": "2", "url": "https://example.com/2", "ts": "2025-01-02T00:00:00", "title": "Feature Request", "text": "player wants a new feature"},
        ]
        ctx = AsyncMock()
        asyncio.run(admin.search_tickets.callback(admin, ctx, query="bug"))
        ctx.send.assert_called()
        embed = ctx.send.call_args[1]["embed"]
        assert "1" in embed.description


class TestLoadTicketIndex:
    @patch("NightCityBot.cogs.admin._db.get_pool")
    def test_loads_from_db(self, mock_get_pool):
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[
            {"message_id": "111", "url": "https://x.com/111", "ts": "2025-01-01", "title": "Tix", "body": "body"},
        ])
        mock_get_pool.return_value = mock_pool
        admin = _make_admin()
        asyncio.run(admin._load_ticket_index())
        assert len(admin._ticket_index) == 1
        assert "111" in admin._ticket_index_ids

    @patch("NightCityBot.cogs.admin._db.get_pool")
    def test_handles_db_error(self, mock_get_pool):
        mock_get_pool.side_effect = Exception("DB connection failed")
        admin = _make_admin()
        asyncio.run(admin._load_ticket_index())
        assert admin._ticket_index == []


class TestShutdownBot:
    def test_shutdown(self):
        admin = _make_admin()
        admin.bot.get_cog = MagicMock(return_value=None)
        admin.bot.close = AsyncMock()
        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "Admin"
        asyncio.run(admin.shutdown_bot.callback(admin, ctx))
        ctx.send.assert_called_once()
        admin.bot.close.assert_called_once()
