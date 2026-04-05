"""CyberwareShop cog tests — helper methods and inventory migration."""

import asyncio
import json
import random
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
    cog.DEFAULT_CW_RESTOCK_SETTINGS = CyberwareShop.DEFAULT_CW_RESTOCK_SETTINGS

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


def _seed_state(cog, lots=None, extra=None):
    state = {}
    if lots is not None:
        state["cw_wholesale_lots"] = lots
    if extra:
        state.update(extra)
    _run(cog._save_state(state))


def _seed_inventory(cog, user_id, items):
    _run(cog._save_inventory(user_id, items))


def _inv_names(inv) -> list[str]:
    """Extract item names from a list that may contain strings or dicts."""
    return [e["name"] if isinstance(e, dict) else e for e in inv]


_CATALOG = [
    {"name": "Kiroshi Optics Mk.1", "price": 3000, "cwp": "CWP-1", "description": "Basic optics"},
    {"name": "Militech Berserk Mk.1", "price": 5000, "cwp": "", "description": ""},
    {"name": "Sandevistan Mk.1", "price": 8000, "cwp": "CWP-3", "description": "Reflex booster"},
]


def _seed_catalog(cog: CyberwareShop, catalog=None) -> None:
    """Write catalog.json so _load_catalog fallback finds it."""
    if catalog is None:
        catalog = _CATALOG
    with open(cog.data_dir / "catalog.json", "w") as f:
        json.dump(catalog, f)


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


