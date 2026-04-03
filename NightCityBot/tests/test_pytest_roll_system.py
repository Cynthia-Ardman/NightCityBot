import asyncio
import re
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from NightCityBot.cogs.roll_system import RollSystem, MAX_DICE, MAX_SIDES, MAX_MOD


def _make_bot():
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    return bot


def _make_channel():
    ch = AsyncMock()
    ch.send = AsyncMock()
    return ch


def _make_author(name="TestUser"):
    a = MagicMock()
    a.display_name = name
    a.id = 12345
    return a


class TestLoggableRoll:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.bot = _make_bot()
        self.cog = RollSystem(self.bot)
        self.channel = _make_channel()
        self.author = _make_author()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_roll_1d6(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "1d6", skip_log=True))
        self.channel.send.assert_called_once()
        text = self.channel.send.call_args[0][0]
        assert "TestUser rolled" in text
        assert "1d6" in text
        assert "Total:" in text

    def test_valid_roll_with_modifier(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "2d8+5", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "2d8" in text
        assert "+5" in text

    def test_valid_roll_negative_modifier(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "1d20-3", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "1d20" in text

    def test_shorthand_single_die(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "20", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "1d20" in text

    def test_invalid_format_rejected(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "abc", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "Invalid format" in text

    def test_too_many_dice_rejected(self):
        dice = f"{MAX_DICE + 1}d6"
        self._run(self.cog.loggable_roll(self.author, self.channel, dice, skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "Too many dice" in text

    def test_too_many_sides_rejected(self):
        dice = f"1d{MAX_SIDES + 1}"
        self._run(self.cog.loggable_roll(self.author, self.channel, dice, skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "Too many sides" in text

    def test_modifier_too_large_rejected(self):
        dice = f"1d6+{MAX_MOD + 1}"
        self._run(self.cog.loggable_roll(self.author, self.channel, dice, skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "Modifier too large" in text

    def test_roll_total_matches_results(self):
        with patch("random.randint", return_value=4):
            self._run(self.cog.loggable_roll(self.author, self.channel, "3d6+2", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "4, 4, 4" in text
        assert "**Total:** 14" in text

    def test_spaces_in_dice_string_tolerated(self):
        self._run(self.cog.loggable_roll(self.author, self.channel, "2 d 6 + 3", skip_log=True))
        text = self.channel.send.call_args[0][0]
        assert "Total:" in text
