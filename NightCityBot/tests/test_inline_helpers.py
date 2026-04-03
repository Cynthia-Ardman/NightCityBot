import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from NightCityBot.utils.inline_helpers import (
    QtySelectView,
    PriceSelectView,
    collect_text_input,
)


def _run(coro):
    return asyncio.run(coro)


def _make_bot_with_response(content, *, raise_timeout=False):
    bot = MagicMock()
    if raise_timeout:
        bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)
    else:
        msg = MagicMock()
        msg.content = content
        msg.delete = AsyncMock()
        bot.wait_for = AsyncMock(return_value=msg)
    return bot


class TestCollectTextInput:
    def test_returns_text(self):
        bot = _make_bot_with_response("hello world")
        result = _run(collect_text_input(bot, 100, 200))
        assert result == "hello world"

    def test_strips_whitespace(self):
        bot = _make_bot_with_response("  padded  ")
        result = _run(collect_text_input(bot, 100, 200))
        assert result == "padded"

    def test_returns_none_on_timeout(self):
        bot = _make_bot_with_response("", raise_timeout=True)
        result = _run(collect_text_input(bot, 100, 200))
        assert result is None

    def test_returns_none_on_cancel(self):
        bot = _make_bot_with_response("cancel")
        result = _run(collect_text_input(bot, 100, 200))
        assert result is None

    def test_cancel_case_insensitive(self):
        bot = _make_bot_with_response("CANCEL")
        result = _run(collect_text_input(bot, 100, 200))
        assert result is None

    def test_delete_failure_swallowed(self):
        bot = MagicMock()
        msg = MagicMock()
        msg.content = "test"
        msg.delete = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        bot.wait_for = AsyncMock(return_value=msg)
        result = _run(collect_text_input(bot, 100, 200))
        assert result == "test"

    def test_check_function_filters(self):
        captured = {}

        async def _test():
            bot = MagicMock()

            async def _fake_wait_for(event, check, timeout):
                captured["fn"] = check
                msg = MagicMock()
                msg.content = "ok"
                msg.delete = AsyncMock()
                return msg

            bot.wait_for = _fake_wait_for
            await collect_text_input(bot, 100, 200)

        _run(_test())
        check = captured["fn"]

        right_msg = MagicMock()
        right_msg.author.id = 200
        right_msg.channel.id = 100
        assert check(right_msg) is True

        wrong_author = MagicMock()
        wrong_author.author.id = 999
        wrong_author.channel.id = 100
        assert check(wrong_author) is False

        wrong_channel = MagicMock()
        wrong_channel.author.id = 200
        wrong_channel.channel.id = 999
        assert check(wrong_channel) is False


class TestQtySelectView:
    def test_init_default_max(self):
        async def _test():
            view = QtySelectView(author_id=1)
            assert view.author_id == 1
            assert view.result is None
            items = [c for c in view.children if isinstance(c, discord.ui.Select)]
            assert len(items) == 1
            assert len(items[0].options) == 10
        _run(_test())

    def test_init_cap_at_25(self):
        async def _test():
            view = QtySelectView(author_id=1, max_qty=50)
            items = [c for c in view.children if isinstance(c, discord.ui.Select)]
            assert len(items[0].options) == 25
        _run(_test())

    def test_init_min_1(self):
        async def _test():
            view = QtySelectView(author_id=1, max_qty=0)
            items = [c for c in view.children if isinstance(c, discord.ui.Select)]
            assert len(items[0].options) == 1
        _run(_test())

    def test_interaction_check_correct_user(self):
        async def _test():
            view = QtySelectView(author_id=42)
            inter = MagicMock()
            inter.user.id = 42
            assert await view.interaction_check(inter) is True
        _run(_test())

    def test_interaction_check_wrong_user(self):
        async def _test():
            view = QtySelectView(author_id=42)
            inter = MagicMock()
            inter.user.id = 99
            assert await view.interaction_check(inter) is False
        _run(_test())

    def test_on_select(self):
        async def _test():
            view = QtySelectView(author_id=1)
            inter = MagicMock()
            inter.data = {"values": ["5"]}
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            await view._on_select(inter)
            assert view.result == 5
            inter.response.edit_message.assert_called_once()
            assert view.is_finished()
        _run(_test())


class TestPriceSelectView:
    def test_init_with_zero(self):
        async def _test():
            view = PriceSelectView(author_id=1, bot=MagicMock(), channel_id=100, allow_zero=True)
            items = [c for c in view.children if isinstance(c, discord.ui.Select)]
            labels = [o.label for o in items[0].options]
            assert "Free ($0)" in labels
            assert "Custom amount\u2026" in labels
        _run(_test())

    def test_init_without_zero(self):
        async def _test():
            view = PriceSelectView(author_id=1, bot=MagicMock(), channel_id=100, allow_zero=False)
            items = [c for c in view.children if isinstance(c, discord.ui.Select)]
            labels = [o.label for o in items[0].options]
            assert "Free ($0)" not in labels
        _run(_test())

    def test_select_preset_price(self):
        async def _test():
            view = PriceSelectView(author_id=1, bot=MagicMock(), channel_id=100)
            inter = MagicMock()
            inter.data = {"values": ["5000"]}
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            await view._on_select(inter)
            assert view.result == 5000
            assert view.is_finished()
        _run(_test())

    def test_select_custom_valid(self):
        async def _test():
            bot = _make_bot_with_response("7500")
            view = PriceSelectView(author_id=1, bot=bot, channel_id=100)
            inter = MagicMock()
            inter.data = {"values": ["custom"]}
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            inter.followup = MagicMock()
            inter.followup.send = AsyncMock()
            await view._on_select(inter)
            assert view.result == 7500
            assert view.is_finished()
        _run(_test())

    def test_select_custom_with_formatting(self):
        async def _test():
            bot = _make_bot_with_response("$12,000")
            view = PriceSelectView(author_id=1, bot=bot, channel_id=100)
            inter = MagicMock()
            inter.data = {"values": ["custom"]}
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            inter.followup = MagicMock()
            inter.followup.send = AsyncMock()
            await view._on_select(inter)
            assert view.result == 12000
        _run(_test())

    def test_select_custom_invalid(self):
        async def _test():
            bot = _make_bot_with_response("abc")
            view = PriceSelectView(author_id=1, bot=bot, channel_id=100)
            inter = MagicMock()
            inter.data = {"values": ["custom"]}
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            inter.followup = MagicMock()
            inter.followup.send = AsyncMock()
            await view._on_select(inter)
            assert view.result is None
            inter.followup.send.assert_called_once()
        _run(_test())

    def test_select_custom_timeout(self):
        async def _test():
            bot = _make_bot_with_response("", raise_timeout=True)
            view = PriceSelectView(author_id=1, bot=bot, channel_id=100)
            inter = MagicMock()
            inter.data = {"values": ["custom"]}
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            await view._on_select(inter)
            assert view.result is None
        _run(_test())

    def test_interaction_check(self):
        async def _test():
            view = PriceSelectView(author_id=42, bot=MagicMock(), channel_id=100)
            inter = MagicMock()
            inter.user.id = 42
            assert await view.interaction_check(inter) is True
            inter.user.id = 99
            assert await view.interaction_check(inter) is False
        _run(_test())
