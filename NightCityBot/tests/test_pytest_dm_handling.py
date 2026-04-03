import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.dm_handling import _relay_description, DMHandler


def _make_message(content="", attachments=None):
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.attachments = attachments or []
    return msg


def _make_attachment(filename="photo.png"):
    att = MagicMock()
    att.filename = filename
    return att


class TestRelayDescription:
    def test_text_content(self):
        msg = _make_message(content="Hello there")
        assert _relay_description(msg) == "Hello there"

    def test_single_attachment_no_text(self):
        msg = _make_message(content="", attachments=[_make_attachment("img.png")])
        assert _relay_description(msg) == "img.png"

    def test_multiple_attachments_no_text(self):
        msg = _make_message(
            content="",
            attachments=[_make_attachment("a.png"), _make_attachment("b.png")],
        )
        assert _relay_description(msg) == "attachment"

    def test_empty_message(self):
        msg = _make_message(content="", attachments=[])
        assert _relay_description(msg) == ""

    def test_whitespace_only_content_with_attachment(self):
        msg = _make_message(content="   ", attachments=[_make_attachment("f.jpg")])
        assert _relay_description(msg) == "f.jpg"


def _make_handler():
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.loop.create_task = MagicMock(side_effect=lambda coro: coro.close())
    handler = DMHandler(bot)
    return handler


class TestDMHandlerInit:
    def test_init_sets_up_state(self):
        handler = _make_handler()
        assert handler.dm_threads == {}
        assert not handler.load_event.is_set()


class TestOnMessageFiltering:
    def test_ignores_own_messages(self):
        handler = _make_handler()
        msg = MagicMock()
        msg.author = handler.bot.user
        msg.author.bot = True
        asyncio.run(handler.on_message(msg))

    def test_ignores_other_bots(self):
        handler = _make_handler()
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = True
        msg.author.__eq__ = lambda self, other: False
        asyncio.run(handler.on_message(msg))

    def test_ignores_when_dm_system_disabled(self):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        handler.bot.get_cog = MagicMock(return_value=control)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False

        asyncio.run(handler.on_message(msg))
        handler.bot.get_cog.assert_called_with("SystemControl")

    def test_routes_dm_channel(self):
        handler = _make_handler()
        handler.handle_dm_message = AsyncMock()
        handler.handle_thread_message = AsyncMock()

        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        handler.bot.get_cog = MagicMock(return_value=control)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.DMChannel)

        asyncio.run(handler.on_message(msg))
        handler.handle_dm_message.assert_called_once_with(msg)
        handler.handle_thread_message.assert_not_called()

    def test_routes_thread_channel(self):
        handler = _make_handler()
        handler.handle_dm_message = AsyncMock()
        handler.handle_thread_message = AsyncMock()

        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        handler.bot.get_cog = MagicMock(return_value=control)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False
        msg.channel = MagicMock(spec=discord.Thread)

        asyncio.run(handler.on_message(msg))
        handler.handle_thread_message.assert_called_once_with(msg)
        handler.handle_dm_message.assert_not_called()


class TestHandleDmMessage:
    def _make_handler_and_thread(self):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        handler.bot.get_cog = MagicMock(return_value=control)

        mock_thread = AsyncMock()
        handler.get_or_create_dm_thread = AsyncMock(return_value=mock_thread)
        return handler, mock_thread

    def test_skips_command_chunks(self):
        handler, mock_thread = self._make_handler_and_thread()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "TestUser"
        msg.author.id = 999
        msg.content = "!roll 2d6"
        msg.attachments = []

        asyncio.run(handler.handle_dm_message(msg))
        mock_thread.send.assert_not_called()

    def test_forwards_normal_message(self):
        handler, mock_thread = self._make_handler_and_thread()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "TestUser"
        msg.author.id = 999
        msg.content = "Hello, I need help"
        msg.attachments = []

        asyncio.run(handler.handle_dm_message(msg))
        mock_thread.send.assert_called_once()
        assert "Hello, I need help" in str(mock_thread.send.call_args)

    def test_forwards_attachments(self):
        handler, mock_thread = self._make_handler_and_thread()

        att = MagicMock()
        att.url = "https://cdn.discord.com/att/file.png"

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "TestUser"
        msg.author.id = 999
        msg.content = ""
        msg.attachments = [att]

        asyncio.run(handler.handle_dm_message(msg))
        assert mock_thread.send.call_count == 2

    def test_handles_exception(self):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        handler.bot.get_cog = MagicMock(return_value=control)
        handler.get_or_create_dm_thread = AsyncMock(side_effect=Exception("fail"))

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "TestUser"
        msg.author.id = 999
        msg.content = "Hello"
        msg.attachments = []

        asyncio.run(handler.handle_dm_message(msg))

    def test_dm_disabled_returns_early(self):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        handler.bot.get_cog = MagicMock(return_value=control)
        handler.get_or_create_dm_thread = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.content = "Hello"
        msg.attachments = []

        asyncio.run(handler.handle_dm_message(msg))
        handler.get_or_create_dm_thread.assert_not_called()

    def test_no_text_sends_placeholder(self):
        handler, mock_thread = self._make_handler_and_thread()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.display_name = "TestUser"
        msg.author.id = 999
        msg.content = ""
        msg.attachments = []

        asyncio.run(handler.handle_dm_message(msg))
        mock_thread.send.assert_called_once()
        assert "No text content" in str(mock_thread.send.call_args)


