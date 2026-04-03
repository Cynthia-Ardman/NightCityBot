"""Cyberware shop cog — Ripperdoc buy/sell marketplace.

Legacy prefix commands (!cw_setsheet, !cw_catalog, !cw_add, !cw_remove,
!cw_give, !cw_take) have been removed.  Primary cyberware actions are now
handled through the Ripperdoc Hub (!ripperdoc) and Fixer Hub (!fixer).

Retained commands:
- !cw_buy, !cw_sell, !cw_install, !cw_inventory — fallbacks for cases
  exceeding the 25-item Discord dropdown limit.
- !cw_tx — transaction history lookup.
- !cw_wh_list, !cw_wh_restock, !cw_wh_add, !cw_wh_remove, !cw_wh_settings
  — wholesale management (also accessible via the hubs).

This cog is still loaded so that hub code can access the helper methods
(inventory loading, catalog management, wholesale operations) via
``bot.cogs.get("CyberwareShop")``.
"""
import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils import helpers
from NightCityBot.utils.db import (
    cw_catalog_get_all,
    cw_catalog_upsert_many,
    cw_catalog_upsert_one,
    cw_catalog_delete_one,
    pi_add_item,
    ResourceLockManager,
    db_save,
    db_load,
    DB_LOAD_FAILED,
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
        self._locks = ResourceLockManager()
        self.lock = self._locks.pin("state")

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
        if not self.tx_file.exists():
            await helpers.save_json_file(self.tx_file, [])
        state = await self._load_state()
        if not state.get("sheet_url") and getattr(config, "CYBERWARE_SHOP_SHEET_URL", ""):
            state["sheet_url"] = config.CYBERWARE_SHOP_SHEET_URL
            await self._save_state(state)
        logger.info(
            "CyberwareShop data paths: data_dir=%s state=%s tx=%s inventory_dir=%s sheet_url=%s",
            self.data_dir,
            self.state_file,
            self.tx_file,
            self.inventory_dir,
            bool(state.get("sheet_url")),
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
                    logger.info("cyberware_catalog startup populate: no cached catalog found (use Admin Hub → Set CW Sheet)")
            else:
                logger.info("cyberware_catalog already populated (%d entries) — skipping startup reload", len(existing))
        except Exception:
            logger.warning("cyberware_catalog startup populate failed (non-fatal)", exc_info=True)

    _DB_STATE_KEY = "cw_shop_state"

    async def _load_state(self) -> dict[str, Any]:
        default = {"sheet_url": "", "items_count": 0}
        state = await db_load(
            self._DB_STATE_KEY,
            default=None,
            seed_path=self.state_file,
        )
        if state is DB_LOAD_FAILED or state is None:
            file_state = await helpers.load_json_file(self.state_file, default=default)
            if state is None and isinstance(file_state, dict) and file_state != default:
                await db_save(self._DB_STATE_KEY, file_state)
            state = file_state
        if not isinstance(state, dict):
            state = default
        return state

    async def _save_state(self, state: dict[str, Any]) -> bool:
        db_ok = await db_save(self._DB_STATE_KEY, state)
        file_ok = await helpers.save_json_file(self.state_file, state)
        return db_ok or file_ok

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

    @commands.command(name="cw_buy")
    @is_ripperdoc()
    async def cw_buy(self, ctx: commands.Context, lot_number: int, qty: int = 1) -> None:
        """Purchase cyberware from this week's wholesale by lot number.

        Usage: !cw_buy <lot_number> [qty=1]
        Use !cw_wh_list to see the numbered list.

        Note: Consider using !ripperdoc for an interactive experience.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if qty < 1:
            await ctx.send("❌ qty must be at least 1.")
            return

        state = await self._load_state()
        lots = state.get("cw_wholesale_lots", [])
        all_ordered = self._sorted_lots(lots)

        if not all_ordered:
            await ctx.send(
                "❌ No cyberware is available from the wholesaler this week. "
                "Ask an admin to run `!cw_wh_restock`."
            )
            return

        if lot_number < 1 or lot_number > len(all_ordered):
            await ctx.send(
                f"❌ Invalid lot number **{lot_number}**. "
                f"Use `!cw_wh_list` to see available items (1–{len(all_ordered)})."
            )
            return

        lot = all_ordered[lot_number - 1]
        if int(lot.get("qty_available", 0)) <= 0:
            await ctx.send(
                f"❌ Lot **{lot_number}** (`{lot['item_name']}`) is sold out this week."
            )
            return

        if qty > int(lot.get("qty_available", 0)):
            await ctx.send(
                f"❌ Only **{lot['qty_available']}** unit(s) of `{lot['item_name']}` available."
            )
            return

        unit_price = int(lot["unit_cost"])
        total_price = unit_price * qty

        balance = await self.unbelievaboat.get_balance(ctx.author.id)
        if balance is None:
            await ctx.send("❌ Could not fetch your balance. Please try again.")
            return

        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_price:
            await ctx.send(
                f"❌ Insufficient funds. {qty}× **{lot['item_name']}** costs ${total_price:,} "
                f"but you only have ${cash + bank:,}."
            )
            return

        cash_deduct = min(max(cash, 0), total_price)
        bank_deduct = max(0, total_price - cash_deduct)
        ok = await self.unbelievaboat.update_balance(
            ctx.author.id,
            {"cash": -cash_deduct, "bank": -bank_deduct},
            reason=f"Cyberware wholesale buy ×{qty}: {lot['item_name']}",
        )
        if not ok:
            await ctx.send("❌ Balance update failed. Please try again.")
            return

        async with self.lock:
            state = await self._load_state()
            lots2 = state.get("cw_wholesale_lots", [])
            all2 = self._sorted_lots(lots2)

            if lot_number < 1 or lot_number > len(all2):
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason=f"Cyberware buy refund (lot disappeared): {lot['item_name']}",
                )
                await ctx.send(
                    "❌ The wholesale list changed while processing. "
                    "Your payment has been refunded."
                )
                return

            current_lot = all2[lot_number - 1]
            if current_lot["item_name"] != lot["item_name"] or int(current_lot.get("qty_available", 0)) < qty:
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason=f"Cyberware buy refund (sold out): {lot['item_name']}",
                )
                await ctx.send(
                    f"❌ **{lot['item_name']}** sold out while processing. "
                    "Your payment has been refunded."
                )
                return

            current_lot["qty_available"] = int(current_lot["qty_available"]) - qty
            now_iso = self._now_iso()
            inventory = await self._load_inventory(ctx.author.id)
            for _ in range(qty):
                inventory.append({
                    "item_id": str(uuid.uuid4()),
                    "name": lot["item_name"],
                    "price_paid": unit_price,
                    "purchased_at": now_iso,
                })
            await self._save_inventory(ctx.author.id, inventory)
            await self._save_state(state)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "BUY",
                "ts": now_iso,
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "item": lot["item_name"],
                "price": total_price,
                "qty": qty,
                "lot_id": lot.get("lot_id", ""),
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🛒 Cyberware Purchase",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Buyer (Ripperdoc)",
                value=f"{ctx.author.mention} ({ctx.author.display_name})",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{lot['item_name']}** × {qty}", inline=True)
            embed.add_field(name="Price Paid", value=f"${total_price:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Purchased **{lot['item_name']}** × {qty} for **${total_price:,}**. "
            "Added to your ripperdoc inventory."
        )

    @commands.command(name="cw_inventory")
    @commands.check_any(is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True))
    async def cw_inventory(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show your (or another Ripperdoc's) current cyberware inventory with row numbers.

        Usage: !cw_inventory [@ripperdoc]
        Row numbers can be used with !cw_sell and !cw_install.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        target = member or ctx.author
        if member:
            author_roles = getattr(ctx.author, "roles", [])
            is_privileged = (
                any(r.id == config.RIPPERDOC_ROLE_ID for r in author_roles)
                or any(r.id == config.FIXER_ROLE_ID for r in author_roles)
                or ctx.author.guild_permissions.administrator
            )
            if not is_privileged:
                await ctx.send("❌ Only Ripperdocs, Fixers, or admins can view another member's inventory.")
                return

        inventory = await self._load_inventory(target.id)
        if not inventory:
            name_str = "Your" if target == ctx.author else f"{target.display_name}'s"
            await ctx.send(f"📦 {name_str} cyberware inventory is empty.")
            return

        groups = self._grouped_inventory(inventory)
        lines = []
        for i, g in enumerate(groups, 1):
            name = g["name"]
            price = g["price_paid"]
            count = g["count"]
            date_str = g["date"] or "—"
            price_str = f"${price:,}" if price else "—"
            suffix = f" × {count}" if count > 1 else ""
            lines.append(f"`{i}.` **{name}**{suffix} — paid {price_str} ea. ({date_str})")

        title = (
            "Your Cyberware Inventory"
            if target == ctx.author
            else f"{target.display_name}'s Cyberware Inventory"
        )
        page_size = 20
        pages = [lines[i: i + page_size] for i in range(0, len(lines), page_size)]
        for page_num, page in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"{title} ({page_num}/{len(pages)})",
                description="\n".join(page),
                color=discord.Color.purple(),
            )
            embed.set_footer(
                text=f"{len(inventory)} item(s) in {len(groups)} slot(s) | Use !cw_sell <row> or !cw_install <row>"
            )
            await ctx.send(embed=embed)

    @commands.command(name="cw_sell")
    @is_ripperdoc()
    async def cw_sell(
        self,
        ctx: commands.Context,
        patient: discord.Member,
        inv_number: int,
        price: int,
        *,
        character_name: str,
    ) -> None:
        """Sell/install a cyberware part to a patient. Removes item from your inventory.

        Usage: !cw_sell @patient <inv_row> <price> character name
        Note: Consider using !ripperdoc for an interactive experience with DM confirmation.
        Use !cw_inventory to see your numbered inventory.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if price <= 0:
            await ctx.send("❌ Price must be a positive number.")
            return
        if patient.id == ctx.author.id:
            await ctx.send("❌ You cannot sell to yourself.")
            return

        character_name = character_name.strip().strip('"').strip("'")
        if not character_name:
            await ctx.send("❌ Character name is required.")
            return

        # Pre-flight inventory check outside the lock (grouped rows)
        pre_inv = await self._load_inventory(ctx.author.id)
        pre_groups = self._grouped_inventory(pre_inv)
        if inv_number < 1 or inv_number > len(pre_groups):
            await ctx.send(
                f"❌ Invalid row **{inv_number}**. Your inventory has {len(pre_groups)} group(s). "
                "Use `!cw_inventory` to see your numbered list."
            )
            return

        pre_group = pre_groups[inv_number - 1]
        item_name = pre_group["name"]
        # Tentative item for balance check (FIFO: first in group)
        inv_item = pre_group["items"][0]
        item_id = inv_item.get("item_id", str(uuid.uuid4()))
        price_paid_orig = inv_item.get("price_paid")

        # Balance check outside the lock
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

        pat_cash_deduct = min(max(pat_cash, 0), price)
        pat_bank_deduct = max(0, price - pat_cash_deduct)

        ok_patient = await self.unbelievaboat.update_balance(
            patient.id,
            {"cash": -pat_cash_deduct, "bank": -pat_bank_deduct},
            reason=f"Cyberware install: {item_name}",
        )
        if not ok_patient:
            await ctx.send("❌ Failed to deduct from patient's balance. Aborting.")
            return

        ok_ripper = await self.unbelievaboat.update_balance(
            ctx.author.id,
            {"cash": price},
            reason=f"Cyberware sale: {item_name} to {patient.display_name}",
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
                reason=f"Cyberware sale refund (Ripperdoc credit failure): {item_name}",
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

        async with self._locks.acquire(str(ctx.author.id)):
            inventory = await self._load_inventory(ctx.author.id)
            locked_groups = self._grouped_inventory(inventory)

            if inv_number < 1 or inv_number > len(locked_groups):
                logger.warning(
                    "cw_sell: group row %d out of range (now %d groups) for %s — refunding",
                    inv_number, len(locked_groups), ctx.author.id,
                )
                await self.unbelievaboat.update_balance(
                    patient.id,
                    {"cash": pat_cash_deduct, "bank": pat_bank_deduct},
                    reason=f"Cyberware sale refund (item no longer in inventory): {item_name}",
                )
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"bank": -price},
                    reason=f"Cyberware sale refund (item no longer in inventory): {item_name}",
                )
                await ctx.send(
                    f"❌ **{item_name}** was no longer in your inventory when the "
                    "sale was processed. All payments have been refunded."
                )
                return

            locked_group = locked_groups[inv_number - 1]
            if locked_group["name"] != item_name:
                await self.unbelievaboat.update_balance(
                    patient.id,
                    {"cash": pat_cash_deduct, "bank": pat_bank_deduct},
                    reason=f"Cyberware sale refund (inventory changed): {item_name}",
                )
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"bank": -price},
                    reason=f"Cyberware sale refund (inventory changed): {item_name}",
                )
                await ctx.send(
                    "❌ Your inventory changed while processing. "
                    "All payments have been refunded. Please try again."
                )
                return

            # Pick FIFO item from the confirmed group
            selected_item = locked_group["items"][0]
            item_id = selected_item.get("item_id", str(uuid.uuid4()))
            price_paid_orig = selected_item.get("price_paid")

            # Insert into player_inventory FIRST (data integrity — don't remove unless write succeeds)
            pi_ok = await pi_add_item({
                "item_id": item_id,
                "owner_id": str(patient.id),
                "character_name": character_name,
                "item_type": "cyberware",
                "name": item_name,
                "restriction": "basic",
                "description": "",
                "price_paid": price,
                "seller_id": str(ctx.author.id),
                "seller_name": ctx.author.display_name,
            })
            if not pi_ok:
                logger.error(
                    "cw_sell: pi_add_item failed for patient=%s item=%s — refunding both parties",
                    patient.id, item_name,
                )
                await self.unbelievaboat.update_balance(
                    patient.id,
                    {"cash": pat_cash_deduct, "bank": pat_bank_deduct},
                    reason=f"Cyberware sale refund (DB write failed): {item_name}",
                )
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"bank": -price},
                    reason=f"Cyberware sale refund (DB write failed): {item_name}",
                )
                await ctx.send(
                    "❌ Failed to record item in patient's inventory. "
                    "All payments have been refunded. Please try again or contact an admin."
                )
                return

            # DB write succeeded — now remove the item from ripperdoc stock
            inventory_updated = [it for it in inventory if it.get("item_id") != item_id]
            await self._save_inventory(ctx.author.id, inventory_updated)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "SELL",
                "ts": self._now_iso(),
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "patient_id": str(patient.id),
                "patient_name": patient.display_name,
                "item": item_name,
                "price": price,
                "character_name": character_name,
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="💉 Cyberware Sell / Install",
                color=discord.Color.dark_teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Ripperdoc",
                value=f"{ctx.author.mention} ({ctx.author.display_name})",
                inline=False,
            )
            embed.add_field(
                name="Patient",
                value=f"{patient.mention} ({patient.display_name}) — {character_name}",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Price Charged", value=f"${price:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Installed **{item_name}** on **{character_name}** ({patient.display_name}) "
            f"for **${price:,}**."
        )

    @commands.command(name="cw_install")
    @is_ripperdoc()
    async def cw_install(
        self,
        ctx: commands.Context,
        patient: discord.Member,
        character_name: str,
        inv_number: int,
    ) -> None:
        """Install a cyberware item onto a patient with no payment (free install).

        Usage: !cw_install @patient "character_name" <inv_row>
        Use !cw_inventory to see your numbered inventory.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        character_name = character_name.strip().strip('"').strip("'")
        if not character_name:
            await ctx.send("❌ Character name is required.")
            return

        pre_inv = await self._load_inventory(ctx.author.id)
        pre_groups = self._grouped_inventory(pre_inv)
        if inv_number < 1 or inv_number > len(pre_groups):
            await ctx.send(
                f"❌ Invalid row **{inv_number}**. Your inventory has {len(pre_groups)} group(s). "
                "Use `!cw_inventory` to see your numbered list."
            )
            return

        async with self._locks.acquire(str(ctx.author.id)):
            inventory = await self._load_inventory(ctx.author.id)
            locked_groups = self._grouped_inventory(inventory)
            if inv_number < 1 or inv_number > len(locked_groups):
                await ctx.send(
                    "❌ Inventory changed while processing. Please try again."
                )
                return

            locked_group = locked_groups[inv_number - 1]
            item_name = locked_group["name"]
            selected_item = locked_group["items"][0]
            item_id = selected_item.get("item_id", str(uuid.uuid4()))
            price_paid_orig = selected_item.get("price_paid")

            # Insert into player_inventory first for data integrity
            pi_ok = await pi_add_item({
                "item_id": item_id,
                "owner_id": str(patient.id),
                "character_name": character_name,
                "item_type": "cyberware",
                "name": item_name,
                "restriction": "basic",
                "description": "",
                "price_paid": price_paid_orig,
                "seller_id": str(ctx.author.id),
                "seller_name": ctx.author.display_name,
            })
            if not pi_ok:
                logger.error(
                    "cw_install: pi_add_item failed for patient=%s item=%s — aborting install",
                    patient.id, item_name,
                )
                await ctx.send(
                    "❌ Failed to record item in patient's inventory. "
                    "Nothing has been removed from your stock. Please try again or contact an admin."
                )
                return

            inventory_updated = [it for it in inventory if it.get("item_id") != item_id]
            await self._save_inventory(ctx.author.id, inventory_updated)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "INSTALL",
                "ts": self._now_iso(),
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "patient_id": str(patient.id),
                "patient_name": patient.display_name,
                "item": item_name,
                "price": 0,
                "character_name": character_name,
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="💉 Cyberware Install (Free)",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Ripperdoc",
                value=f"{ctx.author.mention} ({ctx.author.display_name})",
                inline=False,
            )
            embed.add_field(
                name="Patient",
                value=f"{patient.mention} ({patient.display_name}) — {character_name}",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Price Charged", value="Free (admin install)", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Installed **{item_name}** on **{character_name}** ({patient.display_name})."
        )

    @commands.command(name="cw_tx")
    @commands.check_any(is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True))
    async def cw_tx(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """Show recent cyberware transactions (admin or own transactions only)."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        author_roles = getattr(ctx.author, "roles", [])
        is_privileged = (
            (isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator)
            or any(r.id == config.FIXER_ROLE_ID for r in author_roles)
        )
        if member and not is_privileged and member.id != ctx.author.id:
            await ctx.send("❌ You can only view your own transactions.")
            return

        target = member
        if not is_privileged and target is None:
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
    @commands.check_any(is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True))
    async def cw_wh_list(self, ctx: commands.Context) -> None:
        """Show this week's cyberware available from the wholesaler."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
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
        all_ordered = self._sorted_lots(lots)
        available_count = sum(1 for l in lots if int(l.get("qty_available", 0)) > 0)

        lines = []
        for i, lot in enumerate(all_ordered, 1):
            qty = int(lot["qty_available"])
            price = int(lot["unit_cost"])
            if qty > 0:
                lines.append(f"**{i}.** `{lot['item_name']}` — ${price:,} × {qty}")
            else:
                lines.append(f"~~**{i}.** `{lot['item_name']}`~~ — Sold out")

        page_size = 20
        pages = [lines[i: i + page_size] for i in range(0, len(lines), page_size)]
        for page_num, page in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"🔩 Cyberware Wholesale — Week of {sunday_key} ({page_num}/{len(pages)})",
                description="\n".join(page),
                color=discord.Color.teal(),
            )
            embed.set_footer(
                text=f"{available_count} of {len(lots)} items available | Use !cw_buy <lot#> [qty]"
            )
            await ctx.send(embed=embed)

    @commands.command(name="cw_wh_restock")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def cw_wh_restock(
        self, ctx: commands.Context, seed: Optional[int] = None
    ) -> None:
        """(Admin) Force a fresh weekly cyberware wholesale rotation."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        catalog = await self._load_catalog()
        if not catalog:
            await ctx.send(
                "❌ Cyberware catalog is empty. Use the `!admin` hub to set a catalog sheet."
            )
            return

        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_cw_restock_settings(state)
            rng = random.Random(seed)
            lots = self._generate_cw_lots(catalog, cfg, rng)
            restock_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            state["cw_wholesale_lots"] = lots
            state.setdefault("settings", {})["last_cw_restock_sunday"] = restock_date
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
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def cw_wh_add(
        self, ctx: commands.Context, qty: int, *, item_name: str
    ) -> None:
        """(Admin) Add or restock an item in this week's cyberware wholesale.

        Usage: !cw_wh_add <qty> <item name>
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if qty <= 0:
            await ctx.send("❌ qty must be positive.")
            return

        catalog = await self._load_catalog()
        item = self._lookup_item(catalog, item_name)
        if item is None:
            await ctx.send(
                f"❌ **{item_name}** not found in catalog. "
                "Use `!cw_wh_list` to browse available items."
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
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def cw_wh_remove(self, ctx: commands.Context, *, item_name: str) -> None:
        """(Admin) Remove an item entirely from this week's cyberware wholesale."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
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
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
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
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        set_value = None
        invalid_msg = None
        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_cw_restock_settings(state)

            if key and value is not None:
                if key not in self.DEFAULT_CW_RESTOCK_SETTINGS:
                    valid = ", ".join(f"`{k}`" for k in self.DEFAULT_CW_RESTOCK_SETTINGS)
                    invalid_msg = f"❌ Invalid key. Valid keys: {valid}"
                else:
                    cfg[key] = max(1, int(value))
                    state.setdefault("settings", {}).setdefault("cw_restock", {})[key] = cfg[key]
                    await self._save_state(state)
                    set_value = cfg[key]

        if invalid_msg:
            await ctx.send(invalid_msg)
            return

        if set_value is not None:
            await ctx.send(f"✅ Set `{key}` = {set_value}.")
            return

        lines = ["**Cyberware Wholesale Restock Settings**"]
        for k in sorted(self.DEFAULT_CW_RESTOCK_SETTINGS):
            lines.append(f"`{k}` = {cfg[k]}")
        await ctx.send("\n".join(lines))
