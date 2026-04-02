"""Tests for CyberwareShop — _sorted_lots, _grouped_inventory,
cw_inventory display, cw_sell FIFO + data-integrity ordering,
and cw_install FIFO + data-integrity ordering.
"""

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


def _ctx(author_id=111):
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.guild = MagicMock()
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = author_id
    ctx.author.display_name = f"Ripperdoc{author_id}"
    ctx.author.mention = f"<@{author_id}>"
    ctx.author.roles = []
    ctx.author.guild_permissions = MagicMock()
    ctx.author.guild_permissions.administrator = False
    return ctx


def _patient(patient_id=999):
    m = MagicMock(spec=discord.Member)
    m.id = patient_id
    m.display_name = f"Patient{patient_id}"
    m.mention = f"<@{patient_id}>"
    m.roles = []
    return m


def _lot(name="Kiroshi Optics Mk.1", price=3000, qty=2):
    return {
        "item_id": str(uuid.uuid4()),
        "name": name,
        "price_paid": price,
        "purchased_at": "2026-04-01T00:00:00+00:00",
    }


async def _cmd(cog, method_name, ctx, *args, **kwargs):
    cmd = getattr(cog, method_name)
    if hasattr(cmd, "callback"):
        return await cmd.callback(cog, ctx, *args, **kwargs)
    return await cmd(ctx, *args, **kwargs)


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
        # Sold-out "Middle" must stay between "Alpha" and "Zebra"
        assert names == ["Alpha", "Middle", "Zebra"]

    def test_stable_numbering_after_sold_out(self):
        """Lot numbers should not shift when an item sells out."""
        lots = [
            {"item_name": "B-Item", "qty_available": 1, "unit_cost": 100},
            {"item_name": "A-Item", "qty_available": 2, "unit_cost": 100},
        ]
        ordered_before = [l["item_name"] for l in CyberwareShop._sorted_lots(lots)]
        # A sells out
        lots[1]["qty_available"] = 0
        ordered_after = [l["item_name"] for l in CyberwareShop._sorted_lots(lots)]
        assert ordered_before == ordered_after  # same order regardless of stock

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
        assert groups[0]["items"][0]["item_id"] == id_early  # oldest first

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


# ------------------------------------------------------------------
# TestCwInventory
# ------------------------------------------------------------------

class TestCwInventory:
    def test_empty_inventory_sends_message(self, tmp_path):
        cog = _make_cog(tmp_path)
        ctx = _ctx()

        async def empty_inv(uid):
            return []

        cog._load_inventory = empty_inv
        _run(_cmd(cog, "cw_inventory", ctx))
        msg = ctx.send.call_args[0][0]
        assert "empty" in msg.lower()

    def test_grouped_display_shows_count(self, tmp_path):
        cog = _make_cog(tmp_path)
        ctx = _ctx()
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]

        async def mock_load(uid):
            return inventory

        cog._load_inventory = mock_load
        _run(_cmd(cog, "cw_inventory", ctx))
        embed = ctx.send.call_args[1]["embed"]
        assert "× 2" in embed.description  # count shown

    def test_dm_guard(self, tmp_path):
        cog = _make_cog(tmp_path)
        ctx = _ctx()
        ctx.guild = None
        _run(_cmd(cog, "cw_inventory", ctx))
        assert "server" in ctx.send.call_args[0][0]


# ------------------------------------------------------------------
# TestCwSell
# ------------------------------------------------------------------

