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


class TestDMHandlerInit:
    def test_init_sets_up_state(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        bot.loop.create_task = MagicMock()
        handler = DMHandler(bot)
        assert handler.dm_threads == {}
        assert not handler.load_event.is_set()


class TestOnMessageFiltering:
    def _make_handler(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        bot.loop.create_task = MagicMock()
        handler = DMHandler(bot)
        return handler

    def test_ignores_own_messages(self):
        handler = self._make_handler()
        msg = MagicMock()
        msg.author = handler.bot.user
        msg.author.bot = True
        asyncio.run(handler.on_message(msg))

    def test_ignores_other_bots(self):
        handler = self._make_handler()
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = True
        msg.author.__eq__ = lambda self, other: False
        asyncio.run(handler.on_message(msg))

    def test_ignores_when_dm_system_disabled(self):
        handler = self._make_handler()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        handler.bot.get_cog = MagicMock(return_value=control)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False

        asyncio.run(handler.on_message(msg))
        handler.bot.get_cog.assert_called_with("SystemControl")


class TestHandleDmMessage:
    def _make_handler_and_thread(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        bot.loop.create_task = MagicMock()
        handler = DMHandler(bot)

        control = MagicMock()
        control.is_enabled = MagicMock(return_value=True)
        bot.get_cog = MagicMock(return_value=control)

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


class TestGetOrCreateThread:
    def test_returns_cached_thread(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        bot.loop.create_task = MagicMock()
        handler = DMHandler(bot)
        handler.load_event.set()

        mock_thread = MagicMock(spec=discord.Thread)
        bot.fetch_channel = AsyncMock(return_value=mock_thread)
        bot.get_channel = MagicMock()
        handler.dm_threads = {"123": 456}

        user = MagicMock()
        user.id = 123
        user.name = "testuser"

        result = asyncio.run(handler.get_or_create_dm_thread(user))

        assert result == mock_thread
        bot.fetch_channel.assert_called_once_with(456)
