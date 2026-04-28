"""CyberwareShop cog tests — helper methods and inventory migration."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from NightCityBot.cogs.cyberware_shop import CyberwareShop


# ------------------------------------------------------------------
# Helpers — cog factory
# ------------------------------------------------------------------

def _make_cog(tmp_path: Path, monkeypatch):
    """Instantiate CyberwareShop bypassing __init__, patch DB helpers."""
    monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
    monkeypatch.setattr("config.RIPPERDOC_ROLE_ID", 800)
    monkeypatch.setattr("config.FIXER_ROLE_ID", 900)

    cog = CyberwareShop.__new__(CyberwareShop)
    cog.bot = MagicMock()
    cog.bot.get_cog = MagicMock(return_value=None)
    cog.bot.get_channel = MagicMock(return_value=None)
    cog.unbelievaboat = MagicMock()
    cog.lock = asyncio.Lock()
    cog._startup_done = True

    data_dir = tmp_path / "cyberware_shop"
    data_dir.mkdir(parents=True)
    inv_dir = data_dir / "inventory"
    inv_dir.mkdir()

    cog.data_dir = data_dir
    cog.state_file = data_dir / "state.json"
    cog.sheet_cache_path = data_dir / "master_sheet.xlsx"
    cog.tx_file = data_dir / "transactions.json"
    cog.inventory_dir = inv_dir

    monkeypatch.setattr(
        "NightCityBot.cogs.cyberware_shop.cw_catalog_get_all",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "NightCityBot.cogs.cyberware_shop.cw_catalog_upsert_many",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "NightCityBot.cogs.cyberware_shop.cw_catalog_upsert_one",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "NightCityBot.cogs.cyberware_shop.cw_catalog_delete_one",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "NightCityBot.cogs.cyberware_shop.pi_add_item",
        AsyncMock(return_value=True),
    )

    return cog


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


_CATALOG = [
    {"name": "Kiroshi Optics Mk.1", "price": 3000, "cwp": "CWP-1", "description": "Basic optics"},
    {"name": "Militech Berserk Mk.1", "price": 5000, "cwp": "", "description": ""},
    {"name": "Sandevistan Mk.1", "price": 8000, "cwp": "CWP-3", "description": "Reflex booster"},
]


# ------------------------------------------------------------------
# Unit tests — pure helpers, no I/O
# ------------------------------------------------------------------

class TestLookupItem:
    def test_exact_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        item = cog._lookup_item(_CATALOG, "Kiroshi Optics Mk.1")
        assert item is not None
        assert item["name"] == "Kiroshi Optics Mk.1"

    def test_case_insensitive_exact(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        item = cog._lookup_item(_CATALOG, "kiroshi optics mk.1")
        assert item is not None
        assert item["name"] == "Kiroshi Optics Mk.1"

    def test_prefix_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        item = cog._lookup_item(_CATALOG, "Kiroshi")
        assert item is not None
        assert item["name"] == "Kiroshi Optics Mk.1"

    def test_no_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        assert cog._lookup_item(_CATALOG, "Unknown Implant XYZ") is None



# ------------------------------------------------------------------
# _load_inventory migration tests
# ------------------------------------------------------------------

class TestLoadInventoryMigration:
    def test_dict_entry_missing_item_id_gets_uuid(self, tmp_path, monkeypatch):
        """Legacy dict entries without item_id are assigned a UUID and saved back."""
        cog = _make_cog(tmp_path, monkeypatch)
        legacy = [{"name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-01-01"}]
        inv_file = cog.inventory_dir / "111.json"
        inv_file.write_text(json.dumps(legacy))

        inv = _run(cog._load_inventory(111))

        assert len(inv) == 1
        assert "item_id" in inv[0]
        assert inv[0]["item_id"]

        saved = json.loads(inv_file.read_text())
        assert "item_id" in saved[0]
        assert saved[0]["item_id"] == inv[0]["item_id"]

    def test_dict_entry_with_item_id_unchanged(self, tmp_path, monkeypatch):
        """Dict entries that already have item_id are left untouched."""
        cog = _make_cog(tmp_path, monkeypatch)
        existing_id = "existing-uuid-123"
        data = [{"item_id": existing_id, "name": "Sandevistan Mk.1", "price_paid": 8000, "purchased_at": None}]
        inv_file = cog.inventory_dir / "222.json"
        inv_file.write_text(json.dumps(data))

        inv = _run(cog._load_inventory(222))

        assert inv[0]["item_id"] == existing_id

    def test_cw_sell_item_without_id_not_duplicated(self, tmp_path, monkeypatch):
        """_load_inventory assigns UUID to dict entry → filter removes correct item."""
        cog = _make_cog(tmp_path, monkeypatch)
        legacy = [{"name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}]
        inv_file = cog.inventory_dir / "111.json"
        inv_file.write_text(json.dumps(legacy))

        inv = _run(cog._load_inventory(111))
        assert "item_id" in inv[0]

        item_id = inv[0]["item_id"]
        updated = [it for it in inv if it.get("item_id") != item_id]
        assert updated == []


# ------------------------------------------------------------------
# Grouped inventory helper tests
# ------------------------------------------------------------------

class TestGroupedInventory:
    """_grouped_inventory correctly groups and FIFO-sorts inventory rows."""

    def test_single_item_one_group(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        inv = [{"item_id": "a", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00"}]
        groups = cog._grouped_inventory(inv)
        assert len(groups) == 1
        assert groups[0]["name"] == "Kiroshi"
        assert groups[0]["count"] == 1

    def test_two_identical_items_one_group(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        inv = [
            {"item_id": "a", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01"},
            {"item_id": "b", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01"},
        ]
        groups = cog._grouped_inventory(inv)
        assert len(groups) == 1
        assert groups[0]["count"] == 2

    def test_fifo_ordering_within_same_date_group(self, tmp_path, monkeypatch):
        """Items with the same date but different times are FIFO-sorted by timestamp."""
        cog = _make_cog(tmp_path, monkeypatch)
        inv = [
            {"item_id": "later",   "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01T14:00:00"},
            {"item_id": "earlier", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01T08:00:00"},
        ]
        groups = cog._grouped_inventory(inv)
        assert len(groups) == 1
        assert groups[0]["items"][0]["item_id"] == "earlier"

    def test_different_dates_different_groups(self, tmp_path, monkeypatch):
        """Items with same name+price but different purchase dates are separate groups."""
        cog = _make_cog(tmp_path, monkeypatch)
        inv = [
            {"item_id": "day2", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-02"},
            {"item_id": "day1", "name": "Kiroshi", "price_paid": 3000, "purchased_at": "2026-04-01"},
        ]
        groups = cog._grouped_inventory(inv)
        assert len(groups) == 2
        assert groups[0]["date"] == "2026-04-01"
        assert groups[1]["date"] == "2026-04-02"

    def test_different_names_different_groups(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        inv = [
            {"item_id": "a", "name": "Sandevistan", "price_paid": 8000, "purchased_at": None},
            {"item_id": "b", "name": "Kiroshi",     "price_paid": 3000, "purchased_at": None},
        ]
        groups = cog._grouped_inventory(inv)
        assert groups[0]["name"] == "Kiroshi"
        assert groups[1]["name"] == "Sandevistan"

class TestCwBuyHelpTextNoDeadRefs:
    """!cw_buy docstring + invalid-lot error must not reference removed commands."""

    _REMOVED = (
        "!cw_catalog",
        "!cw_wh_list",
        "!cw_wh_restock",
        "!cw_wh_add",
        "!cw_wh_remove",
        "!cw_wh_settings",
    )

    def test_docstring_has_no_removed_command_references(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        doc = cog.cw_buy.callback.__doc__ or ""
        for removed in self._REMOVED:
            assert removed not in doc, f"cw_buy docstring references removed command {removed}"

    def test_invalid_lot_error_has_no_removed_command_references(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        catalog_items = [
            {"name": "Kiroshi Mk.I", "price": 2000, "cwp": 7, "slot": "ocular system"},
        ]
        monkeypatch.setattr(
            "NightCityBot.cogs.cyberware_shop.cw_catalog_get_all",
            AsyncMock(return_value=catalog_items),
        )
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        _run(cog.cw_buy.callback(cog, ctx, lot_number=99, qty=1))
        msg = ctx.send.call_args[0][0]
        assert "Invalid lot number" in msg
        for removed in self._REMOVED:
            assert removed not in msg, f"cw_buy invalid-lot message references removed command {removed}"

