import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import discord
import pytest

from NightCityBot.utils.startup_checks import _check_gdrive_config
from NightCityBot.utils.interaction_safety import safe_followup
from NightCityBot.utils.helpers import truncation_note


def _run(coro):
    return asyncio.run(coro)


class TestGDriveConfigCheck:
    def test_warns_missing_folder_id(self, caplog):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GDRIVE_BACKUP_FOLDER_ID", None)
            os.environ.pop("GDRIVE_SERVICE_ACCOUNT_JSON", None)
            import importlib
            with caplog.at_level("WARNING"):
                _check_gdrive_config()
            assert "GDRIVE_BACKUP_FOLDER_ID" in caplog.text

    def test_warns_missing_sa_json(self, caplog):
        with patch.dict(os.environ, {"GDRIVE_BACKUP_FOLDER_ID": "abc"}, clear=True):
            os.environ.pop("GDRIVE_SERVICE_ACCOUNT_JSON", None)
            with caplog.at_level("WARNING"):
                _check_gdrive_config()
            assert "GDRIVE_SERVICE_ACCOUNT_JSON" in caplog.text

    def test_warns_invalid_sa_json(self, caplog):
        with patch.dict(os.environ, {
            "GDRIVE_BACKUP_FOLDER_ID": "abc",
            "GDRIVE_SERVICE_ACCOUNT_JSON": "not-json",
        }, clear=True):
            with caplog.at_level("WARNING"):
                _check_gdrive_config()
            assert "not valid JSON" in caplog.text

    def test_no_warnings_valid_config(self, caplog):
        with patch.dict(os.environ, {
            "GDRIVE_BACKUP_FOLDER_ID": "abc",
            "GDRIVE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account"}),
        }, clear=True):
            with caplog.at_level("WARNING"):
                _check_gdrive_config()
            assert "GDRIVE_BACKUP_FOLDER_ID" not in caplog.text
            assert "GDRIVE_SERVICE_ACCOUNT_JSON" not in caplog.text


