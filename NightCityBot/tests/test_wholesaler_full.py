"""Comprehensive wholesaler cog tests covering all command flows end-to-end."""

import asyncio
import json
import random
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from openpyxl import Workbook

from NightCityBot.cogs.wholesaler import WholesalerCog


def _make_cog(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.UNBELIEVABOAT_API_TOKEN", "test-token")
    monkeypatch.setattr("config.GUILD_ID", 111)
    monkeypatch.setattr("config.WHOLESALER_ADMIN_ROLE_IDS", "900")
    monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", "800")
    monkeypatch.setattr("config.WHOLESALER_AUDIT_CHANNEL_ID", 0)
    monkeypatch.setattr("config.WHOLESALER_GOOGLE_SHEET_XLSX_URL", "")
    monkeypatch.setattr("config.WHOLESALER_XLSX_PATH", "")
    monkeypatch.setattr("config.WHOLESALER_MASTER_SHEET_NAME", "Master Gun List")

    cog = WholesalerCog.__new__(WholesalerCog)
    cog.bot = MagicMock()
    cog.bot.get_cog = MagicMock(return_value=None)
    cog.unbelievaboat = MagicMock()
    cog.lock = asyncio.Lock()
    cog.data_dir = tmp_path
    cog.state_file = tmp_path / "state.json"
    cog.store_state_file = tmp_path / "stores.json"
    cog.wholesale_inventory_file = tmp_path / "inventory" / "wholesale.json"
    cog.store_inventory_dir = tmp_path / "inventory" / "stores"
    cog.tx_file = tmp_path / "transactions.json"
    cog.sheet_cache_path = tmp_path / "master_sheet_latest.xlsx"
    cog.DEFAULT_RESTOCK_SETTINGS = WholesalerCog.DEFAULT_RESTOCK_SETTINGS
    cog.WEAPON_TYPES = WholesalerCog.WEAPON_TYPES
    cog.WEAPON_TYPE_PATTERNS = WholesalerCog.WEAPON_TYPE_PATTERNS

    cog._audit_send = AsyncMock()

    return cog


def _make_xlsx(tmp_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Master Gun List"
    ws.append(["Shop A", "Gun Name", "Type", "Mag Size", "Price", "Cyberware Needed"])
    ws.append([None, "Tamayura", "Power (M)", 8, 1200, None])
    ws.append([None, "Slaught-O-Matic", "Power (L)", 36, 100, None])
    ws.append([None, "Liberty", "Power (M)", 14, 2000, None])
    ws.append([None, "M-10AF Lexington", "Power (L)", 20, 1000, None])
    ws.append([None, "Nue", "Power (M)", 10, 1300, None])
    ws.append([None, "Revolvers", "Type", None, "Price New", None])
    ws.append([None, "DR-5 Nova", "Power (H)", 6, 4000, None])
    ws.append([None, "Overture", "Power (M)", 6, 3000, None])
    ws.append([None, "Submachine Guns", "Type", None, "Price New", None])
    ws.append([None, "Guillotine", "Power (L)", 30, 3000, None])
    ws.append([None, "Shigure", "Power (L)", 24, 5000, None])
    ws.append([None, "Shotguns", "Type", None, "Price New", None])
    ws.append([None, "Crusher (6 pellets) (Auto)", "Power (L)", 8, 6000, None])
    ws.append([None, "Assault Rifles", "Type", None, "Price New", None])
    ws.append([None, "D5 Copperhead", "Power (L)", 30, 4000, None])
    ws.append([None, "Kyubi", "Power (M)", 25, 6000, None])
    xlsx = tmp_path / "guns.xlsx"
    wb.save(xlsx)
    return xlsx


def _member(member_id, is_admin=False, role_id=0):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.mention = f"<@{member_id}>"
    member.display_name = f"User{member_id}"
    perms = MagicMock()
    perms.administrator = is_admin
    member.guild_permissions = perms
    role = MagicMock()
    role.id = role_id
    member.roles = [role]
    return member


def _admin():
    return _member(1001, is_admin=True, role_id=900)


def _store_owner():
    return _member(2001, is_admin=False, role_id=800)


def _buyer():
    return _member(3001, is_admin=False, role_id=0)


def _ctx(member, guild_id=111):
    ctx = AsyncMock()
    ctx.author = member
    ctx.guild = MagicMock()
    ctx.guild.id = guild_id
    ctx.guild.get_member = MagicMock(return_value=member)
    return ctx


async def _cmd(cog, method_name, ctx, *args, **kwargs):
    cmd = getattr(cog, method_name)
    if hasattr(cmd, 'callback'):
        return await cmd.callback(cog, ctx, *args, **kwargs)
    return await cmd(ctx, *args, **kwargs)


class TestSheetParsing:
    def test_section_headers_assign_weapon_types(self, tmp_path):
        xlsx = _make_xlsx(tmp_path)
        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        types_by_name = {g["gun_name"]: g["weapon_type"] for g in guns}
        assert types_by_name["Tamayura"] == "pistol"
        assert types_by_name["Nue"] == "pistol"
        assert types_by_name["DR-5 Nova"] == "revolver"
        assert types_by_name["Overture"] == "revolver"
        assert types_by_name["Guillotine"] == "submachine_gun"
        assert types_by_name["Crusher (6 pellets) (Auto)"] == "shotgun"
        assert types_by_name["D5 Copperhead"] == "assault_rifle"
        assert types_by_name["Kyubi"] == "assault_rifle"

    def test_section_headers_are_not_included_as_guns(self, tmp_path):
        xlsx = _make_xlsx(tmp_path)
        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        names = {g["gun_name"] for g in guns}
        assert "Revolvers" not in names
        assert "Submachine Guns" not in names
        assert "Shotguns" not in names
        assert "Assault Rifles" not in names

    def test_all_guns_have_valid_levels(self, tmp_path):
        xlsx = _make_xlsx(tmp_path)
        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        for g in guns:
            assert g["gun_level"] in ("L", "M", "H"), f"{g['gun_name']} has invalid level {g['gun_level']}"

    def test_all_guns_have_positive_prices(self, tmp_path):
        xlsx = _make_xlsx(tmp_path)
        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        for g in guns:
            assert g["price_new"] > 0, f"{g['gun_name']} has price {g['price_new']}"

    def test_zero_price_guns_are_filtered(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "Master Gun List"
        ws.append(["Gun Name", "Type", "Mag Size", "Price", "Cyberware"])
        ws.append(["Good Gun", "Power (L)", 10, 500, ""])
        ws.append(["Free Gun", "Power (M)", 10, 0, ""])
        ws.append(["No Price", "Power (H)", 10, None, ""])
        xlsx = tmp_path / "prices.xlsx"
        wb.save(xlsx)
        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        assert len(guns) == 1
        assert guns[0]["gun_name"] == "Good Gun"


class TestRestockLotConsolidation:
    def test_duplicate_guns_are_merged_into_single_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        guns = [{"gun_name": "Nue", "gun_level": "M", "price_new": 1300, "weapon_type": "pistol"}]
        cfg = {
            "total_lots": 50,
            "lots_L": 0, "lots_M": 5, "lots_H": 0,
            "qty_min_L": 1, "qty_max_L": 1,
            "qty_min_M": 2, "qty_max_M": 2,
            "qty_min_H": 1, "qty_max_H": 1,
        }
        lots, totals = cog._generate_restock_lots(guns, cfg, random.Random(42))
        assert len(lots) == 1
        assert lots[0]["gun_name"] == "Nue"
        assert lots[0]["qty_available"] == 10
        assert totals["M"] == 10

    def test_different_guns_stay_separate(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        guns = [
            {"gun_name": "Nue", "gun_level": "M", "price_new": 1300, "weapon_type": "pistol"},
            {"gun_name": "Liberty", "gun_level": "M", "price_new": 2000, "weapon_type": "pistol"},
        ]
        cfg = {
            "total_lots": 50,
            "lots_L": 0, "lots_M": 4, "lots_H": 0,
            "qty_min_L": 1, "qty_max_L": 1,
            "qty_min_M": 1, "qty_max_M": 1,
            "qty_min_H": 1, "qty_max_H": 1,
        }
        lots, _ = cog._generate_restock_lots(guns, cfg, random.Random(42))
        assert len(lots) <= 2
        for lot in lots:
            assert lot["weapon_type"] == "pistol"

    def test_same_gun_different_levels_stay_separate(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        guns = [
            {"gun_name": "TestGun", "gun_level": "L", "price_new": 100, "weapon_type": "pistol"},
            {"gun_name": "TestGun", "gun_level": "H", "price_new": 500, "weapon_type": "pistol"},
        ]
        cfg = {
            "total_lots": 50,
            "lots_L": 2, "lots_M": 0, "lots_H": 2,
            "qty_min_L": 1, "qty_max_L": 1,
            "qty_min_M": 1, "qty_max_M": 1,
            "qty_min_H": 1, "qty_max_H": 1,
        }
        lots, _ = cog._generate_restock_lots(guns, cfg, random.Random(42))
        assert len(lots) == 2
        levels = {l["gun_level"] for l in lots}
        assert levels == {"L", "H"}


class TestStateManagement:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        state = {
            "wholesale_lots": [
                {"lot_id": "lot-001", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 5}
            ],
            "stores": {
                "111:2001": {
                    "owner_id": 2001,
                    "lots": [
                        {"lot_id": "lot-001", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 3}
                    ]
                }
            },
            "shop_registry": {"shop1": 2001},
            "settings": {},
        }

        async def _run():
            ok = await cog._save_state(state)
            assert ok is True
            loaded = await cog._load_state()
            return loaded

        loaded = asyncio.run(_run())
        assert loaded["wholesale_lots"][0]["lot_id"] == "lot-001"
        assert loaded["wholesale_lots"][0]["weapon_type"] == "pistol"
        assert loaded["stores"]["111:2001"]["lots"][0]["qty_remaining"] == 3
        assert loaded["shop_registry"]["shop1"] == 2001

    def test_empty_state_initializes_defaults(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)

        async def _run():
            return await cog._load_state()

        state = asyncio.run(_run())
        assert "wholesale_lots" in state
        assert isinstance(state["wholesale_lots"], list)

    def test_save_creates_per_store_inventory_files(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        state = {
            "wholesale_lots": [],
            "stores": {
                "111:2001": {
                    "owner_id": 2001,
                    "lots": [{"lot_id": "s1", "gun_name": "Nue", "qty_remaining": 3}]
                },
                "111:2002": {
                    "owner_id": 2002,
                    "lots": [{"lot_id": "s2", "gun_name": "Liberty", "qty_remaining": 1}]
                },
            },
            "shop_registry": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)

        asyncio.run(_run())
        assert cog.store_inventory_dir.exists()
        store_files = list(cog.store_inventory_dir.glob("*.json"))
        assert len(store_files) == 2


class TestWholesaleBuy:
    def test_buy_deducts_funds_and_moves_to_store(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 10}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 3)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["wholesale_lots"][0]["qty_available"] == 7
        store_id = f"111:{owner.id}"
        assert store_id in result["stores"]
        store_lots = result["stores"][store_id]["lots"]
        assert len(store_lots) == 1
        assert store_lots[0]["qty_remaining"] == 3
        assert store_lots[0]["weapon_type"] == "pistol"

    def test_buy_insufficient_funds_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 10}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 3)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["wholesale_lots"][0]["qty_available"] == 10
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("insufficient" in m.lower() or "afford" in m.lower() or "not enough" in m.lower() for m in sent_messages)

    def test_buy_invalid_lot_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-nonexistent", 1)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("not found" in m.lower() or "invalid" in m.lower() or "unavailable" in m.lower() for m in sent_messages)

    def test_buy_exceeding_available_qty_fails(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 2}
            ],
            "stores": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 5)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("only" in m.lower() or "insufficient" in m.lower() or "available" in m.lower() for m in sent_messages)

    def test_buy_zero_qty_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 5}
            ],
            "stores": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 0)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("must be" in m.lower() or "> 0" in m or "positive" in m.lower() or "at least" in m.lower() for m in sent_messages)

    def test_buy_entire_lot_sets_qty_to_zero(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-full", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 3}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-full", 3)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["wholesale_lots"][0]["qty_available"] == 0


