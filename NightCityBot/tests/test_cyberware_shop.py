"""Tests for CyberwareShop helper methods (_sorted_lots, _grouped_inventory)."""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord
import pytest

from NightCityBot.cogs.cyberware_shop import CyberwareShop


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_cog(tmp_path: Path):
    """Build a CyberwareShop with file-system mocked to tmp_path and
    all Discord/DB interactions stubbed out."""
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=Exception("no channel"))
    bot.get_cog = MagicMock(return_value=None)

    ub = MagicMock()
    ub.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
    ub.update_balance = AsyncMock(return_value=True)

    cog = CyberwareShop.__new__(CyberwareShop)
    cog.bot = bot
    cog.unbelievaboat = ub
    cog.lock = asyncio.Lock()
    cog.data_dir = tmp_path
    cog.state_file = tmp_path / "state.json"
    cog.sheet_cache_path = tmp_path / "master_sheet.xlsx"
    cog.tx_file = tmp_path / "transactions.json"
    cog.inventory_dir = tmp_path / "inventory"
    cog.inventory_dir.mkdir(parents=True, exist_ok=True)
    cog._startup_done = True
    return cog


# ------------------------------------------------------------------
# TestSortedLots
# ------------------------------------------------------------------

class TestSortedLots:
    def test_alphabetical_ordering(self):
        lots = [
            {"item_name": "Sandevistan", "qty_available": 2, "unit_cost": 10000},
            {"item_name": "Berserk",     "qty_available": 1, "unit_cost": 8000},
            {"item_name": "Kiroshi",     "qty_available": 0, "unit_cost": 3000},
        ]
        result = CyberwareShop._sorted_lots(lots)
        assert [l["item_name"] for l in result] == ["Berserk", "Kiroshi", "Sandevistan"]

    def test_includes_sold_out_items_in_single_list(self):
        """Sold-out lots must NOT be moved to the end — all lots in one alpha list."""
        lots = [
            {"item_name": "Zebra",  "qty_available": 0, "unit_cost": 500},
            {"item_name": "Alpha",  "qty_available": 3, "unit_cost": 500},
            {"item_name": "Middle", "qty_available": 0, "unit_cost": 500},
        ]
        result = CyberwareShop._sorted_lots(lots)
        names = [l["item_name"] for l in result]
        assert names == ["Alpha", "Middle", "Zebra"]

    def test_stable_numbering_after_sold_out(self):
        """Lot numbers should not shift when an item sells out."""
        lots = [
            {"item_name": "B-Item", "qty_available": 1, "unit_cost": 100},
            {"item_name": "A-Item", "qty_available": 2, "unit_cost": 100},
        ]
        ordered_before = [l["item_name"] for l in CyberwareShop._sorted_lots(lots)]
        lots[1]["qty_available"] = 0
        ordered_after = [l["item_name"] for l in CyberwareShop._sorted_lots(lots)]
        assert ordered_before == ordered_after

    def test_empty_list_returns_empty(self):
        assert CyberwareShop._sorted_lots([]) == []


# ------------------------------------------------------------------
# TestGroupedInventory
# ------------------------------------------------------------------

class TestGroupedInventory:
    def test_groups_by_name_price_date(self):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T12:00:00+00:00"},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["name"] == "Kiroshi"

    def test_different_price_makes_separate_group(self):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 4000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert len(groups) == 2

    def test_different_date_makes_separate_group(self):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-02T10:00:00+00:00"},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert len(groups) == 2

    def test_fifo_ordering_within_group(self):
        id_early = str(uuid.uuid4())
        id_late  = str(uuid.uuid4())
        inventory = [
            {"item_id": id_late,  "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T12:00:00+00:00"},
            {"item_id": id_early, "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T08:00:00+00:00"},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert groups[0]["items"][0]["item_id"] == id_early

    def test_alphabetical_group_ordering(self):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Sandevistan", "price_paid": None,
             "purchased_at": None},
            {"item_id": str(uuid.uuid4()), "name": "Berserk", "price_paid": None,
             "purchased_at": None},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert groups[0]["name"] == "Berserk"
        assert groups[1]["name"] == "Sandevistan"

    def test_count_reflects_group_size(self):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        groups = CyberwareShop._grouped_inventory(inventory)
        assert groups[0]["count"] == 3