class TestHandleThreadMessage:
    def _setup_handler(self):
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {"12345": 999}
        return handler

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_ignores_wrong_parent(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        handler = self._setup_handler()

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 777

        asyncio.run(handler.handle_thread_message(msg))

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_ignores_unknown_thread_no_id_in_name(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {}

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 9999
        msg.channel.name = "no-user-id-here"
        msg.author = MagicMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        asyncio.run(handler.handle_thread_message(msg))

    @patch("NightCityBot.cogs.dm_handling.dm_thread_set")
    @patch("NightCityBot.cogs.dm_handling.config")
    def test_finds_user_from_thread_name(self, mock_config, mock_thread_set):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        mock_thread_set.return_value = None
        handler = self._setup_handler()
        handler.dm_threads = {}

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 9999
        msg.channel.name = "user-12345"
        msg.content = "Hello user"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)

        asyncio.run(handler.handle_thread_message(msg))
        handler.bot.fetch_user.assert_called_once_with(12345)
        assert "12345" in handler.dm_threads

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_ignores_non_fixer(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.author = MagicMock()
        role = MagicMock()
        role.id = 999
        msg.author.roles = [role]

        asyncio.run(handler.handle_thread_message(msg))

    @patch("NightCityBot.cogs.dm_handling.dm_thread_delete")
    @patch("NightCityBot.cogs.dm_handling.config")
    def test_handles_unknown_user(self, mock_config, mock_delete):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        mock_delete.return_value = None
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = "Hello"
        msg.author = MagicMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        handler.bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))

        asyncio.run(handler.handle_thread_message(msg))
        assert "12345" not in handler.dm_threads

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_roll_command_relay(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        roll_cog = AsyncMock()
        roll_cog.roll = AsyncMock()
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "RollSystem":
                return roll_cog
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = "!roll 2d6"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        msg.delete = AsyncMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        mock_target.create_dm = AsyncMock(return_value=AsyncMock())
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)
        handler.bot.get_context = AsyncMock(return_value=AsyncMock())

        asyncio.run(handler.handle_thread_message(msg))
        roll_cog.roll.assert_called_once()
        msg.delete.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_start_rp_relay(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        rp_cog = AsyncMock()
        rp_cog.start_rp = AsyncMock()
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "RPManager":
                return rp_cog
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = "!start-rp"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        msg.delete = AsyncMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)
        handler.bot.get_context = AsyncMock(return_value=AsyncMock())

        asyncio.run(handler.handle_thread_message(msg))
        rp_cog.start_rp.assert_called_once()
        msg.delete.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_normal_relay_sends_message(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = "Hey there, how's it going?"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        msg.delete = AsyncMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        mock_target.send = AsyncMock()
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)

        asyncio.run(handler.handle_thread_message(msg))
        mock_target.send.assert_called_once()
        msg.channel.send.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_large_attachment_warning(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        handler.bot.get_cog = MagicMock(return_value=None)

        att = MagicMock()
        att.size = 10 * 1024 * 1024
        att.filename = "huge.zip"

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = ""
        msg.attachments = [att]
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        msg.delete = AsyncMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        mock_target.send = AsyncMock()
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)

        asyncio.run(handler.handle_thread_message(msg))
        assert any("too large" in str(c) for c in msg.channel.send.call_args_list)

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_generic_command_relay(self, mock_config):
        mock_config.DM_INBOX_CHANNEL_ID = 888
        mock_config.FIXER_ROLE_ID = 555
        handler = self._setup_handler()
        handler.dm_threads = {"12345": 999}

        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)
        handler.bot.get_context = AsyncMock(return_value=AsyncMock())
        handler.bot.invoke = AsyncMock()

        msg = MagicMock()
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.parent_id = 888
        msg.channel.id = 999
        msg.channel.name = "user-12345"
        msg.content = "!some_command arg"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.display_name = "Fixer"
        msg.author.id = 777
        msg.delete = AsyncMock()
        role = MagicMock()
        role.id = 555
        msg.author.roles = [role]

        mock_target = AsyncMock()
        mock_target.display_name = "TargetUser"
        mock_target.id = 12345
        handler.bot.fetch_user = AsyncMock(return_value=mock_target)

        asyncio.run(handler.handle_thread_message(msg))
        handler.bot.invoke.assert_called_once()
        msg.delete.assert_called_once()


