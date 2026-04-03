"""Cyberware shop cog — Ripperdoc buy/sell marketplace.

The legacy prefix commands (!cw_setsheet, !cw_catalog, !cw_add, !cw_remove,
!cw_give, !cw_take, !cw_buy, !cw_inventory, !cw_sell, !cw_install, !cw_tx,
!cw_wh_list, !cw_wh_restock, !cw_wh_add, !cw_wh_remove, !cw_wh_settings)
have been removed.  All cyberware actions are now handled through the
Ripperdoc Hub (!ripperdoc) and Fixer Hub (!fixer).

This cog is still loaded so that hub code can access the helper methods
(inventory loading, catalog management, wholesale operations) via
``bot.cogs.get("CyberwareShop")``.
"""
import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

import config
from NightCityBot.services.cyberware_shop_data import (
    download_sheet,
    parse_cyberware_sheet,
)
from NightCityBot.utils import helpers
from NightCityBot.utils.db import (
    cw_catalog_get_all,
    cw_catalog_upsert_many,
    cw_catalog_upsert_one,
    cw_catalog_delete_one,
    pi_add_item,
)
from NightCityBot.utils.permissions import is_ripperdoc, is_fixer

logger = logging.getLogger(__name__)

_TX_LIMIT = 20


class CyberwareShop(commands.Cog):
    """Ripperdoc buy/sell cyberware marketplace."""

    DEFAULT_CW_RESTOCK_SETTINGS: dict[str, int] = {
        "total_items": 15,
        "qty_min": 1,
        "qty_max": 3,
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.unbelievaboat = bot.unbelievaboat
        self.lock = asyncio.Lock()

        base_dir = Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parents[2]))
        data_dir = Path(
            getattr(config, "CYBERWARE_SHOP_DATA_DIR", base_dir / "data" / "cyberware_shop")
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        self.data_dir = data_dir
        self.state_file = data_dir / "state.json"
        self.sheet_cache_path = data_dir / "master_sheet.xlsx"
        self.tx_file = data_dir / "transactions.json"
        self.inventory_dir = data_dir / "inventory"
        self.inventory_dir.mkdir(parents=True, exist_ok=True)
        self._startup_done = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup_done:
            return
        self._startup_done = True
        if not self.state_file.exists():
            state = {"sheet_url": getattr(config, "CYBERWARE_SHOP_SHEET_URL", ""), "items_count": 0}
            await helpers.save_json_file(self.state_file, state)
        if not self.tx_file.exists():
            await helpers.save_json_file(self.tx_file, [])
        logger.info(
            "CyberwareShop data paths: data_dir=%s state=%s tx=%s inventory_dir=%s",
            self.data_dir,
            self.state_file,
            self.tx_file,
            self.inventory_dir,
        )

        try:
            existing = await cw_catalog_get_all()
            if not existing:
                logger.info("cyberware_catalog is empty — attempting to populate from cache on startup")
                catalog_file = self.data_dir / "catalog.json"
                cached = await helpers.load_json_file(catalog_file, default=[])
                if isinstance(cached, list) and cached:
                    await cw_catalog_upsert_many(cached)
                    logger.info("cyberware_catalog populated on startup: %d items", len(cached))
                else:
                    logger.info("cyberware_catalog startup populate: no cached catalog found (run cw_setsheet first)")
            else:
                logger.info("cyberware_catalog already populated (%d entries) — skipping startup reload", len(existing))
        except Exception:
            logger.warning("cyberware_catalog startup populate failed (non-fatal)", exc_info=True)

    async def _load_state(self) -> dict[str, Any]:
        return await helpers.load_json_file(
            self.state_file,
            default={"sheet_url": "", "items_count": 0},
        )

    async def _save_state(self, state: dict[str, Any]) -> bool:
        return await helpers.save_json_file(self.state_file, state)

    async def _load_catalog(self) -> list[dict[str, Any]]:
        db_items = await cw_catalog_get_all()
        if db_items:
            return db_items
        catalog_file = self.data_dir / "catalog.json"
        return await helpers.load_json_file(catalog_file, default=[])

    async def _save_catalog(self, items: list[dict[str, Any]]) -> bool:
        catalog_file = self.data_dir / "catalog.json"
        return await helpers.save_json_file(catalog_file, items)

    def _inventory_file(self, user_id: int | str) -> Path:
        return self.inventory_dir / f"{user_id}.json"

    async def _load_inventory(self, user_id: int | str) -> list[dict]:
        path = self._inventory_file(user_id)
        data = await helpers.load_json_file(path, default=[])
        if not isinstance(data, list):
            return []
        migrated = False
        result: list[dict] = []
        for entry in data:
            if isinstance(entry, str):
                result.append({
                    "item_id": str(uuid.uuid4()),
                    "name": entry,
                    "price_paid": None,
                    "purchased_at": None,
                })
                migrated = True
            elif isinstance(entry, dict):
                if "item_id" not in entry:
                    entry = {**entry, "item_id": str(uuid.uuid4())}
                    migrated = True
                result.append(entry)
        if migrated:
            await self._save_inventory(user_id, result)
        return result

    async def _save_inventory(self, user_id: int | str, items: list[dict]) -> bool:
        return await helpers.save_json_file(self._inventory_file(user_id), items)

    async def _append_tx(self, tx: dict[str, Any]) -> bool:
        return await helpers.append_json_file(self.tx_file, tx)

    async def _load_tx(self) -> list[dict[str, Any]]:
        data = await helpers.load_json_file(self.tx_file, default=[])
        if isinstance(data, list):
            return data
        return []

    def _lookup_item(
        self, catalog: list[dict[str, Any]], query: str
    ) -> Optional[dict[str, Any]]:
        q = query.strip().lower()
        for item in catalog:
            if item["name"].lower() == q:
                return item
        for item in catalog:
            if item["name"].lower().startswith(q):
                return item
        return None

    async def _log_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "CYBERWARE_LOG_CHANNEL_ID", 0)
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                logger.warning("Could not fetch CYBERWARE_LOG_CHANNEL_ID", exc_info=True)
        return ch

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _resolve_cw_restock_settings(self, state: dict) -> dict:
        cfg = dict(self.DEFAULT_CW_RESTOCK_SETTINGS)
        saved = state.get("settings", {}).get("cw_restock", {})
        cfg.update({k: v for k, v in saved.items() if k in cfg})
        return cfg

    def _generate_cw_lots(
        self, catalog: list[dict], cfg: dict, rng: random.Random
    ) -> list[dict]:
        total = min(cfg.get("total_items", 15), len(catalog))
        qty_min = max(1, cfg.get("qty_min", 1))
        qty_max = max(qty_min, cfg.get("qty_max", 3))
        sample = rng.sample(catalog, total)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return [
            {
                "lot_id": f"cwlot-{stamp}-{uuid.uuid4().hex[:6]}",
                "item_name": item["name"],
                "unit_cost": item["price"],
                "qty_available": rng.randint(qty_min, qty_max),
                "created_at": self._now_iso(),
            }
            for item in sample
        ]

    def _lookup_lot(
        self, lots: list[dict], query: str
    ) -> Optional[dict]:
        q = query.strip().lower()
        for lot in lots:
            if lot.get("item_name", "").lower() == q:
                return lot
        for lot in lots:
            if lot.get("item_name", "").lower().startswith(q):
                return lot
        return None

    async def auto_cw_restock_if_due(self, now: datetime) -> bool:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware_shop"):
            return False
        catalog = await self._load_catalog()
        if not catalog:
            logger.warning("auto_cw_restock_if_due: catalog is empty, skipping")
            return False

        restock_date = now.strftime("%Y-%m-%d")
        async with self.lock:
            state = await self._load_state()
            settings = state.setdefault("settings", {})
            if str(settings.get("last_cw_restock_sunday", "")) == restock_date:
                return True
            cfg = self._resolve_cw_restock_settings(state)
            rng = random.Random()
            lots = self._generate_cw_lots(catalog, cfg, rng)
            state["cw_wholesale_lots"] = lots
            settings["last_cw_restock_sunday"] = restock_date
            saved = await self._save_state(state)
            if not saved:
                logger.error("auto_cw_restock_if_due: save failed")

        logger.info("auto_cw_restock_if_due: rotated %d CW lots for %s", len(lots), restock_date)
        log_ch = await self._log_channel()
        if log_ch:
            await log_ch.send(
                f"📦 [CW_AUTO_RESTOCK] {len(lots)} items rotated for week of {restock_date}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True

    @staticmethod
    def _sorted_lots(lots: list[dict]) -> list[dict]:
        return sorted(lots, key=lambda l: l["item_name"])

    @staticmethod
    def _grouped_inventory(inventory: list[dict]) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for item in inventory:
            name = item["name"]
            price = item.get("price_paid")
            raw_date = item.get("purchased_at")
            date_str = str(raw_date)[:10] if raw_date else ""
            key = (name, price, date_str)
            if key not in groups:
                groups[key] = {
                    "name": name,
                    "price_paid": price,
                    "date": date_str,
                    "items": [],
                }
            groups[key]["items"].append(item)
        for g in groups.values():
            g["items"].sort(
                key=lambda i: (i.get("purchased_at") is None, str(i.get("purchased_at") or ""))
            )
            g["count"] = len(g["items"])
        return sorted(
            groups.values(),
            key=lambda g: (g["name"], str(g["price_paid"] or ""), g["date"]),
        )
