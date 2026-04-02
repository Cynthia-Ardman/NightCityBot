"""Comprehensive CyberwareShop cog tests covering all command flows end-to-end."""

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


def _ctx(guild=True, author_id=111):
    """Create a minimal mock discord context."""
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = author_id
    ctx.author.display_name = f"User{author_id}"
    ctx.author.mention = f"<@{author_id}>"
    ctx.author.roles = []
    ctx.author.guild_permissions = MagicMock()
    ctx.author.guild_permissions.administrator = False
    ctx.guild = MagicMock() if guild else None
    return ctx


def _make_member(member_id: int, name: str = "Member") -> discord.Member:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.guild_permissions = MagicMock()
    m.guild_permissions.administrator = False
    return m


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


def _run(coro):
    """Run an async coroutine synchronously (matches test_wholesaler_full.py pattern)."""
    return asyncio.run(coro)


async def _cmd(cog, method_name, ctx, *args, **kwargs):
    """Invoke a discord Command's callback directly, bypassing argument conversion."""
    cmd = getattr(cog, method_name)
    if hasattr(cmd, "callback"):
        return await cmd.callback(cog, ctx, *args, **kwargs)
    return await cmd(ctx, *args, **kwargs)


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
# cw_buy tests
# ------------------------------------------------------------------