class TestGetOrCreateThread:
    def test_returns_cached_thread(self):
        handler = _make_handler()
        handler.load_event.set()

        mock_thread = MagicMock(spec=discord.Thread)
        handler.bot.fetch_channel = AsyncMock(return_value=mock_thread)
        handler.bot.get_channel = MagicMock()
        handler.dm_threads = {"123": 456}

        user = MagicMock()
        user.id = 123
        user.name = "testuser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))

        assert result == mock_thread
        handler.bot.fetch_channel.assert_called_once_with(456)

    @patch("NightCityBot.cogs.dm_handling.dm_thread_set")
    def test_finds_thread_from_channel_threads(self, mock_set):
        mock_set.return_value = None
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {}

        existing_thread = MagicMock(spec=discord.Thread)
        existing_thread.name = "testuser-123"
        existing_thread.id = 789

        log_channel = MagicMock(spec=discord.TextChannel)
        log_channel.threads = [existing_thread]

        handler.bot.get_channel = MagicMock(return_value=log_channel)

        user = MagicMock()
        user.id = 123
        user.name = "testuser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))
        assert result == existing_thread
        assert handler.dm_threads["123"] == 789

    @patch("NightCityBot.cogs.dm_handling.dm_thread_set")
    def test_creates_new_thread_in_text_channel(self, mock_set):
        mock_set.return_value = None
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {}

        new_thread = MagicMock(spec=discord.Thread)
        new_thread.id = 1111

        log_channel = MagicMock(spec=discord.TextChannel)
        log_channel.threads = []
        log_channel.create_thread = AsyncMock(return_value=new_thread)

        handler.bot.get_channel = MagicMock(return_value=log_channel)

        user = MagicMock()
        user.id = 123
        user.name = "testuser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))
        assert result == new_thread
        log_channel.create_thread.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.dm_thread_set")
    def test_creates_new_thread_in_forum_channel(self, mock_set):
        mock_set.return_value = None
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {}

        new_thread = MagicMock(spec=discord.Thread)
        new_thread.id = 2222
        created = MagicMock()
        created.thread = new_thread

        log_channel = MagicMock(spec=discord.ForumChannel)
        log_channel.threads = []
        log_channel.create_thread = AsyncMock(return_value=created)

        handler.bot.get_channel = MagicMock(return_value=log_channel)

        user = MagicMock()
        user.id = 456
        user.name = "otheruser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))
        assert result == new_thread

    def test_raises_for_unsupported_channel_type(self):
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {}

        log_channel = MagicMock(spec=discord.VoiceChannel)
        log_channel.threads = []
        handler.bot.get_channel = MagicMock(return_value=log_channel)

        user = MagicMock()
        user.id = 789
        user.name = "voiceuser"

        with pytest.raises(RuntimeError, match="TextChannel or ForumChannel"):
            asyncio.run(handler.get_or_create_dm_thread(user))

    @patch("NightCityBot.cogs.dm_handling.dm_thread_set")
    def test_recreates_deleted_cached_thread(self, mock_set):
        mock_set.return_value = None
        handler = _make_handler()
        handler.load_event.set()
        handler.dm_threads = {"123": 456}

        handler.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))

        new_thread = MagicMock(spec=discord.Thread)
        new_thread.id = 789

        log_channel = MagicMock(spec=discord.TextChannel)
        log_channel.threads = []
        log_channel.create_thread = AsyncMock(return_value=new_thread)
        handler.bot.get_channel = MagicMock(return_value=log_channel)

        user = MagicMock()
        user.id = 123
        user.name = "testuser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))
        assert result == new_thread


