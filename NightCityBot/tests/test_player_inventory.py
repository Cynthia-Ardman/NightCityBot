"""Tests for PlayerInventoryCog helper methods (_group_items, _build_display)."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.player_inventory import PlayerInventoryCog, TradeConfirmView


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _item(name="Kiroshi", item_type="cyberware", restriction="basic",
          char="V", price=3000, seller="Doc", date="2026-04-01"):
    return {
        "item_id": str(uuid.uuid4()),
        "name": name,
        "item_type": item_type,
        "restriction": restriction,
        "character_name": char,
        "price_paid": price,
        "seller_name": seller,
        "acquired_at": date,
        "owner_id": "111",
    }


# ------------------------------------------------------------------
# TestGroupItems — static helper
# ------------------------------------------------------------------

class TestGroupItems:
    def test_identical_items_grouped_together(self):
        items = [
            _item("Kiroshi", date="2026-04-01"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        assert len(groups) == 1
        assert groups[0]["count"] == 2

    def test_different_names_separate_groups(self):
        items = [_item("Sandevistan"), _item("Kiroshi")]
        groups = PlayerInventoryCog._group_items(items)
        assert groups[0]["name"] == "Kiroshi"
        assert groups[1]["name"] == "Sandevistan"

    def test_fifo_ordering_within_group(self):
        items = [
            _item("Kiroshi", date="2026-04-01"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["items"][0]["acquired_at"] == "2026-04-01"

    def test_different_dates_produce_separate_groups(self):
        """Items with same name/price/seller but different acquisition dates → separate rows."""
        items = [
            _item("Kiroshi", date="2026-04-02"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        assert len(groups) == 2
        assert groups[0]["acquired_date"] == "2026-04-01"
        assert groups[1]["acquired_date"] == "2026-04-02"
        assert groups[0]["count"] == 1
        assert groups[1]["count"] == 1

    def test_acquired_date_in_group_key(self):
        """acquired_date is included in each group dict for display."""
        items = [_item("Sandevistan", date="2026-03-15")]
        groups = PlayerInventoryCog._group_items(items)
        assert groups[0]["acquired_date"] == "2026-03-15"


# ------------------------------------------------------------------
# TestBuildDisplay — grouped character display
# ------------------------------------------------------------------

class TestBuildDisplay:
    def test_groups_by_item_type(self):
        items = [
            {**_item("Kiroshi", char="V", item_type="cyberware"), "owner_id": "1"},
            {**_item("Berserk", char="Johnny", item_type="gun"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items)
        header_lines = [ln for rn, ln in display if rn is None]
        assert any("Guns" in h for h in header_lines)
        assert any("Cyberware" in h for h in header_lines)

    def test_row_numbers_sequential(self):
        items = [
            {**_item("Kiroshi", char="V"),     "owner_id": "1"},
            {**_item("Berserk", char="Johnny"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items)
        item_rows = [rn for rn, _ in display if rn is not None]
        assert item_rows == [1, 2]

    def test_char_filter_narrows_display(self):
        items = [
            {**_item("Kiroshi", char="V"),      "owner_id": "1"},
            {**_item("Berserk",  char="Johnny"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items, char_filter="V")
        assert all(
            rn is None or "Kiroshi" in ln or "V" in ln
            for rn, ln in display
        )
        item_rows = [rn for rn, _ in display if rn is not None]
        assert len(item_rows) == 1

    def test_char_filter_assigns_local_row_numbers(self):
        """Filtered view assigns local row numbers starting from 1."""
        items = [
            {**_item("Axe",     char="Alpha"), "owner_id": "1"},
            {**_item("Bomb",    char="Alpha"), "owner_id": "1"},
            {**_item("Pistol",  char="V", item_type="gun"), "owner_id": "1"},
        ]
        display_all, _ = PlayerInventoryCog._build_display(items)
        all_rows = [rn for rn, _ in display_all if rn is not None]
        assert all_rows == [1, 2, 3]

        display_v, groups_v = PlayerInventoryCog._build_display(items, char_filter="V")
        filtered_rows = [rn for rn, _ in display_v if rn is not None]
        assert filtered_rows == [1]
        assert len(groups_v) == 1
        assert groups_v[0]["name"] == "Pistol"
