import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from NightCityBot.utils.interaction_safety import (
    SafeModal,
    SafeView,
    _safe_respond,
    _USER_ERROR_MSG,
    modal_on_error,
    view_on_error,
)


def _run(coro):
    return asyncio.run(coro)


def _make_interaction(response_done=False):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock()
    inter.user.id = 12345
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=response_done)
    inter.response.send_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


class TestSafeRespond:
    def test_sends_message_when_not_done(self):
        inter = _make_interaction(response_done=False)
        _run(_safe_respond(inter, "test"))
        inter.response.send_message.assert_called_once_with("test", ephemeral=True)

    def test_sends_followup_when_done(self):
        inter = _make_interaction(response_done=True)
        _run(_safe_respond(inter, "test"))
        inter.followup.send.assert_called_once_with("test", ephemeral=True)

    def test_swallows_exception(self):
        inter = _make_interaction(response_done=False)
        inter.response.send_message = AsyncMock(side_effect=RuntimeError("fail"))
        _run(_safe_respond(inter, "test"))


class TestViewOnError:
    def test_logs_and_responds(self):
        inter = _make_interaction(response_done=False)
        view = MagicMock(spec=discord.ui.View)
        type(view).__name__ = "TestView"
        item = MagicMock()
        item.label = "Buy"
        err = ValueError("oops")
        with patch("NightCityBot.utils.interaction_safety.logger") as mock_log:
            _run(view_on_error(view, inter, err, item))
            mock_log.error.assert_called_once()
        inter.response.send_message.assert_called_once_with(_USER_ERROR_MSG, ephemeral=True)

    def test_item_without_label(self):
        inter = _make_interaction(response_done=False)
        view = MagicMock(spec=discord.ui.View)
        type(view).__name__ = "TestView"
        item = MagicMock(spec=[])
        err = RuntimeError("err")
        with patch("NightCityBot.utils.interaction_safety.logger"):
            _run(view_on_error(view, inter, err, item))
        inter.response.send_message.assert_called_once()


class TestModalOnError:
    def test_logs_and_responds(self):
        inter = _make_interaction(response_done=False)
        modal = MagicMock(spec=discord.ui.Modal)
        type(modal).__name__ = "TestModal"
        err = ValueError("oops")
        with patch("NightCityBot.utils.interaction_safety.logger") as mock_log:
            _run(modal_on_error(modal, inter, err))
            mock_log.error.assert_called_once()
        inter.response.send_message.assert_called_once_with(_USER_ERROR_MSG, ephemeral=True)


class TestSafeView:
    def test_on_error_delegates(self):
        async def _test():
            view = SafeView()
            inter = _make_interaction(response_done=False)
            item = MagicMock()
            item.label = "btn"
            err = RuntimeError("err")
            with patch("NightCityBot.utils.interaction_safety.logger"):
                await view.on_error(inter, err, item)
            inter.response.send_message.assert_called_once_with(_USER_ERROR_MSG, ephemeral=True)
        _run(_test())


class TestSafeModal:
    def test_on_error_delegates(self):
        async def _test():
            modal = SafeModal(title="Test")
            inter = _make_interaction(response_done=False)
            err = RuntimeError("err")
            with patch("NightCityBot.utils.interaction_safety.logger"):
                await modal.on_error(inter, err)
            inter.response.send_message.assert_called_once_with(_USER_ERROR_MSG, ephemeral=True)
        _run(_test())
