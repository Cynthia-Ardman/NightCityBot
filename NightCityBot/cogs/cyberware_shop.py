"""Cyberware shop cog — Ripperdoc buy/sell marketplace.

Ripperdocs source parts from an unlimited-stock wholesaler at sheet price,
then sell/install them for patients at their own price.  Every transaction
is audited to RIPPERDOC_LOG_CHANNEL_ID.
"""
import asyncio
import logging
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
from NightCityBot.utils.permissions import is_ripperdoc

logger = logging.getLogger(__name__)

_TX_LIMIT = 20


class CyberwareShop(commands.Cog):
    """Ripperdoc buy/sell cyberware marketplace."""

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
        """Return the cached item list from the last !cw_setsheet parse."""
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

        lines = [f"`{item['name']}` — ${item['price']:,}" for item in catalog]
        page_size = 20
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

    @commands.command(name="cw_buy")
    @is_ripperdoc()
    async def cw_buy(self, ctx: commands.Context, *, item_name: str) -> None:
        """Purchase a cyberware part from the wholesaler at the sheet price."""
        catalog = await self._load_catalog()
        if not catalog:
            await ctx.send(
                "❌ Cyberware catalog is empty. "
                "An admin must run `!cw_setsheet <url>` first."
            )
            return

        item = self._lookup_item(catalog, item_name)
        if item is None:
            await ctx.send(
                f"❌ Item **{item_name}** not found in catalog. "
                "Use `!cw_catalog` to browse available items."
            )
            return

        price = item["price"]

        balance = await self.unbelievaboat.get_balance(ctx.author.id)
        if balance is None:
            await ctx.send("❌ Could not fetch your balance. Please try again.")
            return

        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        total_funds = cash + bank
        if total_funds < price:
            await ctx.send(
                f"❌ Insufficient funds. **{item['name']}** costs ${price:,} "
                f"but you only have ${total_funds:,}."
            )
            return

        cash_deduct = min(price, cash)
        bank_deduct = max(0, price - cash)

        ok = await self.unbelievaboat.update_balance(
            ctx.author.id,
            {"cash": -cash_deduct, "bank": -bank_deduct},
            reason=f"Cyberware buy: {item['name']}",
        )
        if not ok:
            await ctx.send("❌ Balance update failed. Please try again.")
            return

        async with self.lock:
            inventory = await self._load_inventory(ctx.author.id)
            inventory.append(item["name"])
            await self._save_inventory(ctx.author.id, inventory)

            tx = {
                "tx_id": str(uuid.uuid4()),
                "tx_type": "BUY",
                "ts": self._now_iso(),
                "ripperdoc_id": str(ctx.author.id),
                "ripperdoc_name": ctx.author.display_name,
                "item": item["name"],
                "price": price,
            }
            await self._append_tx(tx)

        log_ch = await self._log_channel()
        receipt = (
            f"🛒 **CYBERWARE PURCHASE**\n"
            f"Ripperdoc: {ctx.author.mention}\n"
            f"Item: **{item['name']}**\n"
            f"Price paid: **${price:,}**"
        )
        if log_ch:
            await log_ch.send(receipt, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Purchased **{item['name']}** for **${price:,}**. "
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
