"""Tests for CyberwareShop helper methods (_grouped_inventory)."""

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