class TestCwBuy:
    @staticmethod
    def _lots():
        return [
            {"lot_id": "cwlot-001", "item_name": "Kiroshi Optics Mk.1", "unit_cost": 3000, "qty_available": 2},
            {"lot_id": "cwlot-002", "item_name": "Sandevistan Mk.1", "unit_cost": 8000, "qty_available": 0},
        ]

    # Lot 1 = Kiroshi Optics Mk.1 (available, first alpha)
    # Lot 2 = Sandevistan Mk.1 (sold out, second)

    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_buy", ctx, 1))
        ctx.send.assert_called_once()
        assert "server" in ctx.send.call_args[0][0]

    def test_empty_wholesale(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=[])
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 1))
        assert "No cyberware" in ctx.send.call_args[0][0]

    def test_invalid_lot_number(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 99))
        assert "Invalid lot number" in ctx.send.call_args[0][0]

    def test_sold_out(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 2))  # lot 2 = Sandevistan Mk.1 (sold out)
        assert "sold out" in ctx.send.call_args[0][0]

    def test_balance_fetch_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        cog.unbelievaboat.get_balance = AsyncMock(return_value=None)
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 1))
        assert "Could not fetch" in ctx.send.call_args[0][0]

    def test_insufficient_funds(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 0})
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 1))
        assert "Insufficient" in ctx.send.call_args[0][0]

    def test_balance_deduct_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=False)
        ctx = _ctx()
        _run(_cmd(cog, "cw_buy", ctx, 1))
        assert "Balance update failed" in ctx.send.call_args[0][0]

    def test_success(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
        ctx = _ctx(author_id=201)

        _run(_cmd(cog, "cw_buy", ctx, 1))  # lot 1 = Kiroshi Optics Mk.1

        # Balance debited with correct amount
        cog.unbelievaboat.update_balance.assert_called_once()
        deduct_args = cog.unbelievaboat.update_balance.call_args[0]
        assert deduct_args[0] == 201
        assert deduct_args[1]["cash"] == -3000

        # Item lands in inventory (as dict)
        inv = _run(cog._load_inventory(201))
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)

        # Lot qty decremented
        state = _run(cog._load_state())
        lot = cog._lookup_lot(state["cw_wholesale_lots"], "Kiroshi Optics Mk.1")
        assert lot["qty_available"] == 1

        # BUY transaction recorded
        txs = _run(cog._load_tx())
        assert len(txs) == 1
        assert txs[0]["tx_type"] == "BUY"
        assert txs[0]["item"] == "Kiroshi Optics Mk.1"
        assert txs[0]["price"] == 3000

        # Success reply
        assert "✅" in ctx.send.call_args[0][0]

    def test_cash_and_bank_split(self, tmp_path, monkeypatch):
        """Price drawn from cash first, remainder from bank."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        # 1000 cash + 2500 bank = 3500 total, price = 3000
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 1000, "bank": 2500})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
        ctx = _ctx(author_id=202)

        _run(_cmd(cog, "cw_buy", ctx, 1))  # lot 1 = Kiroshi Optics Mk.1

        payload = cog.unbelievaboat.update_balance.call_args[0][1]
        assert payload["cash"] == -1000   # all cash used
        assert payload["bank"] == -2000   # remainder from bank

    def test_concurrent_sold_out_refunds(self, tmp_path, monkeypatch):
        """Lot drains to 0 between pre-check and lock → refund issued, inventory unchanged."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=self._lots())
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})

        call_count = 0

        async def deduct_then_drain(user_id, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Deduct succeeds, but drain the lot to simulate a race
                state = await cog._load_state()
                lot = cog._lookup_lot(state["cw_wholesale_lots"], "Kiroshi Optics Mk.1")
                lot["qty_available"] = 0
                await cog._save_state(state)
                return True
            return True  # refund call

        cog.unbelievaboat.update_balance = AsyncMock(side_effect=deduct_then_drain)
        ctx = _ctx(author_id=203)

        _run(_cmd(cog, "cw_buy", ctx, 1))  # lot 1 = Kiroshi Optics Mk.1

        assert call_count == 2  # deduct + refund
        inv = _run(cog._load_inventory(203))
        assert inv == []  # item was NOT added
        msg = ctx.send.call_args[0][0]
        assert "sold out" in msg or "refunded" in msg


# ------------------------------------------------------------------
# cw_sell tests
# ------------------------------------------------------------------

class TestCwSell:
    # New signature: (ctx, patient, inv_number: int, price: int, *, character_name: str)
    # Row 1 = first item in inventory list

    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))
        assert "server" in ctx.send.call_args[0][0]

    def test_price_zero_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 0, character_name="V"))
        assert "positive" in ctx.send.call_args[0][0]

    def test_negative_price_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, -100, character_name="V"))
        assert "positive" in ctx.send.call_args[0][0]

    def test_self_sell_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(111), 1, 3500, character_name="V"))
        assert "yourself" in ctx.send.call_args[0][0]

    def test_invalid_row_number(self, tmp_path, monkeypatch):
        """Out-of-range row number → clear error message."""
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(author_id=111)
        # No inventory seeded → row 1 is invalid
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_out_of_range_row_large(self, tmp_path, monkeypatch):
        """Row larger than inventory size is rejected."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 99, 3500, character_name="V"))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_patient_balance_fetch_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value=None)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))
        assert "Could not fetch" in ctx.send.call_args[0][0]

    def test_patient_cannot_afford(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 0})
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))
        assert "cannot afford" in ctx.send.call_args[0][0]

    def test_patient_deduct_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=False)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))
        assert "Failed to deduct" in ctx.send.call_args[0][0]
        # Item must still be in inventory
        inv = _run(cog._load_inventory(111))
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)

    def test_ripper_credit_fails_refunds_patient(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})

        call_count = 0

        async def update_side(user_id, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True   # patient deduct ok
            if call_count == 2:
                return False  # ripper credit fails
            return True       # patient refund ok

        cog.unbelievaboat.update_balance = AsyncMock(side_effect=update_side)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))

        # Three calls: deduct, credit attempt, refund
        assert call_count == 3
        msg = ctx.send.call_args[0][0]
        assert "Failed to credit" in msg or "refunded" in msg

    def test_success(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1", "Sandevistan Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 2000})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
        ctx = _ctx(author_id=111)
        patient = _make_member(999)

        _run(_cmd(cog, "cw_sell", ctx, patient, 1, 3500, character_name="V"))  # row 1 = Kiroshi

        # Two balance calls: patient deduct + ripper credit
        assert cog.unbelievaboat.update_balance.call_count == 2
        patient_call = cog.unbelievaboat.update_balance.call_args_list[0]
        assert patient_call[0][0] == 999
        ripper_call = cog.unbelievaboat.update_balance.call_args_list[1]
        assert ripper_call[0][0] == 111
        assert ripper_call[0][1]["cash"] == 3500

        # Item removed from inventory; other items untouched
        inv = _run(cog._load_inventory(111))
        names = _inv_names(inv)
        assert "Kiroshi Optics Mk.1" not in names
        assert "Sandevistan Mk.1" in names

        # SELL transaction recorded
        txs = _run(cog._load_tx())
        assert len(txs) == 1
        assert txs[0]["tx_type"] == "SELL"
        assert txs[0]["item"] == "Kiroshi Optics Mk.1"
        assert txs[0]["price"] == 3500
        assert txs[0]["ripperdoc_id"] == "111"
        assert txs[0]["patient_id"] == "999"

        # Success reply
        assert "✅" in ctx.send.call_args[0][0]

    def test_sell_drains_cash_first(self, tmp_path, monkeypatch):
        """Patient's cash is used before bank."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        # 1000 cash + 3000 bank; price = 3000
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 1000, "bank": 3000})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
        ctx = _ctx(author_id=111)

        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3000, character_name="V"))

        patient_payload = cog.unbelievaboat.update_balance.call_args_list[0][0][1]
        assert patient_payload["cash"] == -1000
        assert patient_payload["bank"] == -2000

    def test_item_stolen_under_lock_refunds_both(self, tmp_path, monkeypatch):
        """Item removed from inventory between pre-check and lock — both parties refunded."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, ["Kiroshi Optics Mk.1"])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})

        call_count = 0

        async def deduct_then_drain(user_id, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Patient deduct — steal item from inventory to simulate race
                await cog._save_inventory(111, [])
                return True
            return True

        cog.unbelievaboat.update_balance = AsyncMock(side_effect=deduct_then_drain)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))

        # At least 3 calls: deduct patient, refund patient, refund ripper
        assert call_count >= 3
        msg = ctx.send.call_args[0][0]
        assert "no longer in your inventory" in msg or "refunded" in msg or "changed" in msg


# ------------------------------------------------------------------
# cw_give / cw_take tests
# ------------------------------------------------------------------

class TestCwGiveTake:
    def test_cw_give_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_give", ctx, _make_member(500), item_name="Kiroshi Optics Mk.1"))
        assert "server" in ctx.send.call_args[0][0]

    def test_cw_give_adds_to_inventory(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_give", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        inv = _run(cog._load_inventory(500))
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)
        assert "✅" in ctx.send.call_args[0][0]

    def test_cw_give_appends_to_existing(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 500, ["Sandevistan Mk.1"])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_give", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        inv = _run(cog._load_inventory(500))
        assert "Sandevistan Mk.1" in _inv_names(inv)
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)

    def test_cw_take_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_take", ctx, _make_member(500), item_name="Item"))
        assert "server" in ctx.send.call_args[0][0]

    def test_cw_take_removes_item(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 500, ["Kiroshi Optics Mk.1", "Sandevistan Mk.1"])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_take", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        inv = _run(cog._load_inventory(500))
        names = _inv_names(inv)
        assert "Kiroshi Optics Mk.1" not in names
        assert "Sandevistan Mk.1" in names
        assert "✅" in ctx.send.call_args[0][0]

    def test_cw_take_prefix_match(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 500, ["Kiroshi Optics Mk.1"])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_take", ctx, ripperdoc, item_name="Kiroshi"))
        inv = _run(cog._load_inventory(500))
        assert inv == []

    def test_cw_take_not_found(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 500, ["Sandevistan Mk.1"])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_take", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        assert "not found" in ctx.send.call_args[0][0]
        # Inventory unchanged
        inv = _run(cog._load_inventory(500))
        assert "Sandevistan Mk.1" in _inv_names(inv)

    def test_cw_take_fifo_removes_oldest_by_purchased_at(self, tmp_path, monkeypatch):
        """cw_take must select the item with the oldest purchased_at (FIFO), not list order."""
        cog = _make_cog(tmp_path, monkeypatch)
        # Seed with two Kiroshi items; newer one is listed first
        _seed_inventory(cog, 500, [
            {"item_id": "aaa", "name": "Kiroshi Optics Mk.1", "price_paid": 500, "purchased_at": "2026-04-10"},
            {"item_id": "bbb", "name": "Kiroshi Optics Mk.1", "price_paid": 500, "purchased_at": "2026-04-01"},
        ])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_take", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        inv = _run(cog._load_inventory(500))
        # The oldest (bbb, 2026-04-01) should have been removed; newer (aaa) remains
        remaining_ids = [e.get("item_id") for e in inv if isinstance(e, dict)]
        assert "aaa" in remaining_ids
        assert "bbb" not in remaining_ids

    def test_cw_take_fifo_none_dates_sorted_last(self, tmp_path, monkeypatch):
        """Items with purchased_at=None are treated as newest (sort last in FIFO)."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 500, [
            {"item_id": "no_date", "name": "Kiroshi Optics Mk.1", "price_paid": 500, "purchased_at": None},
            {"item_id": "dated",   "name": "Kiroshi Optics Mk.1", "price_paid": 500, "purchased_at": "2026-04-01"},
        ])
        ctx = _ctx()
        ripperdoc = _make_member(500, "Doc")
        _run(_cmd(cog, "cw_take", ctx, ripperdoc, item_name="Kiroshi Optics Mk.1"))
        inv = _run(cog._load_inventory(500))
        remaining_ids = [e.get("item_id") for e in inv if isinstance(e, dict)]
        # dated item (older) was removed first; no_date item remains
        assert "no_date" in remaining_ids
        assert "dated" not in remaining_ids