class TestDmCommand:
    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_disabled(self, mock_config):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = 1
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user hello"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()
        user = MagicMock(spec=discord.User)

        asyncio.run(handler.dm.callback(handler, ctx, user, message="hello"))
        ctx.send.assert_called()
        assert "disabled" in ctx.send.call_args_list[0][0][0].lower()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_sends_message_successfully(self, mock_config):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        mock_thread = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)
        handler.get_or_create_dm_thread = AsyncMock(return_value=mock_thread)

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.display_name = "Fixer"
        ctx.author.id = 1
        ctx.guild = MagicMock()
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user hello there"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()

        user = AsyncMock(spec=discord.User)
        user.display_name = "Player"
        user.id = 42
        user.send = AsyncMock()

        asyncio.run(handler.dm.callback(handler, ctx, user, message="hello there"))
        user.send.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_forbidden(self, mock_config):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)
        handler.get_or_create_dm_thread = AsyncMock(return_value=AsyncMock())

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.display_name = "Fixer"
        ctx.author.id = 1
        ctx.guild = MagicMock()
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user hello"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()

        user = AsyncMock(spec=discord.User)
        user.display_name = "Player"
        user.id = 42
        user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "cant dm"))

        asyncio.run(handler.dm.callback(handler, ctx, user, message="hello"))
        assert any("Privacy" in str(c) for c in ctx.send.call_args_list)

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_with_roll_command(self, mock_config):
        mock_config.FIXER_ROLE_ID = 555
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        roll_cog = AsyncMock()
        roll_cog.roll = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            if name == "RollSystem":
                return roll_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)
        handler.get_or_create_dm_thread = AsyncMock(return_value=AsyncMock())
        handler.bot.get_context = AsyncMock(return_value=AsyncMock())

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.display_name = "Fixer"
        ctx.author.id = 1
        ctx.guild = MagicMock()
        ctx.guild.get_member = MagicMock(return_value=None)
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user !roll 2d6"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()

        user = AsyncMock(spec=discord.User)
        user.display_name = "Player"
        user.id = 42
        user.create_dm = AsyncMock(return_value=AsyncMock())

        asyncio.run(handler.dm.callback(handler, ctx, user, message="!roll 2d6"))
        roll_cog.roll.assert_called_once()

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_with_invalid_roll(self, mock_config):
        mock_config.FIXER_ROLE_ID = 555
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        roll_cog = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            if name == "RollSystem":
                return roll_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.display_name = "Fixer"
        ctx.author.id = 1
        ctx.guild = MagicMock()
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user !roll bad_dice"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()

        user = AsyncMock(spec=discord.User)
        user.display_name = "Player"
        user.id = 42

        asyncio.run(handler.dm.callback(handler, ctx, user, message="!roll bad_dice"))
        assert any("Format" in str(c) for c in ctx.send.call_args_list)

    @patch("NightCityBot.cogs.dm_handling.config")
    def test_dm_no_message_no_attachments(self, mock_config):
        handler = _make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        admin_cog = AsyncMock()
        admin_cog.log_audit = AsyncMock()
        mock_thread = AsyncMock()

        def get_cog_side_effect(name):
            if name == "SystemControl":
                return control
            if name == "Admin":
                return admin_cog
            return None

        handler.bot.get_cog = MagicMock(side_effect=get_cog_side_effect)
        handler.get_or_create_dm_thread = AsyncMock(return_value=mock_thread)

        ctx = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.display_name = "Fixer"
        ctx.author.id = 1
        ctx.guild = MagicMock()
        ctx.message = MagicMock()
        ctx.message.content = "!dm @user"
        ctx.message.attachments = []
        ctx.message.delete = AsyncMock()

        user = AsyncMock(spec=discord.User)
        user.display_name = "Player"
        user.id = 42
        user.send = AsyncMock()

        asyncio.run(handler.dm.callback(handler, ctx, user, message=None))
        user.send.assert_called_once()
        assert "No text" in str(user.send.call_args)
