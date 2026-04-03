import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from NightCityBot.cogs.economy import Economy


def _make_bot():
    bot = MagicMock()
    bot.unbelievaboat = MagicMock()
    return bot


def _make_economy():
    with patch("NightCityBot.services.unbelievaboat.aiohttp.ClientSession", new=MagicMock()):
        return Economy(_make_bot())


class TestRestoreEventState:
    def test_restore_active_event(self):
        econ = _make_economy()
        now = datetime.now(ZoneInfo("US/Eastern"))
        started = now - timedelta(hours=1)
        expires = now + timedelta(hours=3)
        data = {"started_at": started.isoformat(), "expires_at": expires.isoformat()}

        with patch("NightCityBot.cogs.economy.db_load", new_callable=AsyncMock, return_value=data):
            with patch("NightCityBot.cogs.economy.helpers.get_tz_now", return_value=now):
                asyncio.run(econ._restore_event_state())

        assert econ.event_started_at == started
        assert econ.event_expires_at == expires

    def test_restore_expired_event_clears(self):
        econ = _make_economy()
        now = datetime.now(ZoneInfo("US/Eastern"))
        started = now - timedelta(hours=5)
        expires = now - timedelta(hours=1)
        data = {"started_at": started.isoformat(), "expires_at": expires.isoformat()}

        with patch("NightCityBot.cogs.economy.db_load", new_callable=AsyncMock, return_value=data) as mock_load:
            with patch("NightCityBot.cogs.economy.db_save", new_callable=AsyncMock) as mock_save:
                with patch("NightCityBot.cogs.economy.helpers.get_tz_now", return_value=now):
                    asyncio.run(econ._restore_event_state())

        assert econ.event_started_at is None
        assert econ.event_expires_at is None
        mock_save.assert_called_once_with("fixer_event", None)

    def test_restore_no_data_no_op(self):
        econ = _make_economy()
        with patch("NightCityBot.cogs.economy.db_load", new_callable=AsyncMock, return_value=None):
            asyncio.run(econ._restore_event_state())
        assert econ.event_started_at is None
        assert econ.event_expires_at is None

    def test_restore_handles_exception(self):
        econ = _make_economy()
        with patch("NightCityBot.cogs.economy.db_load", new_callable=AsyncMock, side_effect=Exception("DB down")):
            asyncio.run(econ._restore_event_state())
        assert econ.event_started_at is None
        assert econ.event_expires_at is None


class TestCogLoadCallsRestore:
    def test_cog_load_invokes_restore(self):
        econ = _make_economy()
        econ.auto_rent_loop = MagicMock()
        econ._cleanup_loop = MagicMock()
        econ._restore_event_state = AsyncMock()
        asyncio.run(econ.cog_load())
        econ._restore_event_state.assert_awaited_once()


class TestEventStartSavesToDB:
    def test_event_start_persists(self):
        import config
        econ = _make_economy()
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.channel.id = config.ATTENDANCE_CHANNEL_ID
        ctx.send = AsyncMock()
        now = datetime.now(ZoneInfo("US/Eastern"))

        with patch("NightCityBot.cogs.economy.helpers.get_tz_now", return_value=now):
            with patch("NightCityBot.cogs.economy.db_save", new_callable=AsyncMock) as mock_save:
                asyncio.run(econ.event_start.callback(econ, ctx))

        assert econ.event_started_at == now
        assert econ.event_expires_at == now + timedelta(hours=4)
        mock_save.assert_called_once_with("fixer_event", {
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        })
