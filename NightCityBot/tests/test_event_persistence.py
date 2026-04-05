import asyncio
from datetime import datetime, timedelta
from pathlib import Path
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

        with patch("NightCityBot.cogs.economy.fixer_event_load", new_callable=AsyncMock, return_value=data):
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

        with patch("NightCityBot.cogs.economy.fixer_event_load", new_callable=AsyncMock, return_value=data):
            with patch("NightCityBot.cogs.economy.fixer_event_save", new_callable=AsyncMock) as mock_save:
                with patch("NightCityBot.cogs.economy.helpers.get_tz_now", return_value=now):
                    asyncio.run(econ._restore_event_state())

        assert econ.event_started_at is None
        assert econ.event_expires_at is None
        mock_save.assert_called_once_with(None)

    def test_restore_no_data_no_op(self):
        econ = _make_economy()
        with patch("NightCityBot.cogs.economy.fixer_event_load", new_callable=AsyncMock, return_value=None):
            asyncio.run(econ._restore_event_state())
        assert econ.event_started_at is None
        assert econ.event_expires_at is None

    def test_restore_handles_exception(self):
        econ = _make_economy()
        with patch("NightCityBot.cogs.economy.fixer_event_load", new_callable=AsyncMock, side_effect=Exception("DB down")):
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
            with patch("NightCityBot.cogs.economy.fixer_event_save", new_callable=AsyncMock) as mock_save:
                asyncio.run(econ.event_start.callback(econ, ctx))

        assert econ.event_started_at == now
        assert econ.event_expires_at == now + timedelta(hours=4)
        mock_save.assert_called_once_with({
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        })


class TestEventStartDuplicateGuard:
    def test_event_start_rejects_when_already_active(self):
        import config
        econ = _make_economy()
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.channel.id = config.ATTENDANCE_CHANNEL_ID
        ctx.send = AsyncMock()
        now = datetime.now(ZoneInfo("US/Eastern"))
        econ.event_started_at = now - timedelta(hours=1)
        econ.event_expires_at = now + timedelta(hours=3)

        with patch("NightCityBot.cogs.economy.helpers.get_tz_now", return_value=now):
            asyncio.run(econ.event_start.callback(econ, ctx))

        msg = ctx.send.call_args[0][0]
        assert "already running" in msg.lower()


class TestCWStateDBPersistence:

    def _make_cw_cog(self, tmp_path):
        from NightCityBot.cogs.cyberware_shop import CyberwareShop
        bot = _make_bot()
        with patch("NightCityBot.cogs.cyberware_shop.config") as mock_cfg:
            mock_cfg.CYBERWARE_SHOP_DATA_DIR = str(tmp_path)
            mock_cfg.BASE_DIR = str(tmp_path)
            mock_cfg.CYBERWARE_SHOP_SHEET_URL = ""
            cog = CyberwareShop(bot)
        return cog

    def test_load_state_from_db(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        db_state = {"sheet_url": "https://example.com/sheet", "items_count": 5}
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_load", new_callable=AsyncMock, return_value=db_state):
            result = asyncio.run(cog._load_state())
        assert result["sheet_url"] == "https://example.com/sheet"

    def test_load_state_falls_back_to_file(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        file_state = {"sheet_url": "https://file-url.com/sheet", "items_count": 3}
        import json
        cog.state_file.write_text(json.dumps(file_state))
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_load", new_callable=AsyncMock, return_value={}):
            with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_save", new_callable=AsyncMock) as mock_save:
                result = asyncio.run(cog._load_state())
        assert result["sheet_url"] == "https://file-url.com/sheet"
        mock_save.assert_called_once()

    def test_save_state_writes_to_db_and_file(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        state = {"sheet_url": "https://new-url.com", "items_count": 10}
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_save", new_callable=AsyncMock, return_value=True) as mock_save:
            result = asyncio.run(cog._save_state(state))
        assert result is True
        mock_save.assert_called_once_with(state)
        import json
        saved = json.loads(cog.state_file.read_text())
        assert saved["sheet_url"] == "https://new-url.com"

    def test_load_state_empty_db_empty_file_returns_default(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_load", new_callable=AsyncMock, return_value={}):
            result = asyncio.run(cog._load_state())
        assert result == {"sheet_url": "", "items_count": 0}

    def test_load_state_db_failure_falls_back_to_file(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        file_state = {"sheet_url": "https://fallback.com/sheet", "items_count": 7}
        import json
        cog.state_file.write_text(json.dumps(file_state))
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_load", new_callable=AsyncMock, return_value={}):
            result = asyncio.run(cog._load_state())
        assert result["sheet_url"] == "https://fallback.com/sheet"

    def test_load_state_db_failure_no_file_returns_default(self, tmp_path):
        cog = self._make_cw_cog(tmp_path)
        with patch("NightCityBot.cogs.cyberware_shop.cw_shop_state_load", new_callable=AsyncMock, return_value={}):
            result = asyncio.run(cog._load_state())
        assert result == {"sheet_url": "", "items_count": 0}