class TestLookupLot:
    @staticmethod
    def _lots():
        return [
            {"item_name": "Kiroshi Optics Mk.1", "unit_cost": 3000, "qty_available": 2},
            {"item_name": "Sandevistan Mk.1", "unit_cost": 8000, "qty_available": 1},
        ]

    def test_exact_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lot = cog._lookup_lot(self._lots(), "Kiroshi Optics Mk.1")
        assert lot is not None
        assert lot["item_name"] == "Kiroshi Optics Mk.1"

    def test_case_insensitive(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lot = cog._lookup_lot(self._lots(), "sandevistan mk.1")
        assert lot is not None
        assert lot["item_name"] == "Sandevistan Mk.1"

    def test_prefix_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lot = cog._lookup_lot(self._lots(), "Kiro")
        assert lot is not None
        assert lot["item_name"] == "Kiroshi Optics Mk.1"

    def test_no_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        assert cog._lookup_lot(self._lots(), "Phantom Liberty") is None


class TestRestockSettings:
    def test_defaults_when_empty(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = cog._resolve_cw_restock_settings({})
        assert cfg == CyberwareShop.DEFAULT_CW_RESTOCK_SETTINGS

    def test_overrides_applied(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        state = {"settings": {"cw_restock": {"total_items": 20, "qty_min": 2}}}
        cfg = cog._resolve_cw_restock_settings(state)
        assert cfg["total_items"] == 20
        assert cfg["qty_min"] == 2
        assert cfg["qty_max"] == CyberwareShop.DEFAULT_CW_RESTOCK_SETTINGS["qty_max"]

    def test_unknown_keys_ignored(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        state = {"settings": {"cw_restock": {"bogus_key": 999}}}
        cfg = cog._resolve_cw_restock_settings(state)
        assert "bogus_key" not in cfg


class TestGenerateCwLots:
    def test_lot_count_capped_by_catalog(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = {"total_items": 100, "qty_min": 1, "qty_max": 3}
        lots = cog._generate_cw_lots(_CATALOG, cfg, random.Random(42))
        assert len(lots) == len(_CATALOG)

    def test_lot_count_respects_setting(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = {"total_items": 2, "qty_min": 1, "qty_max": 3}
        lots = cog._generate_cw_lots(_CATALOG, cfg, random.Random(42))
        assert len(lots) == 2

    def test_qty_within_bounds(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = {"total_items": 3, "qty_min": 2, "qty_max": 5}
        lots = cog._generate_cw_lots(_CATALOG, cfg, random.Random(99))
        for lot in lots:
            assert 2 <= lot["qty_available"] <= 5

    def test_lot_has_required_fields(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = {"total_items": 1, "qty_min": 1, "qty_max": 1}
        lots = cog._generate_cw_lots(_CATALOG, cfg, random.Random(0))
        assert len(lots) == 1
        lot = lots[0]
        for field in ("lot_id", "item_name", "unit_cost", "qty_available", "created_at"):
            assert field in lot
        assert lot["lot_id"].startswith("cwlot-")

    def test_deterministic_with_seed(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = {"total_items": 2, "qty_min": 1, "qty_max": 3}
        names_a = [l["item_name"] for l in cog._generate_cw_lots(_CATALOG, cfg, random.Random(7))]
        names_b = [l["item_name"] for l in cog._generate_cw_lots(_CATALOG, cfg, random.Random(7))]
        assert names_a == names_b


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

    def test_stable_numbering_not_affected_by_stock(self, tmp_path, monkeypatch):
        """_sorted_lots returns all lots alphabetically; sold-out lots keep their numbers."""
        cog = _make_cog(tmp_path, monkeypatch)
        lots = [
            {"item_name": "Sandevistan",  "qty_available": 2, "unit_cost": 8000, "lot_id": "s"},
            {"item_name": "Kiroshi",      "qty_available": 0, "unit_cost": 3000, "lot_id": "k"},
            {"item_name": "Berserk",      "qty_available": 1, "unit_cost": 5000, "lot_id": "b"},
        ]
        ordered = cog._sorted_lots(lots)
        assert [l["item_name"] for l in ordered] == ["Berserk", "Kiroshi", "Sandevistan"]


class TestCwWhListGrouped:
    """!cw_wh_list should use format_cw_lines_grouped with slot headers."""

    def test_cw_wh_list_grouped_by_slot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lots = [
            {"item_name": "Kiroshi Mk.I", "qty_available": 3, "unit_cost": 2000, "lot_id": "a", "cwp": 7, "slot": "ocular system"},
            {"item_name": "Neural Link",  "qty_available": 5, "unit_cost": 5000, "lot_id": "b", "cwp": 14, "slot": "neural"},
            {"item_name": "Subdermal",    "qty_available": 0, "unit_cost": 1000, "lot_id": "c", "cwp": 2, "slot": "integumentary system"},
        ]
        state = {"cw_wholesale_lots": lots, "settings": {"last_cw_restock_sunday": "2026-04-05"}}
        cog._load_state = AsyncMock(return_value=state)
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        _run(cog.cw_wh_list.callback(cog, ctx))
        assert ctx.send.call_count == 1
        embed = ctx.send.call_args[1]["embed"]
        desc = embed.description
        assert "▬▬" in desc
        assert "[CWP:" in desc
        assert "Neural Link" in desc
        assert "Kiroshi Mk.I" in desc

    def test_cw_wh_list_empty(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cog._load_state = AsyncMock(return_value={"cw_wholesale_lots": []})
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        _run(cog.cw_wh_list.callback(cog, ctx))
        msg = ctx.send.call_args[0][0]
        assert "No cyberware wholesale stock" in msg

    def test_cw_wh_list_and_buy_row_consistency(self, tmp_path, monkeypatch):
        """Row N in !cw_wh_list must map to same item in !cw_buy N."""
        cog = _make_cog(tmp_path, monkeypatch)
        lots = [
            {"item_name": "Zetatech Link", "qty_available": 2, "unit_cost": 9000, "lot_id": "z", "cwp": 14, "slot": "neural"},
            {"item_name": "Kiroshi Mk.I",  "qty_available": 3, "unit_cost": 2000, "lot_id": "k", "cwp": 7, "slot": "ocular system"},
            {"item_name": "Arm Blade",     "qty_available": 1, "unit_cost": 4000, "lot_id": "a", "cwp": 5, "slot": "arms & arm attachments"},
        ]
        state = {"cw_wholesale_lots": lots, "settings": {"last_cw_restock_sunday": "2026-04-05"}}
        cog._load_state = AsyncMock(return_value=state)
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        _run(cog.cw_wh_list.callback(cog, ctx))
        embed = ctx.send.call_args[1]["embed"]
        desc = embed.description
        displayed = []
        for line in desc.split("\n"):
            if line.startswith("`") and "**" in line:
                name = line.split("**")[1]
                displayed.append(name)
        buy_ordered = cog._slot_ordered_lots(lots)
        buy_names = [l["item_name"] for l in buy_ordered]
        assert displayed == buy_names