class TestCwSell:
    def _setup_cog(self, tmp_path, inventory):
        """Return a cog with inventory pre-loaded for sell tests."""
        cog = _make_cog(tmp_path)
        inv_store = list(inventory)

        async def load_inv(uid):
            return list(inv_store)

        async def save_inv(uid, inv):
            inv_store.clear()
            inv_store.extend(inv)
            return True

        cog._load_inventory = load_inv
        cog._save_inventory = save_inv
        cog._append_tx = AsyncMock(return_value=True)
        cog._log_channel = AsyncMock(return_value=None)
        return cog, inv_store

    def test_invalid_row_rejected(self, tmp_path):
        cog, _ = self._setup_cog(tmp_path, [])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _patient(), 1, 5000, character_name="V"))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_zero_price_rejected(self, tmp_path):
        inventory = [{"item_id": str(uuid.uuid4()), "name": "Kiroshi",
                      "price_paid": 3000, "purchased_at": "2026-04-01T00:00:00+00:00"}]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _patient(), 1, 0, character_name="V"))
        msg = ctx.send.call_args[0][0]
        assert "❌" in msg and ("Price" in msg or "positive" in msg)

    def test_self_sell_rejected(self, tmp_path):
        inventory = [{"item_id": str(uuid.uuid4()), "name": "Kiroshi",
                      "price_paid": 3000, "purchased_at": "2026-04-01T00:00:00+00:00"}]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _patient(111), 1, 5000, character_name="V"))
        assert "yourself" in ctx.send.call_args[0][0].lower()

    def test_pi_add_item_called_before_save_inventory(self, tmp_path):
        """Data-integrity: pi_add_item must succeed before we remove from ripperdoc stock."""
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        call_order = []

        async def mock_pi_add(item_dict):
            call_order.append("pi_add")
            return True

        save_calls_when_pi_ran = []

        async def mock_save_inv(uid, inv):
            call_order.append("save_inv")
            inv_store.clear()
            inv_store.extend(inv)
            return True

        cog._save_inventory = mock_save_inv

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", mock_pi_add):
            _run(_cmd(cog, "cw_sell", ctx, _patient(999), 1, 5000, character_name="V"))

        assert "pi_add" in call_order
        assert "save_inv" in call_order
        # pi_add must come before save_inv
        assert call_order.index("pi_add") < call_order.index("save_inv")
        assert "✅" in ctx.send.call_args[0][0]

    def test_pi_add_failure_aborts_and_refunds(self, tmp_path):
        """If pi_add_item fails, the sale is rolled back — no item removed from ripperdoc stock."""
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        async def failing_pi_add(item_dict):
            return False

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", failing_pi_add):
            _run(_cmd(cog, "cw_sell", ctx, _patient(999), 1, 5000, character_name="V"))

        # Ripperdoc stock must still have the item
        assert len(inv_store) == 1
        assert inv_store[0]["item_id"] == item_id
        msg = ctx.send.call_args[0][0]
        assert "❌" in msg

    def test_fifo_selection_picks_oldest(self, tmp_path):
        """When a group has multiple items, the oldest (by purchased_at) is consumed first."""
        id_early = str(uuid.uuid4())
        id_late  = str(uuid.uuid4())
        inventory = [
            {"item_id": id_late,  "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T12:00:00+00:00"},
            {"item_id": id_early, "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T08:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        consumed = []

        async def capture_pi_add(item_dict):
            consumed.append(item_dict["item_id"])
            return True

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", capture_pi_add):
            _run(_cmd(cog, "cw_sell", ctx, _patient(999), 1, 5000, character_name="V"))

        assert consumed == [id_early]  # oldest consumed
        remaining_ids = {it["item_id"] for it in inv_store}
        assert id_late in remaining_ids
        assert id_early not in remaining_ids

    def test_item_recorded_in_player_inventory_with_correct_fields(self, tmp_path):
        """pi_add_item receives correct owner, character, price, and type."""
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Sandevistan", "price_paid": 8000,
             "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        patient = _patient(999)

        captured = []

        async def capture_pi_add(item_dict):
            captured.append(item_dict)
            return True

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", capture_pi_add):
            _run(_cmd(cog, "cw_sell", ctx, patient, 1, 12000, character_name="Johnny"))

        assert len(captured) == 1
        rec = captured[0]
        assert rec["owner_id"] == str(patient.id)
        assert rec["character_name"] == "Johnny"
        assert rec["item_type"] == "cyberware"
        assert rec["name"] == "Sandevistan"
        assert rec["price_paid"] == 12000  # price charged, not price_paid_orig
        assert rec["seller_id"] == str(ctx.author.id)


# ------------------------------------------------------------------
# TestCwInstall
# ------------------------------------------------------------------

class TestCwInstall:
    def _setup_cog(self, tmp_path, inventory):
        cog = _make_cog(tmp_path)
        inv_store = list(inventory)

        async def load_inv(uid):
            return list(inv_store)

        async def save_inv(uid, inv):
            inv_store.clear()
            inv_store.extend(inv)
            return True

        cog._load_inventory = load_inv
        cog._save_inventory = save_inv
        cog._append_tx = AsyncMock(return_value=True)
        cog._log_channel = AsyncMock(return_value=None)
        return cog, inv_store

    def test_invalid_row_rejected(self, tmp_path):
        cog, _ = self._setup_cog(tmp_path, [])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_install", ctx, _patient(), "V", 1))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_pi_add_item_called_before_save_inventory(self, tmp_path):
        """pi_add_item must succeed before ripperdoc stock is modified."""
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        call_order = []

        async def mock_pi_add(item_dict):
            call_order.append("pi_add")
            return True

        async def mock_save_inv(uid, inv):
            call_order.append("save_inv")
            inv_store.clear()
            inv_store.extend(inv)
            return True

        cog._save_inventory = mock_save_inv

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", mock_pi_add):
            _run(_cmd(cog, "cw_install", ctx, _patient(999), "V", 1))

        assert call_order.index("pi_add") < call_order.index("save_inv")
        assert "✅" in ctx.send.call_args[0][0]

    def test_pi_add_failure_aborts_install_stock_unchanged(self, tmp_path):
        """If pi_add_item fails, ripperdoc stock must NOT be modified."""
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        async def failing_pi_add(item_dict):
            return False

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", failing_pi_add):
            _run(_cmd(cog, "cw_install", ctx, _patient(999), "V", 1))

        assert len(inv_store) == 1
        assert inv_store[0]["item_id"] == item_id
        assert "❌" in ctx.send.call_args[0][0]

    def test_fifo_selection_picks_oldest(self, tmp_path):
        id_early = str(uuid.uuid4())
        id_late  = str(uuid.uuid4())
        inventory = [
            {"item_id": id_late,  "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T18:00:00+00:00"},
            {"item_id": id_early, "name": "Kiroshi", "price_paid": 3000,
             "purchased_at": "2026-04-01T06:00:00+00:00"},
        ]
        cog, inv_store = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)

        consumed = []

        async def capture_pi_add(item_dict):
            consumed.append(item_dict["item_id"])
            return True

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", capture_pi_add):
            _run(_cmd(cog, "cw_install", ctx, _patient(999), "V", 1))

        assert consumed == [id_early]
        assert id_late in {it["item_id"] for it in inv_store}
        assert id_early not in {it["item_id"] for it in inv_store}

    def test_item_recorded_with_correct_fields(self, tmp_path):
        item_id = str(uuid.uuid4())
        inventory = [
            {"item_id": item_id, "name": "Berserk",
             "price_paid": 8000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        patient = _patient(999)

        captured = []

        async def capture_pi_add(item_dict):
            captured.append(item_dict)
            return True

        with patch("NightCityBot.cogs.cyberware_shop.pi_add_item", capture_pi_add):
            _run(_cmd(cog, "cw_install", ctx, patient, "Johnny", 1))

        assert len(captured) == 1
        rec = captured[0]
        assert rec["owner_id"] == str(patient.id)
        assert rec["character_name"] == "Johnny"
        assert rec["item_type"] == "cyberware"
        assert rec["name"] == "Berserk"
        assert rec["price_paid"] == 8000  # original purchase price passed through
        assert rec["seller_id"] == str(ctx.author.id)

    def test_dm_guard(self, tmp_path):
        cog, _ = self._setup_cog(tmp_path, [])
        ctx = _ctx(author_id=111)
        ctx.guild = None
        _run(_cmd(cog, "cw_install", ctx, _patient(999), "V", 1))
        assert "server" in ctx.send.call_args[0][0]

    def test_missing_character_name_rejected(self, tmp_path):
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        # Pass empty string as character_name
        _run(_cmd(cog, "cw_install", ctx, _patient(999), "", 1))
        assert "Character" in ctx.send.call_args[0][0] or "character" in ctx.send.call_args[0][0].lower()


# ------------------------------------------------------------------
# TestCwCatalog — coverage for cw_catalog DM guard and empty catalog
# ------------------------------------------------------------------

class TestCwCatalog:
    def test_dm_guard(self, tmp_path):
        """cw_catalog in a DM is rejected."""
        cog = _make_cog(tmp_path)
        ctx = _ctx()
        ctx.guild = None
        _run(_cmd(cog, "cw_catalog", ctx))
        assert "server" in ctx.send.call_args[0][0]

    def test_empty_catalog_message(self, tmp_path):
        """cw_catalog sends an appropriate message when catalog is empty."""
        cog = _make_cog(tmp_path)
        ctx = _ctx()

        async def empty_catalog():
            return []

        cog._load_catalog = empty_catalog
        _run(_cmd(cog, "cw_catalog", ctx))
        msg = ctx.send.call_args[0][0]
        assert "❌" in msg
        assert "catalog" in msg.lower() or "empty" in msg.lower()


# ------------------------------------------------------------------
# TestCwSellExtraGuards — additional cw_sell guards for coverage
# ------------------------------------------------------------------

class TestCwSellExtraGuards:
    def _setup_cog(self, tmp_path, inventory):
        cog = _make_cog(tmp_path)
        inv_store = list(inventory)

        async def load_inv(uid):
            return list(inv_store)

        async def save_inv(uid, inv):
            inv_store.clear()
            inv_store.extend(inv)
            return True

        cog._load_inventory = load_inv
        cog._save_inventory = save_inv
        cog._append_tx = AsyncMock(return_value=True)
        cog._log_channel = AsyncMock(return_value=None)
        return cog, inv_store

    def test_empty_character_name_rejected(self, tmp_path):
        """cw_sell rejects an empty character name."""
        inventory = [
            {"item_id": str(uuid.uuid4()), "name": "Kiroshi",
             "price_paid": 3000, "purchased_at": "2026-04-01T10:00:00+00:00"},
        ]
        cog, _ = self._setup_cog(tmp_path, inventory)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _patient(999), 1, 5000, character_name=""))
        msg = ctx.send.call_args[0][0]
        assert "❌" in msg
        assert "character" in msg.lower()
