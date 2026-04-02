"""Cyberware shop cog — Ripperdoc buy/sell marketplace.

Ripperdocs source parts from an unlimited-stock wholesaler at sheet price,
then sell/install them for patients at their own price.  Every transaction
is audited to RIPPERDOC_LOG_CHANNEL_ID.
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

        # Populate cyberware_catalog DB table on startup if it is empty.
        # Falls back to the local catalog.json cache written by !cw_setsheet.
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
                    logger.info("cyberware_catalog startup populate: no cached catalog found (run !cw_setsheet first)")
            else:
                logger.info("cyberware_catalog already populated (%d entries) — skipping startup reload", len(existing))
        except Exception:
            logger.warning("cyberware_catalog startup populate failed (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_state(self) -> dict[str, Any]:
        return await helpers.load_json_file(
            self.state_file,
            default={"sheet_url": "", "items_count": 0},
        )

    async def _save_state(self, state: dict[str, Any]) -> bool:
        return await helpers.save_json_file(self.state_file, state)

    async def _load_catalog(self) -> list[dict[str, Any]]:
        """Return the item list from cyberware_catalog DB, falling back to catalog.json."""
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

    async def _load_inventory(self, user_id: int | str) -> list[str]:
        """Return the list of item names currently stocked by this Ripperdoc."""
        path = self._inventory_file(user_id)
        data = await helpers.load_json_file(path, default=[])
        if isinstance(data, list):
            return data
        return []

    async def _save_inventory(self, user_id: int | str, items: list[str]) -> bool:
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
        """Case-insensitive exact match, then prefix match as fallback."""
        q = query.strip().lower()
        for item in catalog:
            if item["name"].lower() == q:
                return item
        for item in catalog:
            if item["name"].lower().startswith(q):
                return item
        return None

    async def _log_channel(self) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(config.RIPPERDOC_LOG_CHANNEL_ID)
            except Exception:
                logger.warning("Could not fetch RIPPERDOC_LOG_CHANNEL_ID", exc_info=True)
        return ch

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Cyberware wholesale helpers
    # ------------------------------------------------------------------

    def _resolve_cw_restock_settings(self, state: dict) -> dict:
        """Merge saved settings over defaults."""
        cfg = dict(self.DEFAULT_CW_RESTOCK_SETTINGS)
        saved = state.get("settings", {}).get("cw_restock", {})
        cfg.update({k: v for k, v in saved.items() if k in cfg})
        return cfg

    def _generate_cw_lots(
        self, catalog: list[dict], cfg: dict, rng: random.Random
    ) -> list[dict]:
        """Randomly pick total_items items from catalog and assign qty."""
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
        """Case-insensitive exact then prefix match against lot item_name."""
        q = query.strip().lower()
        for lot in lots:
            if lot.get("item_name", "").lower() == q:
                return lot
        for lot in lots:
            if lot.get("item_name", "").lower().startswith(q):
                return lot
        return None

    async def auto_cw_restock_if_due(self, now: datetime) -> bool:
        """Run the weekly cyberware wholesale rotation if not yet done this Sunday.

        Called by the cyberware weekly process (cyberware.py) on Sunday.
        Returns True if a restock ran or was already done; False on failure.
        """
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware_shop"):
            return False
        catalog = await self._load_catalog()
        if not catalog:
            logger.warning("auto_cw_restock_if_due: catalog is empty, skipping")
            return False

        sunday_key = now.strftime("%Y-%m-%d")
        async with self.lock:
            state = await self._load_state()
            settings = state.setdefault("settings", {})
            if str(settings.get("last_cw_restock_sunday", "")) == sunday_key:
                return True
            cfg = self._resolve_cw_restock_settings(state)
            rng = random.Random()
            lots = self._generate_cw_lots(catalog, cfg, rng)
            state["cw_wholesale_lots"] = lots
            settings["last_cw_restock_sunday"] = sunday_key
            saved = await self._save_state(state)
            if not saved:
                logger.error("auto_cw_restock_if_due: save failed")

        logger.info("auto_cw_restock_if_due: rotated %d CW lots for %s", len(lots), sunday_key)
        log_ch = await self._log_channel()
        if log_ch:
            await log_ch.send(
                f"📦 [CW_AUTO_RESTOCK] {len(lots)} items rotated for week of {sunday_key}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.command(name="cw_setsheet")
    @commands.has_permissions(administrator=True)
    async def cw_setsheet(self, ctx: commands.Context, *, url: str) -> None:
        """(Admin) Set the cyberware catalog Google Sheet URL and refresh the cache."""
        async with self.lock:
            await ctx.send("⏳ Downloading and parsing cyberware sheet…")
            try:
                await download_sheet(url, self.sheet_cache_path)
            except Exception as exc:
                await ctx.send(f"❌ Failed to download sheet: {exc}")
                logger.error("cw_setsheet download failed", exc_info=True)
                return

            try:
                items = await asyncio.to_thread(
                    parse_cyberware_sheet, self.sheet_cache_path
                )
            except Exception as exc:
                await ctx.send(f"❌ Failed to parse sheet: {exc}")
                logger.error("cw_setsheet parse failed", exc_info=True)
                return

            if not items:
                await ctx.send(
                    "⚠️ Sheet downloaded but no items were parsed. "
                    "Check that the sheet has Name and Price columns."
                )
                return

            await self._save_catalog(items)
            await cw_catalog_upsert_many(items)
            state = await self._load_state()
            state["sheet_url"] = url.strip()
            state["items_count"] = len(items)
            await self._save_state(state)

        await ctx.send(
            f"✅ Cyberware catalog updated — **{len(items)}** item(s) loaded."
        )

    @commands.command(name="cw_catalog")
    @commands.check_any(is_ripperdoc(), commands.has_permissions(administrator=True))
    async def cw_catalog(self, ctx: commands.Context) -> None:
        """Show the full cyberware catalog with reference prices."""
        catalog = await self._load_catalog()
        if not catalog:
            await ctx.send(
                "❌ Cyberware catalog is empty. "
                "An admin must run `!cw_setsheet <url>` first."
            )
            return

        lines = []
        for item in catalog:
            cwp_str = f" | CWP: **{item['cwp']}**" if item.get("cwp") else ""
            line = f"`{item['name']}` — ${item['price']:,}{cwp_str}"
            if item.get("description"):
                line += f"\n> {item['description']}"
            lines.append(line)

        page_size = 10
        pages = [lines[i: i + page_size] for i in range(0, len(lines), page_size)]
        total = len(catalog)

        for page_num, page in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"Cyberware Catalog (Page {page_num}/{len(pages)})",
                description="\n".join(page),
                color=discord.Color.teal(),
            )
            embed.set_footer(text=f"{total} item(s) total")
            await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Admin catalog / inventory management
    # ------------------------------------------------------------------

    @commands.command(name="cw_add")
    @is_fixer()
    async def cw_add(
        self,
        ctx: commands.Context,
        item_name: str,
        price: int,
        cwp: str = "",
        *,
        description: str = "",
    ) -> None:
        """Add or update a single item in the cyberware catalog.

        Usage:
          !cw_add "Item Name" <price> [cwp] [description...]

        Examples:
          !cw_add "Kiroshi Optics Mk.2" 5000
          !cw_add "Kiroshi Optics Mk.2" 5000 CWP-2
          !cw_add "Kiroshi Optics Mk.2" 5000 CWP-2 Enhanced optical neural interface
        """
        if price <= 0:
            await ctx.send("❌ Price must be a positive number.")
            return
        ok = await cw_catalog_upsert_one(
            {"name": item_name, "price": price, "cwp": cwp, "description": description}
        )
        if ok:
            parts = [f"✅ **{item_name}** saved to catalog — ${price:,}"]
            if cwp:
                parts.append(f"CWP: {cwp}")
            msg = " | ".join(parts)
            if description:
                msg += f"\n> {description}"
            await ctx.send(msg)
        else:
            await ctx.send("❌ Failed to save item to catalog. Check the bot logs.")

    @commands.command(name="cw_remove")
    @is_fixer()
    async def cw_remove(self, ctx: commands.Context, *, item_name: str) -> None:
        """Remove an item from the cyberware catalog by name.

        Usage: !cw_remove <item name>
        This does NOT affect Ripperdocs' existing inventories.
        """
        deleted = await cw_catalog_delete_one(item_name)
        if deleted:
            await ctx.send(f"✅ **{item_name}** removed from the catalog.")
        else:
            await ctx.send(
                f"❌ No catalog entry found matching **{item_name}**. "
                "Check the spelling or use `!cw_catalog` to browse."
            )

    @commands.command(name="cw_give")
    @is_fixer()
    async def cw_give(
        self, ctx: commands.Context, ripperdoc: discord.Member, *, item_name: str
    ) -> None:
        """Give a cyberware item directly to a Ripperdoc's inventory (bypasses wholesale).

        Usage: !cw_give @ripperdoc <item name>
        The item does not need to be in the weekly wholesale rotation.
        """
        async with self.lock:
            inventory = await self._load_inventory(ripperdoc.id)
            inventory.append(item_name)
            await self._save_inventory(ripperdoc.id, inventory)

        log_ch = await self._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔧 Admin: Cyberware Given",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Ripperdoc", value=ripperdoc.mention, inline=True)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed)

        await ctx.send(
            f"✅ **{item_name}** added to {ripperdoc.display_name}'s inventory."
        )

    @commands.command(name="cw_take")
    @is_fixer()
    async def cw_take(
        self, ctx: commands.Context, ripperdoc: discord.Member, *, item_name: str
    ) -> None:
        """Remove a cyberware item from a Ripperdoc's inventory.

        Usage: !cw_take @ripperdoc <item name>
        Removes the first matching entry (case-insensitive).
        """
        async with self.lock:
            inventory = await self._load_inventory(ripperdoc.id)
            q = item_name.strip().lower()
            idx = next(
                (i for i, n in enumerate(inventory) if n.strip().lower() == q), None
            )
            if idx is None:
                idx = next(
                    (i for i, n in enumerate(inventory) if n.strip().lower().startswith(q)), None
                )
            if idx is None:
                await ctx.send(
                    f"❌ **{item_name}** not found in {ripperdoc.display_name}'s inventory."
                )
                return
            removed = inventory.pop(idx)
            await self._save_inventory(ripperdoc.id, inventory)

        log_ch = await self._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔧 Admin: Cyberware Removed from Inventory",
                color=discord.Color.red(),
            )
            embed.add_field(name="Ripperdoc", value=ripperdoc.mention, inline=True)
            embed.add_field(name="Item Removed", value=removed, inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed)

        await ctx.send(
            f"✅ **{removed}** removed from {ripperdoc.display_name}'s inventory."
        )

    @commands.command(name="cw_buy")
    @is_ripperdoc()
    async def cw_buy(self, ctx: commands.Context, *, item_name: str) -> None:
        """Purchase a cyberware part from this week's wholesale stock."""
        state = await self._load_state()
        lots = state.get("cw_wholesale_lots", [])

        if not lots:
            await ctx.send(
                "❌ No cyberware is available from the wholesaler this week. "
                "Use `!cw_wh_list` to check stock, or ask an admin to run `!cw_wh_restock`."
            )
            return

        lot = self._lookup_lot(lots, item_name)
        if lot is None:
            await ctx.send(
                f"❌ **{item_name}** is not in this week's wholesale stock. "
                "Use `!cw_wh_list` to see what's available."
            )
            return

        if int(lot.get("qty_available", 0)) <= 0:
            await ctx.send(
                f"❌ **{lot['item_name']}** is sold out this week."
            )
            return

        price = int(lot["unit_cost"])

        balance = await self.unbelievaboat.get_balance(ctx.author.id)
        if balance is None:
            await ctx.send("❌ Could not fetch your balance. Please try again.")
            return

        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < price:
            await ctx.send(
                f"❌ Insufficient funds. **{lot['item_name']}** costs ${price:,} "
                f"but you only have ${cash + bank:,}."
            )
            return

        cash_deduct = min(price, cash)
        bank_deduct = max(0, price - cash)
        ok = await self.unbelievaboat.update_balance(
            ctx.author.id,
            {"cash": -cash_deduct, "bank": -bank_deduct},
            reason=f"Cyberware wholesale buy: {lot['item_name']}",
        )
        if not ok:
            await ctx.send("❌ Balance update failed. Please try again.")
            return

        async with self.lock:
            state = await self._load_state()
            current_lot = self._lookup_lot(state.get("cw_wholesale_lots", []), lot["item_name"])
            if current_lot is None or int(current_lot.get("qty_available", 0)) <= 0:
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason=f"Cyberware wholesale buy refund (sold out): {lot['item_name']}",
                )
                await ctx.send(
                    f"❌ **{lot['item_name']}** sold out while processing. "
                    "Your payment has been refunded."
                )
                return

            current_lot["qty_available"] = int(current_lot["qty_available"]) - 1
            inventory = await self._load_inventory(ctx.author.id)
            inventory.append(lot["item_name"])
            await self._save_inventory(ctx.author.id, inventory)
            await self._save_state(state)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "BUY",
                "ts": self._now_iso(),
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "item": lot["item_name"],
                "price": price,
                "lot_id": lot.get("lot_id", ""),
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        receipt = (
            f"🛒 **CYBERWARE PURCHASE**\n"
            f"Ripperdoc: {ctx.author.mention}\n"
            f"Item: **{lot['item_name']}**\n"
            f"Price paid: **${price:,}**"
        )
        if log_ch:
            await log_ch.send(receipt, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Purchased **{lot['item_name']}** for **${price:,}**. "
            "It is now in your inventory."
        )

    @commands.command(name="cw_inventory")
    @commands.check_any(is_ripperdoc(), commands.has_permissions(administrator=True))
    async def cw_inventory(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show your (or another Ripperdoc's) current cyberware inventory."""
        target = member or ctx.author
        if member and not any(
            r.id == config.RIPPERDOC_ROLE_ID for r in ctx.author.roles
        ) and not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only Ripperdocs or admins can view another member's inventory.")
            return

        inventory = await self._load_inventory(target.id)
        if not inventory:
            name = "Your" if target == ctx.author else f"{target.display_name}'s"
            await ctx.send(f"📦 {name} cyberware inventory is empty.")
            return

        from collections import Counter
        counts = Counter(inventory)
        lines = [
            f"`{name}` × {qty}" if qty > 1 else f"`{name}`"
            for name, qty in sorted(counts.items())
        ]
        desc = "\n".join(lines)
        title = (
            "Your Cyberware Inventory"
            if target == ctx.author
            else f"{target.display_name}'s Cyberware Inventory"
        )
        embed = discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{len(inventory)} item(s) in stock")
        await ctx.send(embed=embed)

    @commands.command(name="cw_sell")
    @is_ripperdoc()
    async def cw_sell(
        self,
        ctx: commands.Context,
        patient: discord.Member,
        item_name: str,
        price: int,
    ) -> None:
        """Sell/install a cyberware part to a patient at your stated price.

        Usage: !cw_sell @patient "item name" <price>
        """
        if price <= 0:
            await ctx.send("❌ Price must be a positive number.")
            return

        if patient.id == ctx.author.id:
            await ctx.send("❌ You cannot sell to yourself.")
            return

        catalog = await self._load_catalog()
        item = self._lookup_item(catalog, item_name)
        if item is None:
            await ctx.send(
                f"❌ Item **{item_name}** not found in catalog. "
                "Use `!cw_catalog` to browse available items."
            )
            return

        async with self.lock:
            inventory = await self._load_inventory(ctx.author.id)
            item_lower = item["name"].lower()
            idx = next(
                (i for i, n in enumerate(inventory) if n.lower() == item_lower),
                None,
            )
            if idx is None:
                await ctx.send(
                    f"❌ **{item['name']}** is not in your inventory. "
                    "Use `!cw_buy` to purchase it first."
                )
                return

            patient_balance = await self.unbelievaboat.get_balance(patient.id)
            if patient_balance is None:
                await ctx.send("❌ Could not fetch patient's balance. Please try again.")
                return

            pat_cash = int(patient_balance.get("cash", 0))
            pat_bank = int(patient_balance.get("bank", 0))
            pat_total = pat_cash + pat_bank
            if pat_total < price:
                await ctx.send(
                    f"❌ {patient.display_name} cannot afford **${price:,}** "
                    f"(they have **${pat_total:,}**)."
                )
                return

            pat_cash_deduct = min(price, pat_cash)
            pat_bank_deduct = max(0, price - pat_cash)

            ok_patient = await self.unbelievaboat.update_balance(
                patient.id,
                {"cash": -pat_cash_deduct, "bank": -pat_bank_deduct},
                reason=f"Cyberware install: {item['name']}",
            )
            if not ok_patient:
                await ctx.send("❌ Failed to deduct from patient's balance. Aborting.")
                return

            ok_ripper = await self.unbelievaboat.update_balance(
                ctx.author.id,
                {"cash": price},
                reason=f"Cyberware sale: {item['name']} to {patient.display_name}",
            )
            if not ok_ripper:
                logger.error(
                    "cw_sell: patient debited but Ripperdoc credit failed for %s — "
                    "attempting refund to patient %s",
                    ctx.author.id,
                    patient.id,
                )
                refund_ok = await self.unbelievaboat.update_balance(
                    patient.id,
                    {"cash": pat_cash_deduct, "bank": pat_bank_deduct},
                    reason=f"Cyberware sale refund (Ripperdoc credit failure): {item['name']}",
                )
                if refund_ok:
                    await ctx.send(
                        "❌ Failed to credit your balance. "
                        "Patient's payment has been refunded. No changes saved."
                    )
                else:
                    await ctx.send(
                        "❌ Critical error: Ripperdoc credit failed AND patient refund failed. "
                        "Please contact an admin immediately to manually correct balances."
                    )
                    logger.error(
                        "cw_sell: BOTH Ripperdoc credit and patient refund failed — "
                        "manual balance correction required for patient %s amount %d",
                        patient.id,
                        price,
                    )
                return

            inventory.pop(idx)
            await self._save_inventory(ctx.author.id, inventory)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "SELL",
                "ts": self._now_iso(),
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "patient_id": str(patient.id),
                "patient_name": patient.display_name,
                "item": item["name"],
                "price": price,
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        receipt = (
            f"💉 **CYBERWARE INSTALL**\n"
            f"Ripperdoc: {ctx.author.mention}\n"
            f"Patient: {patient.mention}\n"
            f"Item: **{item['name']}**\n"
            f"Price charged: **${price:,}**"
        )
        if log_ch:
            await log_ch.send(receipt, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Sold **{item['name']}** to {patient.display_name} for **${price:,}**."
        )

    @commands.command(name="cw_tx")
    @commands.check_any(is_ripperdoc(), commands.has_permissions(administrator=True))
    async def cw_tx(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """Show recent cyberware transactions (admin or own transactions only)."""
        is_admin = ctx.author.guild_permissions.administrator
        if member and not is_admin and member.id != ctx.author.id:
            await ctx.send("❌ You can only view your own transactions.")
            return

        target = member
        if not is_admin and target is None:
            target = ctx.author

        all_tx = await self._load_tx()
        if not all_tx:
            await ctx.send("📋 No cyberware transactions recorded yet.")
            return

        if target is not None:
            filtered = [
                t for t in all_tx if t.get("ripperdoc_id") == str(target.id)
            ]
        else:
            filtered = all_tx

        recent = filtered[-_TX_LIMIT:][::-1]
        if not recent:
            name = target.display_name if target else "anyone"
            await ctx.send(f"📋 No transactions found for {name}.")
            return

        lines: list[str] = []
        for tx in recent:
            ts = tx.get("ts", "")[:19].replace("T", " ")
            kind = tx.get("tx_type", "?")
            item = tx.get("item", "?")
            price = tx.get("price", 0)
            ripper = tx.get("ripperdoc_name", "?")
            if kind == "BUY":
                lines.append(f"`{ts}` **BUY** — {ripper} bought `{item}` for ${price:,}")
            elif kind == "SELL":
                patient_name = tx.get("patient_name", "?")
                lines.append(
                    f"`{ts}` **SELL** — {ripper} → {patient_name}: `{item}` for ${price:,}"
                )
            else:
                lines.append(f"`{ts}` **{kind}** — {ripper}: `{item}` ${price:,}")

        title = (
            f"Cyberware Transactions — {target.display_name}"
            if target
            else "Cyberware Transactions (All)"
        )
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Showing last {len(recent)} of {len(filtered)} transaction(s)")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Cyberware wholesale commands
    # ------------------------------------------------------------------

    @commands.command(name="cw_wh_list", aliases=["cw_wholesale", "cw_stock"])
    @commands.check_any(is_ripperdoc(), commands.has_permissions(administrator=True))
    async def cw_wh_list(self, ctx: commands.Context) -> None:
        """Show this week's cyberware available from the wholesaler."""
        state = await self._load_state()
        lots = state.get("cw_wholesale_lots", [])
        if not lots:
            await ctx.send(
                "⚠️ No cyberware wholesale stock this week. "
                "Ask an admin to run `!cw_wh_restock`."
            )
            return

        settings = state.get("settings", {})
        sunday_key = str(settings.get("last_cw_restock_sunday", "unknown"))
        available = sorted(
            [l for l in lots if int(l.get("qty_available", 0)) > 0],
            key=lambda l: l["item_name"],
        )
        sold_out = sorted(
            [l for l in lots if int(l.get("qty_available", 0)) <= 0],
            key=lambda l: l["item_name"],
        )

        lines = []
        for lot in available:
            qty = int(lot["qty_available"])
            price = int(lot["unit_cost"])
            lines.append(f"`{lot['item_name']}` — ${price:,} × {qty}")
        for lot in sold_out:
            lines.append(f"~~`{lot['item_name']}`~~ — Sold out")

        page_size = 20
        pages = [lines[i: i + page_size] for i in range(0, len(lines), page_size)]
        for page_num, page in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"🔩 Cyberware Wholesale — Week of {sunday_key} ({page_num}/{len(pages)})",
                description="\n".join(page),
                color=discord.Color.teal(),
            )
            embed.set_footer(
                text=f"{len(available)} of {len(lots)} items available | Use !cw_buy <name> to purchase"
            )
            await ctx.send(embed=embed)

    @commands.command(name="cw_wh_restock")
    @commands.has_permissions(administrator=True)
    async def cw_wh_restock(
        self, ctx: commands.Context, seed: Optional[int] = None
    ) -> None:
        """(Admin) Force a fresh weekly cyberware wholesale rotation."""
        catalog = await self._load_catalog()
        if not catalog:
            await ctx.send(
                "❌ Cyberware catalog is empty. Run `!cw_setsheet <url>` first."
            )
            return

        if len(catalog) < 1:
            await ctx.send("❌ Not enough items in catalog to generate lots.")
            return

        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_cw_restock_settings(state)
            rng = random.Random(seed)
            lots = self._generate_cw_lots(catalog, cfg, rng)
            sunday_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            state["cw_wholesale_lots"] = lots
            state.setdefault("settings", {})["last_cw_restock_sunday"] = sunday_key
            saved = await self._save_state(state)

        if not saved:
            await ctx.send("❌ Failed to save restock state.")
            return

        total_units = sum(int(l["qty_available"]) for l in lots)
        summary = (
            f"✅ Cyberware wholesale restocked — **{len(lots)}** items, "
            f"**{total_units}** units total."
        )
        item_lines = [
            f"  `{lot['item_name']}` × {lot['qty_available']} @ ${lot['unit_cost']:,}"
            for lot in sorted(lots, key=lambda l: l["item_name"])
        ]
        full = summary + "\n" + "\n".join(item_lines)
        if len(full) <= 1900:
            await ctx.send(full)
        else:
            await ctx.send(summary)
            chunk = ""
            for line in item_lines:
                if len(chunk) + len(line) + 1 > 1900:
                    await ctx.send(chunk)
                    chunk = line
                else:
                    chunk = (chunk + "\n" + line).lstrip("\n")
            if chunk:
                await ctx.send(chunk)

        log_ch = await self._log_channel()
        if log_ch:
            await log_ch.send(
                f"📦 [CW_RESTOCK] by={ctx.author.mention} items={len(lots)} "
                f"units={total_units} seed={seed}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.command(name="cw_wh_add")
    @commands.has_permissions(administrator=True)
    async def cw_wh_add(
        self, ctx: commands.Context, qty: int, *, item_name: str
    ) -> None:
        """(Admin) Add or restock an item in this week's cyberware wholesale.

        Usage: !cw_wh_add <qty> <item name>
        """
        if qty <= 0:
            await ctx.send("❌ qty must be positive.")
            return

        catalog = await self._load_catalog()
        item = self._lookup_item(catalog, item_name)
        if item is None:
            await ctx.send(
                f"❌ **{item_name}** not found in catalog. "
                "Use `!cw_catalog` to browse available items."
            )
            return

        async with self.lock:
            state = await self._load_state()
            lots = state.setdefault("cw_wholesale_lots", [])
            existing = self._lookup_lot(lots, item["name"])
            if existing:
                existing["qty_available"] = int(existing["qty_available"]) + qty
                msg = (
                    f"✅ Added **{qty}** unit(s) to `{item['name']}` "
                    f"(now ×{existing['qty_available']})."
                )
            else:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
                lots.append({
                    "lot_id": f"cwlot-{stamp}-{uuid.uuid4().hex[:6]}",
                    "item_name": item["name"],
                    "unit_cost": item["price"],
                    "qty_available": qty,
                    "created_at": self._now_iso(),
                })
                msg = (
                    f"✅ Added **{item['name']}** × {qty} "
                    f"@ ${item['price']:,} to this week's wholesale."
                )
            await self._save_state(state)

        await ctx.send(msg)

    @commands.command(name="cw_wh_remove")
    @commands.has_permissions(administrator=True)
    async def cw_wh_remove(self, ctx: commands.Context, *, item_name: str) -> None:
        """(Admin) Remove an item entirely from this week's cyberware wholesale."""
        async with self.lock:
            state = await self._load_state()
            lots = state.get("cw_wholesale_lots", [])
            lot = self._lookup_lot(lots, item_name)
            if lot is None:
                await ctx.send(
                    f"❌ **{item_name}** not found in this week's wholesale lots."
                )
                return
            lots.remove(lot)
            state["cw_wholesale_lots"] = lots
            await self._save_state(state)

        await ctx.send(f"✅ Removed **{lot['item_name']}** from this week's wholesale.")

    @commands.command(name="cw_wh_settings")
    @commands.has_permissions(administrator=True)
    async def cw_wh_settings(
        self,
        ctx: commands.Context,
        key: Optional[str] = None,
        value: Optional[int] = None,
    ) -> None:
        """(Admin) View or update cyberware wholesale restock settings.

        Keys: total_items, qty_min, qty_max
        Example: !cw_wh_settings total_items 20
        """
        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_cw_restock_settings(state)

            if key and value is not None:
                if key not in self.DEFAULT_CW_RESTOCK_SETTINGS:
                    valid = ", ".join(f"`{k}`" for k in self.DEFAULT_CW_RESTOCK_SETTINGS)
                    await ctx.send(f"❌ Invalid key. Valid keys: {valid}")
                    return
                cfg[key] = max(1, int(value))
                state.setdefault("settings", {}).setdefault("cw_restock", {})[key] = cfg[key]
                await self._save_state(state)
                await ctx.send(f"✅ Set `{key}` = {cfg[key]}.")
                return

        lines = ["**Cyberware Wholesale Restock Settings**"]
        for k in sorted(self.DEFAULT_CW_RESTOCK_SETTINGS):
            lines.append(f"`{k}` = {cfg[k]}")
        await ctx.send("\n".join(lines))