class TestPlayerSale:
    def test_sell_deducts_buyer_credits_seller(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 2, 3000)
            return await cog._load_state()

        result = asyncio.run(_run())
        store_lots = result["stores"][f"111:{seller.id}"]["lots"]
        assert store_lots[0]["qty_remaining"] == 3
        assert cog.unbelievaboat.update_balance.call_count == 2

    def test_sell_buyer_insufficient_funds(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10, "bank": 0})

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 2, 3000)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["stores"][f"111:{seller.id}"]["lots"][0]["qty_remaining"] == 5

    def test_sell_seller_payout_fails_marks_pending(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        call_count = 0
        async def _update_balance(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True
            return False
        cog.unbelievaboat.update_balance = _update_balance

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 2, 3000)
            return await cog._load_state()

        result = asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("pending" in m.lower() or "payout" in m.lower() for m in sent_messages)

    def test_sell_invalid_lot_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {"owner_id": seller.id, "lots": []}
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-fake", 1, 1000)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("not found" in m.lower() or "invalid" in m.lower() or "no lot" in m.lower() for m in sent_messages)

    def test_sell_zero_qty_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 0, 1000)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("must be" in m.lower() or "> 0" in m or "positive" in m.lower() or "at least" in m.lower() for m in sent_messages)

    def test_sell_zero_price_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 1, 0)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("must be" in m.lower() or "> 0" in m or "positive" in m.lower() or "at least" in m.lower() for m in sent_messages)

    def test_sell_exceeding_store_qty_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 2}
                    ]
                }
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 5, 1000)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("only" in m.lower() or "insufficient" in m.lower() or "available" in m.lower() for m in sent_messages)