# ------------------------------------------------------------------
# cw_wh_restock tests
# ------------------------------------------------------------------

class TestCwWhRestock:
    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_wh_restock", ctx))
        assert "server" in ctx.send.call_args[0][0]

    def test_empty_catalog(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_restock", ctx))
        assert "empty" in ctx.send.call_args[0][0]

    def test_generates_lots_and_saves(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_restock", ctx, seed=42))
        state = _run(cog._load_state())
        assert "cw_wholesale_lots" in state
        assert len(state["cw_wholesale_lots"]) > 0
        first_msg = ctx.send.call_args_list[0][0][0]
        assert "✅" in first_msg

    def test_deterministic_with_seed(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_restock", ctx, seed=7))
        names_a = [l["item_name"] for l in _run(cog._load_state())["cw_wholesale_lots"]]
        _run(_cmd(cog, "cw_wh_restock", ctx, seed=7))
        names_b = [l["item_name"] for l in _run(cog._load_state())["cw_wholesale_lots"]]
        assert names_a == names_b

    def test_updates_last_restock_date(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_restock", ctx, seed=1))
        state = _run(cog._load_state())
        assert "last_cw_restock_sunday" in state.get("settings", {})


# ------------------------------------------------------------------
# cw_wh_add tests
# ------------------------------------------------------------------

class TestCwWhAdd:
    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_wh_add", ctx, 2, item_name="Kiroshi Optics Mk.1"))
        assert "server" in ctx.send.call_args[0][0]

    def test_zero_qty_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_add", ctx, 0, item_name="Kiroshi Optics Mk.1"))
        assert "positive" in ctx.send.call_args[0][0]

    def test_negative_qty_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_add", ctx, -5, item_name="Kiroshi Optics Mk.1"))
        assert "positive" in ctx.send.call_args[0][0]

    def test_item_not_in_catalog(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=[])
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_add", ctx, 2, item_name="Totally Unknown Item"))
        assert "not found" in ctx.send.call_args[0][0]

    def test_creates_new_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog, lots=[])
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_add", ctx, 3, item_name="Kiroshi Optics Mk.1"))
        state = _run(cog._load_state())
        lots = state["cw_wholesale_lots"]
        assert len(lots) == 1
        assert lots[0]["item_name"] == "Kiroshi Optics Mk.1"
        assert lots[0]["qty_available"] == 3
        assert lots[0]["unit_cost"] == 3000
        assert "✅" in ctx.send.call_args[0][0]

    def test_increments_existing_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        existing = [
            {"lot_id": "cwlot-old", "item_name": "Kiroshi Optics Mk.1", "unit_cost": 3000, "qty_available": 1}
        ]
        _seed_state(cog, lots=existing)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_add", ctx, 4, item_name="Kiroshi Optics Mk.1"))
        state = _run(cog._load_state())
        lot = cog._lookup_lot(state["cw_wholesale_lots"], "Kiroshi Optics Mk.1")
        assert lot["qty_available"] == 5  # 1 + 4
        assert len(state["cw_wholesale_lots"]) == 1  # no duplicate created


# ------------------------------------------------------------------
# cw_wh_remove tests
# ------------------------------------------------------------------

class TestCwWhRemove:
    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_wh_remove", ctx, item_name="Kiroshi Optics Mk.1"))
        assert "server" in ctx.send.call_args[0][0]

    def test_item_not_found(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_state(cog, lots=[])
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_remove", ctx, item_name="Unknown Item"))
        assert "not found" in ctx.send.call_args[0][0]

    def test_removes_item(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lots = [
            {"lot_id": "cwlot-001", "item_name": "Kiroshi Optics Mk.1", "unit_cost": 3000, "qty_available": 2},
            {"lot_id": "cwlot-002", "item_name": "Sandevistan Mk.1", "unit_cost": 8000, "qty_available": 1},
        ]
        _seed_state(cog, lots=lots)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_remove", ctx, item_name="Kiroshi Optics Mk.1"))
        state = _run(cog._load_state())
        remaining = state["cw_wholesale_lots"]
        assert len(remaining) == 1
        assert remaining[0]["item_name"] == "Sandevistan Mk.1"
        assert "✅" in ctx.send.call_args[0][0]

    def test_prefix_match_removes(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        lots = [
            {"lot_id": "cwlot-001", "item_name": "Kiroshi Optics Mk.1", "unit_cost": 3000, "qty_available": 2},
        ]
        _seed_state(cog, lots=lots)
        ctx = _ctx()
        _run(_cmd(cog, "cw_wh_remove", ctx, item_name="Kiro"))
        state = _run(cog._load_state())
        assert state["cw_wholesale_lots"] == []


# ------------------------------------------------------------------
# auto_cw_restock_if_due tests
# ------------------------------------------------------------------

class TestAutoCwRestockIfDue:
    def test_runs_when_not_yet_done(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog)
        now = datetime(2026, 4, 7, tzinfo=timezone.utc)
        result = _run(cog.auto_cw_restock_if_due(now))
        assert result is True
        state = _run(cog._load_state())
        assert len(state.get("cw_wholesale_lots", [])) > 0

    def test_skips_when_already_done(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(
            cog,
            lots=[{"item_name": "OldLot"}],
            extra={"settings": {"last_cw_restock_sunday": "2026-04-07"}},
        )
        now = datetime(2026, 4, 7, tzinfo=timezone.utc)
        result = _run(cog.auto_cw_restock_if_due(now))
        assert result is True
        state = _run(cog._load_state())
        assert state["cw_wholesale_lots"][0]["item_name"] == "OldLot"

    def test_returns_false_when_catalog_empty(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        cog = _make_cog(tmp_path, monkeypatch)
        now = datetime(2026, 4, 7, tzinfo=timezone.utc)
        result = _run(cog.auto_cw_restock_if_due(now))
        assert result is False

    def test_returns_false_when_system_disabled(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        now = datetime(2026, 4, 7, tzinfo=timezone.utc)
        result = _run(cog.auto_cw_restock_if_due(now))
        assert result is False

    def test_different_weeks_both_restock(self, tmp_path, monkeypatch):
        """Two different Monday dates each trigger a fresh restock."""
        from datetime import datetime, timezone
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_catalog(cog)
        _seed_state(cog)

        result_a = _run(cog.auto_cw_restock_if_due(datetime(2026, 4, 7, tzinfo=timezone.utc)))
        assert result_a is True

        result_b = _run(cog.auto_cw_restock_if_due(datetime(2026, 4, 14, tzinfo=timezone.utc)))
        assert result_b is True
        state = _run(cog._load_state())
        assert state["settings"]["last_cw_restock_sunday"] == "2026-04-14"


# ------------------------------------------------------------------
# cw_tx tests
# ------------------------------------------------------------------

class TestCwTx:
    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_tx", ctx))
        assert "server" in ctx.send.call_args[0][0]

    def test_empty_transactions(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx()
        _run(_cmd(cog, "cw_tx", ctx))
        assert "No cyberware transactions" in ctx.send.call_args[0][0]

    def test_shows_own_tx_as_ripperdoc(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        txs = [
            {
                "tx_id": "a", "tx_type": "BUY", "ts": "2026-04-01T10:00:00",
                "ripperdoc_id": "111", "ripperdoc_name": "Me",
                "item": "Kiroshi", "price": 3000,
            },
            {
                "tx_id": "b", "tx_type": "BUY", "ts": "2026-04-01T11:00:00",
                "ripperdoc_id": "222", "ripperdoc_name": "Other",
                "item": "Sandevistan", "price": 8000,
            },
        ]
        for tx in txs:
            _run(cog._append_tx(tx))

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_tx", ctx))

        # Should have sent an embed
        call_kwargs = ctx.send.call_args[1]
        assert "embed" in call_kwargs
        embed = call_kwargs["embed"]
        # Footer should say 1 of 1 (only user 111's tx, not user 222's)
        assert "1" in embed.footer.text

    def test_no_tx_for_requesting_user(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _run(cog._append_tx({
            "tx_id": "a", "tx_type": "BUY", "ts": "2026-04-01T10:00:00",
            "ripperdoc_id": "222", "ripperdoc_name": "Other",
            "item": "Kiroshi", "price": 3000,
        }))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_tx", ctx))
        msg = ctx.send.call_args[0][0]
        assert "No transactions found" in msg


# ------------------------------------------------------------------
# _load_inventory migration tests
# ------------------------------------------------------------------

class TestLoadInventoryMigration:
    def test_dict_entry_missing_item_id_gets_uuid(self, tmp_path, monkeypatch):
        """Legacy dict entries without item_id are assigned a UUID and saved back."""
        cog = _make_cog(tmp_path, monkeypatch)
        # Manually write a dict entry without item_id
        legacy = [{"name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-01-01"}]
        inv_file = cog.inventory_dir / "111.json"
        inv_file.write_text(json.dumps(legacy))

        inv = _run(cog._load_inventory(111))

        assert len(inv) == 1
        assert "item_id" in inv[0]
        assert inv[0]["item_id"]  # non-empty UUID string

        # Verify migrated UUID was saved back to file
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
        """_load_inventory assigns UUID to dict entry → cw_sell filter removes correct item."""
        cog = _make_cog(tmp_path, monkeypatch)
        # Write a legacy dict entry without item_id
        legacy = [{"name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}]
        inv_file = cog.inventory_dir / "111.json"
        inv_file.write_text(json.dumps(legacy))

        # Load and migrate — item now has a UUID
        inv = _run(cog._load_inventory(111))
        assert "item_id" in inv[0]

        # Simulate the cw_sell remove step: filter by item_id
        item_id = inv[0]["item_id"]
        updated = [it for it in inv if it.get("item_id") != item_id]
        assert updated == []  # item was removed, NOT duplicated


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
        # Both have same date → one group; earliest timestamp first
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
        # Different dates → two groups, sorted by (name, price, date)
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
        # Alphabetical: Kiroshi before Sandevistan
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
        # Should be fully alphabetical regardless of qty_available
        assert [l["item_name"] for l in ordered] == ["Berserk", "Kiroshi", "Sandevistan"]
        # Kiroshi (sold out) is lot #2 — NOT moved to the end


# ------------------------------------------------------------------
# Additional cw_sell tests — FIFO + data integrity
# ------------------------------------------------------------------

class TestCwSellFifo:
    """FIFO consumption and pi_add_item data integrity tests for cw_sell."""

    def test_fifo_picks_earliest_item(self, tmp_path, monkeypatch):
        """When two identical items exist, the one with the earliest purchased_at is consumed."""
        cog = _make_cog(tmp_path, monkeypatch)
        # Seed two identical Kiroshi items — 'early' has earlier timestamp
        inv = [
            {"item_id": "late",  "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-02"},
            {"item_id": "early", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"},
        ]
        _seed_inventory(cog, 111, inv)
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        captured_item_id = []
        original_pi_add_item = AsyncMock(return_value=True)

        async def capturing_pi_add_item(item_dict):
            captured_item_id.append(item_dict.get("item_id"))
            return True

        monkeypatch.setattr("NightCityBot.cogs.cyberware_shop.pi_add_item", capturing_pi_add_item)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))

        # FIFO: 'early' should be consumed, 'late' should remain
        assert captured_item_id == ["early"]
        inv_after = _run(cog._load_inventory(111))
        remaining_ids = [e.get("item_id") for e in inv_after]
        assert "early" not in remaining_ids
        assert "late" in remaining_ids

    def test_pi_add_item_failure_aborts_and_refunds(self, tmp_path, monkeypatch):
        """If pi_add_item fails, item stays in inventory and both parties are refunded."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, [
            {"item_id": "abc", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}
        ])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        monkeypatch.setattr("NightCityBot.cogs.cyberware_shop.pi_add_item", AsyncMock(return_value=False))

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))

        # Item STILL in inventory (was not removed since DB write failed)
        inv = _run(cog._load_inventory(111))
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)

        # At least 4 balance calls: deduct patient, credit ripper, refund patient, refund ripper
        assert cog.unbelievaboat.update_balance.call_count >= 4
        msg = ctx.send.call_args[0][0]
        assert "Failed" in msg or "refunded" in msg

    def test_pi_add_item_called_before_remove(self, tmp_path, monkeypatch):
        """pi_add_item is called inside the lock before removing from stock."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, [
            {"item_id": "xyz", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}
        ])
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        pi_called = []

        async def tracking_pi_add_item(item_dict):
            # Check ripperdoc still has the item when pi_add_item is called
            inv = await cog._load_inventory(111)
            pi_called.append(len(inv))
            return True

        monkeypatch.setattr("NightCityBot.cogs.cyberware_shop.pi_add_item", tracking_pi_add_item)

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_sell", ctx, _make_member(999), 1, 3500, character_name="V"))

        # pi_add_item was called and ripperdoc still had 1 item at that point
        assert pi_called == [1]
        # After success, item is gone
        inv = _run(cog._load_inventory(111))
        assert inv == []


# ------------------------------------------------------------------
# cw_install tests
# ------------------------------------------------------------------

class TestCwInstall:
    """Tests for the !cw_install command."""

    def test_dm_guard(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "cw_install", ctx, _make_member(999), "V", 1))
        assert "server" in ctx.send.call_args[0][0]

    def test_invalid_row(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "cw_install", ctx, _make_member(999), "V", 5))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_success_removes_item_and_records_tx(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, [
            {"item_id": "inst1", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"},
            {"item_id": "inst2", "name": "Sandevistan Mk.1",    "price_paid": 8000, "purchased_at": "2026-04-01"},
        ])
        ctx = _ctx(author_id=111)
        patient = _make_member(999)

        _run(_cmd(cog, "cw_install", ctx, patient, "V", 1))  # row 1 = Kiroshi (alpha)

        inv = _run(cog._load_inventory(111))
        names = _inv_names(inv)
        assert "Kiroshi Optics Mk.1" not in names
        assert "Sandevistan Mk.1" in names

        txs = _run(cog._load_tx())
        assert txs[0]["tx_type"] == "INSTALL"
        assert txs[0]["item"] == "Kiroshi Optics Mk.1"
        assert "✅" in ctx.send.call_args[0][0]

    def test_pi_add_item_called_with_correct_args(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, [
            {"item_id": "myid", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}
        ])
        ctx = _ctx(author_id=111)
        patient = _make_member(888)

        captured = []

        async def capture(d):
            captured.append(d)
            return True

        monkeypatch.setattr("NightCityBot.cogs.cyberware_shop.pi_add_item", capture)
        _run(_cmd(cog, "cw_install", ctx, patient, "Johnny", 1))

        assert len(captured) == 1
        assert captured[0]["item_id"] == "myid"
        assert captured[0]["owner_id"] == "888"
        assert captured[0]["character_name"] == "Johnny"
        assert captured[0]["item_type"] == "cyberware"

    def test_pi_add_item_failure_aborts(self, tmp_path, monkeypatch):
        """If pi_add_item fails, inventory is unchanged."""
        cog = _make_cog(tmp_path, monkeypatch)
        _seed_inventory(cog, 111, [
            {"item_id": "keep", "name": "Kiroshi Optics Mk.1", "price_paid": 3000, "purchased_at": "2026-04-01"}
        ])
        monkeypatch.setattr("NightCityBot.cogs.cyberware_shop.pi_add_item", AsyncMock(return_value=False))
        ctx = _ctx(author_id=111)

        _run(_cmd(cog, "cw_install", ctx, _make_member(999), "V", 1))

        inv = _run(cog._load_inventory(111))
        assert "Kiroshi Optics Mk.1" in _inv_names(inv)
        msg = ctx.send.call_args[0][0]
        assert "Failed" in msg