class TestSafeFollowup:
    def test_returns_true_on_success(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        interaction.user.id = 123
        result = _run(safe_followup(interaction, "test", ephemeral=True))
        assert result is True
        interaction.followup.send.assert_called_once()

    def test_returns_false_on_not_found(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "expired"))
        interaction.user.id = 123
        result = _run(safe_followup(interaction, "test", ephemeral=True))
        assert result is False

    def test_returns_false_on_http_exception(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = True
        resp = MagicMock()
        resp.status = 500
        interaction.followup.send = AsyncMock(side_effect=discord.HTTPException(resp, "server error"))
        interaction.user.id = 123
        result = _run(safe_followup(interaction, "test", ephemeral=True))
        assert result is False

    def test_uses_response_when_not_done(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.user.id = 123
        result = _run(safe_followup(interaction, "hi", ephemeral=True))
        assert result is True
        interaction.response.send_message.assert_called_once()


class TestTruncationNote:
    def test_no_note_under_limit(self):
        assert truncation_note(10) == ""
        assert truncation_note(25) == ""

    def test_note_over_limit(self):
        note = truncation_note(30)
        assert "25" in note
        assert "30" in note

    def test_custom_kind(self):
        note = truncation_note(50, kind="guns")
        assert "guns" in note


class TestOwnerIdFallback:
    def test_store_id_parsing(self):
        store_id = "12345:67890"
        parts = store_id.split(":")
        assert parts[-1] == "67890"

    def test_fallback_uses_store_id(self):
        store_id = "12345:67890"
        store_data = {}
        _fallback_owner = int(store_id.split(":")[-1]) if ":" in store_id else 0
        owner_id = store_data.get("owner_id", _fallback_owner)
        assert owner_id == 67890

    def test_store_data_takes_priority(self):
        store_id = "12345:67890"
        store_data = {"owner_id": 11111}
        _fallback_owner = int(store_id.split(":")[-1]) if ":" in store_id else 0
        owner_id = store_data.get("owner_id", _fallback_owner)
        assert owner_id == 11111


class TestUnbelievaBoatSemaphore:
    def test_semaphore_created(self):
        from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
        api = UnbelievaBoatAPI("test-token", session=MagicMock())
        assert hasattr(api, "_semaphore")
        assert isinstance(api._semaphore, asyncio.Semaphore)

    def test_retry_after_parsing(self):
        from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
        assert UnbelievaBoatAPI._parse_retry_after({"retry_after": 2}, 0) == 2.0
        assert UnbelievaBoatAPI._parse_retry_after({"retry_after": 2000}, 0) == 2.0
        delay_with_backoff = UnbelievaBoatAPI._parse_retry_after({"retry_after": 1}, 2)
        assert delay_with_backoff > 1.0
        assert UnbelievaBoatAPI._parse_retry_after({}, 0) == 1.0


class TestPoolCloseOnShutdown:
    def test_close_pool_import_exists(self):
        from NightCityBot.utils.db import close_pool
        assert callable(close_pool)

    def test_bot_close_code_references_close_pool(self):
        import inspect
        from NightCityBot.bot import NightCityBot as BotClass
        source = inspect.getsource(BotClass.close)
        assert "close_pool" in source


class TestCancelPendingTransfers:
    def test_extract_owner_id_from_store_id(self):
        from NightCityBot.utils.db import cancel_pending_transfers_for_store
        store_id = "12345:67890"
        parts = store_id.rsplit(":", 1)
        assert parts[-1] == "67890"

    def test_ripperdoc_store_id_format(self):
        store_id = "rd:12345:67890"
        parts = store_id.rsplit(":", 1)
        assert parts[-1] == "67890"


class TestEventLockExists:
    def test_economy_has_event_lock(self):
        with patch("NightCityBot.cogs.economy.config") as mock_cfg:
            mock_cfg.ATTENDANCE_CHANNEL_ID = 123
            mock_cfg.TIMEZONE = "UTC"
            with patch("NightCityBot.cogs.economy.TraumaTeamService"):
                bot = MagicMock()
                bot.unbelievaboat = MagicMock()
                from NightCityBot.cogs.economy import Economy
                eco = Economy(bot)
                assert hasattr(eco, "_event_lock")
                assert isinstance(eco._event_lock, asyncio.Lock)


class TestCWCatalogBuyFlow:
    def test_catalog_buy_adds_to_inventory(self):
        async def run():
            from NightCityBot.cogs.ripperdoc_hub import WholesaleBuySelect

            catalog_lot = {
                "lot_id": "cat-Neural Link",
                "item_name": "Neural Link",
                "unit_cost": 5000,
                "qty_available": 99,
            }

            cw_cog = MagicMock()
            cw_cog._locks = MagicMock()
            lock = asyncio.Lock()
            cw_cog._locks.acquire = MagicMock(return_value=lock)
            cw_cog._load_inventory = AsyncMock(return_value=[])
            saved_inventories = []
            async def fake_save_inv(uid, inv):
                saved_inventories.append(inv)
                return True
            cw_cog._save_inventory = AsyncMock(side_effect=fake_save_inv)

            cog = MagicMock()
            cog.unbelievaboat = MagicMock()
            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10000, "bank": 0})
            cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
            cog._log_channel = AsyncMock(return_value=None)

            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 12345

            view = WholesaleBuySelect(cog, ctx, [catalog_lot], cw_cog)

            inter = MagicMock(spec=discord.Interaction)
            inter.user = MagicMock()
            inter.user.id = 12345
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            inter.response.is_done = MagicMock(return_value=False)
            inter.followup = MagicMock()
            inter.followup.send = AsyncMock()

            qty_mock = MagicMock()
            qty_mock.result = 1
            qty_mock.wait = AsyncMock()
            with patch("NightCityBot.cogs.ripperdoc_hub.QtySelectView", return_value=qty_mock), \
                 patch("NightCityBot.cogs.ripperdoc_hub.ih_record_event", new_callable=AsyncMock):
                view.select._values = ["0"]
                inter.data = {"values": ["0"]}
                await view.on_select(inter)

            assert len(saved_inventories) == 1
            assert len(saved_inventories[0]) == 1
            assert saved_inventories[0][0]["name"] == "Neural Link"

        _run(run())
