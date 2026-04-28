"""Cyberware shop cog — Ripperdoc buy/sell marketplace.

Legacy prefix commands (!cw_setsheet, !cw_catalog, !cw_add, !cw_remove,
!cw_give, !cw_take) have been removed.  Primary cyberware actions are now
handled through the Ripperdoc Hub (!ripperdoc) and Fixer Hub (!fixer).

Retained commands:
- !cw_buy, !cw_sell, !cw_install, !cw_inventory — fallbacks for cases
  exceeding the 25-item Discord dropdown limit.
- !cw_tx — transaction history lookup.

The previous rotating wholesale system (!cw_wh_list, !cw_wh_restock,
!cw_wh_add, !cw_wh_remove, !cw_wh_settings) has been removed; Ripperdocs
now buy directly from the full catalog via the hub or !cw_buy.

This cog is still loaded so that hub code can access the helper methods
(inventory loading, catalog management) via
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
    cw_shop_state_load,
    cw_shop_state_save,
)
from NightCityBot.utils.permissions import is_ripperdoc, is_fixer

logger = logging.getLogger(__name__)

_TX_LIMIT = 20


class CyberwareShop(commands.Cog):
    """Ripperdoc buy/sell cyberware marketplace."""

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

    async def _load_state(self) -> dict[str, Any]:
        default = {"sheet_url": "", "items_count": 0}
        state = await cw_shop_state_load()
        if not state:
            file_state = await helpers.load_json_file(self.state_file, default=default)
            if isinstance(file_state, dict) and file_state != default:
                await cw_shop_state_save(file_state)
            state = file_state
        if not isinstance(state, dict):
            state = default
        return state

    _PROTECTED_KEYS = ("ripperdoc_stores",)

    async def _save_state(self, state: dict[str, Any]) -> bool:
        existing = await self._load_state()
        if isinstance(existing, dict):
            for key in self._PROTECTED_KEYS:
                if key in existing and key not in state:
                    state[key] = existing[key]
        db_ok = await cw_shop_state_save(state)
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

    @staticmethod
    def _sorted_lots(lots: list[dict]) -> list[dict]:
        return sorted(lots, key=lambda l: l["item_name"])

    @staticmethod
    def _slot_ordered_lots(lots: list[dict]) -> list[dict]:
        from NightCityBot.utils.constants import CW_SLOT_ORDER, CW_SLOT_DISPLAY_NAMES
        buckets: dict[str, list[dict]] = {}
        for lot in lots:
            slot = (lot.get("slot") or "").strip().lower()
            if slot not in CW_SLOT_DISPLAY_NAMES:
                slot = "other"
            buckets.setdefault(slot, []).append(lot)
        ordered: list[dict] = []
        for key in CW_SLOT_ORDER:
            if key in buckets:
                ordered.extend(sorted(buckets[key], key=lambda l: l["item_name"]))
        if "other" in buckets:
            ordered.extend(sorted(buckets["other"], key=lambda l: l["item_name"]))
        return ordered

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

    @staticmethod
    def _slot_ordered_groups(inventory: list[dict]) -> list[dict]:
        from NightCityBot.utils.constants import CW_SLOT_ORDER, CW_SLOT_DISPLAY_NAMES
        groups = CyberwareShop._grouped_inventory(inventory)
        slot_buckets: dict[str, list[dict]] = {}
        for g in groups:
            sample = g["items"][0] if g.get("items") else {}
            slot_raw = (sample.get("slot") or "").strip().lower()
            if slot_raw not in CW_SLOT_DISPLAY_NAMES:
                slot_raw = "other"
            slot_buckets.setdefault(slot_raw, []).append(g)
        ordered: list[dict] = []
        for key in CW_SLOT_ORDER:
            if key in slot_buckets:
                ordered.extend(slot_buckets[key])
        if "other" in slot_buckets:
            ordered.extend(slot_buckets["other"])
        return ordered

    @commands.command(name="cw_buy")
    @is_ripperdoc()
    async def cw_buy(self, ctx: commands.Context, lot_number: int, qty: int = 1) -> None:
        """Purchase cyberware from the catalog by lot number.

        Usage: !cw_buy <lot_number> [qty=1]
        Open !ripperdoc and use **Catalogue List** to see the numbered list.

        Note: Consider using !ripperdoc for an interactive experience.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if qty < 1:
            await ctx.send("❌ qty must be at least 1.")
            return

        catalog = await cw_catalog_get_all()
        lots = [
            {
                "lot_id": f"cat-{item['name']}",
                "item_name": item["name"],
                "unit_cost": int(item.get("price", 0)),
                "cwp": item.get("cwp", ""),
                "slot": item.get("slot", ""),
                "qty_available": 99,
            }
            for item in catalog
        ]
        all_ordered = self._slot_ordered_lots(lots)

        if not all_ordered:
            await ctx.send(
                "❌ No cyberware is available in the catalog."
            )
            return

        if lot_number < 1 or lot_number > len(all_ordered):
            await ctx.send(
                f"❌ Invalid lot number **{lot_number}**. "
                f"Open `!ripperdoc` → **Catalogue List** to see available items "
                f"(1–{len(all_ordered)})."
            )
            return

        lot = all_ordered[lot_number - 1]
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
            reason=f"Cyberware catalog buy ×{qty}: {lot['item_name']}",
        )
        if not ok:
            await ctx.send("❌ Balance update failed. Please try again.")
            return

        async with self.lock:
            now_iso = self._now_iso()
            inventory = await self._load_inventory(ctx.author.id)
            new_items = []
            for _ in range(qty):
                new_item = {
                    "item_id": str(uuid.uuid4()),
                    "name": lot["item_name"],
                    "price_paid": unit_price,
                    "purchased_at": now_iso,
                }
                inventory.append(new_item)
                new_items.append(new_item)
            inv_ok = await self._save_inventory(ctx.author.id, inventory)
            if not inv_ok:
                for ni in new_items:
                    if ni in inventory:
                        inventory.remove(ni)
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason=f"Cyberware buy refund (save failed): {lot['item_name']}",
                )
                await ctx.send(
                    f"❌ Failed to save your purchase of **{lot['item_name']}**. "
                    "Your payment has been refunded."
                )
                return

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

        from NightCityBot.utils.constants import CW_SLOT_DISPLAY_NAMES
        ordered_groups = self._slot_ordered_groups(inventory)
        current_slot = None
        lines = []
        row = 1
        for g in ordered_groups:
            sample = g["items"][0] if g.get("items") else {}
            slot_raw = (sample.get("slot") or "").strip().lower()
            if slot_raw not in CW_SLOT_DISPLAY_NAMES:
                slot_raw = "other"
            if slot_raw != current_slot:
                current_slot = slot_raw
                header = CW_SLOT_DISPLAY_NAMES.get(slot_raw, "Other")
                lines.append(f"\n▬▬ {header} ▬▬")
            cwp = sample.get("cwp", "")
            cwp_tag = f" — [CWP: {cwp}]" if cwp else ""
            price = g["price_paid"]
            count = g["count"]
            date_str = g["date"] or "—"
            price_str = f"${price:,}" if price else "—"
            suffix = f" × {count}" if count > 1 else ""
            lines.append(f"`{row}.` **{g['name']}**{cwp_tag}{suffix} — paid {price_str} ea. ({date_str})")
            row += 1

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
                text=f"{len(inventory)} item(s) in {len(ordered_groups)} group(s) | Use !cw_sell <row> or !cw_install <row>"
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
        pre_groups = self._slot_ordered_groups(pre_inv)
        if inv_number < 1 or inv_number > len(pre_groups):
            await ctx.send(
                f"❌ Invalid row **{inv_number}**. Your inventory has {len(pre_groups)} group(s). "
                "Use `!cw_inventory` to see your numbered list."
            )
            return

        pre_group = pre_groups[inv_number - 1]
        item_name = pre_group["name"]
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
            locked_groups = self._slot_ordered_groups(inventory)

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
        pre_groups = self._slot_ordered_groups(pre_inv)
        if inv_number < 1 or inv_number > len(pre_groups):
            await ctx.send(
                f"❌ Invalid row **{inv_number}**. Your inventory has {len(pre_groups)} group(s). "
                "Use `!cw_inventory` to see your numbered list."
            )
            return

        async with self._locks.acquire(str(ctx.author.id)):
            inventory = await self._load_inventory(ctx.author.id)
            locked_groups = self._slot_ordered_groups(inventory)
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