class TestPermissions:
    def test_non_store_owner_cannot_buy(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        ctx = _ctx(person)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 10}
            ],
            "stores": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 1)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("store owner" in m.lower() for m in sent_messages)

    def test_non_store_owner_cannot_sell(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        non_owner = _buyer()
        buyer = _buyer()
        ctx = _ctx(non_owner)

        async def _run():
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-x", 1, 1000)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("store owner" in m.lower() for m in sent_messages)

    def test_admin_check_accepts_administrator_permission(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        assert cog._is_admin(admin) is True

    def test_admin_check_rejects_regular_user(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        assert cog._is_admin(person) is False

    def test_store_owner_check_with_role(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        assert cog._is_store_owner(owner) is True

    def test_store_owner_check_without_role(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        assert cog._is_store_owner(person) is False

    def test_admin_does_not_bypass_store_owner_check(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _member(1001, is_admin=True, role_id=900)
        assert cog._is_store_owner(admin) is (900 in cog._coerce_role_ids("800"))


class TestShopRegistry:
    def test_setshop_binds_owner(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        owner = _store_owner()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_setshop", ctx, "shop1", owner)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["shop_registry"]["shop1"] == owner.id

    def test_setshop_non_admin_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_setshop", ctx, "shop1", owner)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("admin" in m.lower() for m in sent_messages)

    def test_setshop_overwrites_existing_binding(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        owner1 = _store_owner()
        owner2 = _member(2002, is_admin=False, role_id=800)
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "shop_registry": {"shop1": owner1.id}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_setshop", ctx, "shop1", owner2)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["shop_registry"]["shop1"] == owner2.id


class TestAdminStockManagement:
    def test_wh_add_creates_wholesale_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_add", ctx, "TestGun", "L", 500, 10)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert len(result["wholesale_lots"]) == 1
        lot = result["wholesale_lots"][0]
        assert lot["gun_name"] == "TestGun"
        assert lot["gun_level"] == "L"
        assert lot["unit_cost"] == 500
        assert lot["qty_available"] == 10

    def test_wh_add_derives_weapon_type(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_add", ctx, "Test Shotgun X1", "M", 1000, 5)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["wholesale_lots"][0]["weapon_type"] == "shotgun"

    def test_store_add_creates_store_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        owner = _store_owner()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "store_add", ctx, owner, "Nue", "M", 1300, 5)
            return await cog._load_state()

        result = asyncio.run(_run())
        store_id = f"111:{owner.id}"
        assert store_id in result["stores"]
        assert result["stores"][store_id]["lots"][0]["gun_name"] == "Nue"
        assert result["stores"][store_id]["lots"][0]["qty_remaining"] == 5

    def test_wh_add_invalid_level_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_add", ctx, "TestGun", "X", 500, 10)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("l/m/h" in m.lower() for m in sent_messages)

    def test_wh_add_zero_cost_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_add", ctx, "TestGun", "L", 0, 10)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("positive" in m.lower() or "> 0" in m or "must be" in m.lower() for m in sent_messages)

    def test_wh_add_non_admin_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        ctx = _ctx(person)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_add", ctx, "TestGun", "L", 500, 10)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("admin" in m.lower() for m in sent_messages)

    def test_store_add_non_admin_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        owner = _store_owner()
        ctx = _ctx(person)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "store_add", ctx, owner, "Nue", "M", 1300, 5)

        asyncio.run(_run())
        sent_messages = [str(c) for c in ctx.send.call_args_list]
        assert any("admin" in m.lower() for m in sent_messages)

    def test_wh_clear_inventory_removes_lots_keeps_stores(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)
        owner = _store_owner()

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-1", "gun_name": "Gun", "gun_level": "L",
                 "weapon_type": "pistol", "unit_cost": 100, "qty_available": 5}
            ],
            "stores": {
                f"111:{owner.id}": {
                    "owner_id": owner.id,
                    "lots": [{"lot_id": "store-1", "gun_name": "Gun", "gun_level": "L",
                              "weapon_type": "pistol", "unit_cost": 100, "qty_remaining": 3}]
                }
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_clear_inventory", ctx)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert len(result["wholesale_lots"]) == 0
        assert len(result["stores"][f"111:{owner.id}"]["lots"]) == 1

    def test_wh_clear_non_admin_rejected(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        person = _buyer()
        ctx = _ctx(person)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-1", "gun_name": "Gun", "gun_level": "L",
                 "weapon_type": "pistol", "unit_cost": 100, "qty_available": 5}
            ],
            "stores": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_clear_inventory", ctx)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert len(result["wholesale_lots"]) == 1


class TestTransactionLog:
    def test_wh_sell_creates_transaction_record(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()
        ctx = _ctx(seller)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-sell", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5}
                    ]
                }
            },
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_sell", ctx, buyer, "TestChar", "lot-sell", 1, 2000)

        asyncio.run(_run())
        assert cog.tx_file.exists()
        txs = json.loads(cog.tx_file.read_text(encoding="utf-8"))
        assert len(txs) >= 1
        tx = txs[-1]
        assert tx["gun_name"] == "Nue"
        assert tx["qty"] == 1
        assert tx["total_price"] == 2000
        assert tx["buyer_id"] == buyer.id
        assert tx["seller_id"] == seller.id
        assert tx["character_name"] == "TestChar"

    def test_wh_buy_creates_transaction_record(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-test", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 10}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_buy", ctx, "lot-test", 2)

        asyncio.run(_run())
        assert cog.tx_file.exists()
        txs = json.loads(cog.tx_file.read_text(encoding="utf-8"))
        assert len(txs) >= 1


class TestFullLifecycle:
    def test_restock_buy_sell_flow(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        xlsx = _make_xlsx(tmp_path)
        cog.sheet_cache_path = xlsx

        guns = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")
        cfg = {
            "total_lots": 50,
            "lots_L": 3, "lots_M": 3, "lots_H": 1,
            "qty_min_L": 5, "qty_max_L": 5,
            "qty_min_M": 3, "qty_max_M": 3,
            "qty_min_H": 2, "qty_max_H": 2,
        }
        lots, totals = cog._generate_restock_lots(guns, cfg, random.Random(99))

        assert len(lots) > 0
        for lot in lots:
            assert lot["weapon_type"] != ""
            assert lot["gun_level"] in ("L", "M", "H")
            assert lot["unit_cost"] > 0
            assert lot["qty_available"] > 0

        unique_keys = {f"{l['gun_name']}|{l['gun_level']}|{l['unit_cost']}" for l in lots}
        assert len(unique_keys) == len(lots), "Lots should be deduplicated"

        state = {"wholesale_lots": lots, "stores": {}, "settings": {}}
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 999999, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        owner = _store_owner()
        buyer = _buyer()
        target_lot = lots[0]
        buy_qty = min(2, target_lot["qty_available"])

        async def _run():
            await cog._save_state(state)
            ctx_buy = _ctx(owner)
            await _cmd(cog, "wh_buy", ctx_buy, target_lot["lot_id"], buy_qty)

            state2 = await cog._load_state()
            store_id = f"111:{owner.id}"
            assert store_id in state2["stores"]
            store_lot = state2["stores"][store_id]["lots"][0]
            assert store_lot["qty_remaining"] == buy_qty
            assert store_lot["weapon_type"] == target_lot["weapon_type"]

            ctx_sell = _ctx(owner)
            await _cmd(cog, "wh_sell", ctx_sell, buyer, "V", store_lot["lot_id"], 1, 5000)

            state3 = await cog._load_state()
            assert state3["stores"][store_id]["lots"][0]["qty_remaining"] == buy_qty - 1

        asyncio.run(_run())

    def test_multiple_buys_same_lot_accumulate(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-acc", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 20}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 999999, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            ctx1 = _ctx(owner)
            await _cmd(cog, "wh_buy", ctx1, "lot-acc", 3)
            ctx2 = _ctx(owner)
            await _cmd(cog, "wh_buy", ctx2, "lot-acc", 5)
            return await cog._load_state()

        result = asyncio.run(_run())
        store_id = f"111:{owner.id}"
        store_lots = result["stores"][store_id]["lots"]
        total_qty = sum(l["qty_remaining"] for l in store_lots)
        assert total_qty == 8
        assert result["wholesale_lots"][0]["qty_available"] == 12

    def test_sell_entire_stock_empties_lot(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        seller = _store_owner()
        buyer = _buyer()

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{seller.id}": {
                    "owner_id": seller.id,
                    "lots": [
                        {"lot_id": "lot-empty", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 2}
                    ]
                }
            },
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 999999, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            ctx = _ctx(seller)
            await _cmd(cog, "wh_sell", ctx, buyer, "V", "lot-empty", 2, 5000)
            return await cog._load_state()

        result = asyncio.run(_run())
        store_lots = result["stores"][f"111:{seller.id}"]["lots"]
        assert store_lots[0]["qty_remaining"] == 0

    def test_two_stores_independent_inventories(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner1 = _store_owner()
        owner2 = _member(2002, is_admin=False, role_id=800)

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-shared", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 20}
            ],
            "stores": {},
            "settings": {},
        }

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 999999, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        async def _run():
            await cog._save_state(state)
            ctx1 = _ctx(owner1)
            await _cmd(cog, "wh_buy", ctx1, "lot-shared", 3)
            ctx2 = _ctx(owner2)
            await _cmd(cog, "wh_buy", ctx2, "lot-shared", 5)
            return await cog._load_state()

        result = asyncio.run(_run())
        store1 = f"111:{owner1.id}"
        store2 = f"111:{owner2.id}"
        assert store1 in result["stores"]
        assert store2 in result["stores"]
        qty1 = sum(l["qty_remaining"] for l in result["stores"][store1]["lots"])
        qty2 = sum(l["qty_remaining"] for l in result["stores"][store2]["lots"])
        assert qty1 == 3
        assert qty2 == 5
        assert result["wholesale_lots"][0]["qty_available"] == 12


class TestDisplayGrouping:
    def test_wh_list_groups_by_weapon_type(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.guild.id = 111

        state = {
            "wholesale_lots": [
                {"lot_id": "lot-p1", "gun_name": "Nue", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 1300, "qty_available": 5},
                {"lot_id": "lot-s1", "gun_name": "Crusher", "gun_level": "L",
                 "weapon_type": "shotgun", "unit_cost": 6000, "qty_available": 3},
                {"lot_id": "lot-p2", "gun_name": "Liberty", "gun_level": "M",
                 "weapon_type": "pistol", "unit_cost": 2000, "qty_available": 2},
            ],
            "stores": {},
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_list", ctx)

        asyncio.run(_run())
        sent_calls = ctx.send.call_args_list
        all_output = " ".join(str(c) for c in sent_calls)
        assert "Pistol" in all_output
        assert "Shotgun" in all_output
        assert "Nue" in all_output
        assert "Liberty" in all_output
        assert "Crusher" in all_output

    def test_store_inv_shows_owner_inventory(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        owner = _store_owner()
        ctx = _ctx(owner)

        state = {
            "wholesale_lots": [],
            "stores": {
                f"111:{owner.id}": {
                    "owner_id": owner.id,
                    "lots": [
                        {"lot_id": "s-p1", "gun_name": "Nue", "gun_level": "M",
                         "weapon_type": "pistol", "unit_cost": 1300, "qty_remaining": 5},
                        {"lot_id": "s-r1", "gun_name": "Nova", "gun_level": "H",
                         "weapon_type": "revolver", "unit_cost": 4000, "qty_remaining": 2},
                    ]
                }
            },
            "settings": {},
        }

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "store_inv", ctx)

        asyncio.run(_run())
        sent_calls = ctx.send.call_args_list
        all_output = " ".join(str(c) for c in sent_calls)
        assert "Nue" in all_output
        assert "Nova" in all_output

    def test_wh_list_empty_shows_message(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.guild.id = 111

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_list", ctx)

        asyncio.run(_run())
        sent_calls = ctx.send.call_args_list
        all_output = " ".join(str(c) for c in sent_calls)
        assert "empty" in all_output.lower() or "no " in all_output.lower()


class TestRestockSettings:
    def test_view_restock_settings(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_restock_settings", ctx)

        asyncio.run(_run())
        sent_calls = ctx.send.call_args_list
        all_output = " ".join(str(c) for c in sent_calls)
        assert "total_lots" in all_output.lower() or "lots" in all_output.lower()

    def test_update_restock_setting(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        admin = _admin()
        ctx = _ctx(admin)

        state = {"wholesale_lots": [], "stores": {}, "settings": {}}

        async def _run():
            await cog._save_state(state)
            await _cmd(cog, "wh_restock_settings", ctx, "total_lots", 20)
            return await cog._load_state()

        result = asyncio.run(_run())
        assert result["settings"]["restock"]["total_lots"] == 20


class TestEdgeCases:
    def test_weapon_type_derivation_patterns(self, tmp_path, monkeypatch):
        assert WholesalerCog._derive_weapon_type("Crusher", "Power Revolver (L)") == "revolver"
        assert WholesalerCog._derive_weapon_type("M221 Saratoga", "SMG (M)") == "submachine_gun"
        assert WholesalerCog._derive_weapon_type("TestGun", "Power (L)") is None
        assert WholesalerCog._derive_weapon_type("Test Pistol", "Power (L)") == "pistol"
        assert WholesalerCog._derive_weapon_type("Test Shotgun", "Power (L)") == "shotgun"
        assert WholesalerCog._derive_weapon_type("Test Gun", "Assault Rifle (M)") == "assault_rifle"
        assert WholesalerCog._derive_weapon_type("LMG Test", "Light Machine Gun (H)") == "light_machine_gun"
        assert WholesalerCog._derive_weapon_type("Sniper X", "Sniper Rifle (H)") == "sniper_rifle"

    def test_level_derivation_edge_cases(self):
        assert WholesalerCog._derive_level("Tech (M-H)") == "H"
        assert WholesalerCog._derive_level("Tech (M)") == "M"
        assert WholesalerCog._derive_level("Power (L)") == "L"
        assert WholesalerCog._derive_level("Smart (H)") == "H"

    def test_normalize_shop_name(self):
        assert WholesalerCog._normalize_shop_name("Shop 1") == "shop-1"
        assert WholesalerCog._normalize_shop_name("SHOP__2") == "shop-2"

    def test_inventory_totals_calculation(self):
        state = {
            "wholesale_lots": [
                {"qty_available": 3},
                {"qty_available": 0},
                {"qty_available": 2},
            ],
            "stores": {
                "shop-a": {"lots": [{"qty_remaining": 1}, {"qty_remaining": 4}]},
                "shop-b": {"lots": [{"qty_remaining": 0}]},
            },
        }
        assert WholesalerCog._inventory_totals(state) == (3, 5, 2, 5)

    def test_inventory_totals_empty_state(self):
        state = {"wholesale_lots": [], "stores": {}}
        assert WholesalerCog._inventory_totals(state) == (0, 0, 0, 0)

    def test_store_id_generation(self):
        assert WholesalerCog._store_id(111, 2001) == "111:2001"
        assert WholesalerCog._store_id(999, 1) == "999:1"

    def test_google_sheet_url_normalization(self):
        url = "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing"
        out = WholesalerCog._normalize_sheet_source_url(url)
        assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"

    def test_google_sheet_url_with_gid(self):
        url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=456"
        out = WholesalerCog._normalize_sheet_source_url(url)
        assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx&gid=456"

    def test_google_sheet_url_strips_angle_brackets(self):
        url = "<https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing>"
        out = WholesalerCog._normalize_sheet_source_url(url)
        assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"

    def test_restock_with_zero_level_lots(self, tmp_path, monkeypatch):
        cog = _make_cog(tmp_path, monkeypatch)
        cfg = cog._resolve_restock_settings({
            "settings": {
                "restock": {
                    "total_lots": 5,
                    "lots_L": 5, "lots_M": 0, "lots_H": 0,
                    "qty_min_L": 2, "qty_max_L": 2,
                }
            }
        })
        assert cfg["lots_M"] == 0
        assert cfg["lots_H"] == 0
        assert cfg["total_lots"] == 5
