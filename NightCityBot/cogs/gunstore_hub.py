"""Unified !gunstore hub command — interactive gun store interface.

Consolidates the separate guns_wh_* command set into a single interactive hub
with Discord dropdowns, buttons, and inline component flows.
"""
import asyncio
import logging
import math
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView, send_ephemeral, respond_ephemeral
from NightCityBot.utils.db import (
    pi_add_item,
    ih_record_event,
    pt_create,
    gun_catalog_get_all,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active
from NightCityBot.utils.inline_helpers import collect_text_input, QtySelectView
from NightCityBot.utils.panel_context import PanelContext

logger = logging.getLogger(__name__)

GUN_STORE_EMPLOYEE_ROLE_ID = config.GUN_STORE_EMPLOYEE_ROLE_ID


def _is_store_owner_member(member: discord.Member) -> bool:
    raw = config.WHOLESALER_STORE_ROLE_IDS
    store_ids = {int(raw)} if isinstance(raw, (int, float, str)) and str(raw).strip().isdigit() else {int(x) for x in raw}
    return any(r.id in store_ids for r in member.roles)


def _is_employee_member(member: discord.Member) -> bool:
    return any(r.id == GUN_STORE_EMPLOYEE_ROLE_ID for r in member.roles)


def _is_black_market_owner(user_id: int) -> bool:
    return user_id in config.BLACK_MARKET_OWNER_IDS


def _find_employee_store(state: dict, guild_id: int, user_id: int) -> tuple:
    prefix = f"{guild_id}:"
    for store_id, store in state.get("stores", {}).items():
        if store_id.startswith(prefix) and user_id in store.get("employees", []):
            return store_id, store
    return None, None


def _find_accessible_stores(state: dict, guild_id: int, user_id: int, member) -> list:
    results = []
    is_owner = _is_store_owner_member(member) if member else False
    if is_owner:
        store_id = f"{guild_id}:{user_id}"
        store = state.get("stores", {}).get(store_id)
        if store:
            results.append((store_id, store))
    prefix = f"{guild_id}:"
    if member and _is_employee_member(member):
        for sid, s in state.get("stores", {}).items():
            if sid.startswith(prefix) and user_id in s.get("employees", []):
                if not any(r[0] == sid for r in results):
                    results.append((sid, s))
    if not results and is_owner:
        store_id = f"{guild_id}:{user_id}"
        results.append((store_id, state.get("stores", {}).get(store_id, {})))
    return results


async def _show_gun_inventory(interaction, store, store_id):
    if not store or not store.get("lots"):
        await send_ephemeral(interaction, "Store inventory is empty.")
        return
    from NightCityBot.utils.helpers import format_gun_lines_grouped
    lines = format_gun_lines_grouped(store["lots"], qty_key="qty_remaining", max_items=30)
    if not lines:
        await send_ephemeral(interaction, "Store inventory is empty.")
        return
    store_name = store.get("store_name") or f"Store {store_id}"
    embed = discord.Embed(
        title=f"📦 {store_name}",
        description="\n".join(lines[:30]),
        color=discord.Color.dark_gold(),
    )
    await send_ephemeral(interaction, embed=embed)


async def _open_sell_for_store(cog, interaction, guns_cog, store_id, store):
    if not store or not store.get("lots"):
        await send_ephemeral(interaction, "Store inventory is empty. Buy from wholesale first.")
        return
    available = [l for l in store["lots"] if int(l.get("qty_remaining", 0)) > 0]
    if not available:
        await send_ephemeral(interaction, "Store inventory is empty.")
        return
    ctx = PanelContext(interaction)
    view = GunSellSetupView(cog, ctx, available, store_id)
    msg = "**Step 1** — Select the customer and the gun to sell:"
    if view.truncated:
        msg += f"\n⚠️ Showing first 25 of {len(available)} lots."
    await send_ephemeral(interaction, msg, view=view)


class GunstoreMenuView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            guild = interaction.client.get_guild(config.GUILD_ID)
            if guild:
                member = guild.get_member(interaction.user.id)
        if member is None:
            await respond_ephemeral(interaction, "Could not verify your role.")
            return False
        if _is_store_owner_member(member) or _is_employee_member(member) or member.guild_permissions.administrator:
            return True
        await respond_ephemeral(interaction, "This panel is for Store Owners and Employees only.")
        return False

    @discord.ui.button(label="Buy from Wholesale", style=discord.ButtonStyle.primary, emoji="🛒", row=0, custom_id="gunstore:buy_wholesale")
    async def buy_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await respond_ephemeral(interaction, 
                "Only Store Owners can buy from wholesale.")
            return
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return

        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
        store = state.get("stores", {}).get(store_id, {})
        is_bm = store.get("store_type") == "black_market" or _is_black_market_owner(interaction.user.id)

        if is_bm:
            catalog = await gun_catalog_get_all()
            lots = _build_black_market_lots(catalog)
            if not lots:
                await send_ephemeral(interaction, "No Black Market stock available.")
                return
            ctx = PanelContext(interaction)
            view = GunBuySelect(cog, ctx, lots, guns_cog, black_market=True)
            await send_ephemeral(interaction, "🏴 **Black Market** — select a gun to buy:", view=view)
        else:
            state = await guns_cog._load_state()
            lots = [l for l in state.get("wholesale_lots", []) if int(l.get("qty_available", 0)) > 0]
            if not lots:
                await send_ephemeral(interaction, "No wholesale stock available.")
                return
            ctx = PanelContext(interaction)
            view = GunBuySelect(cog, ctx, lots, guns_cog)
            await send_ephemeral(interaction, "Select a gun to buy:", view=view)

    @discord.ui.button(label="Wholesale List", style=discord.ButtonStyle.secondary, emoji="📋", row=0, custom_id="gunstore:wholesale_list")
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        lots = state.get("wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await send_ephemeral(interaction, "No wholesale stock available.")
            return
        from NightCityBot.utils.helpers import format_gun_lines_grouped
        lines = format_gun_lines_grouped(available, qty_key="qty_available", max_items=30)
        embed = discord.Embed(
            title="🔫 Gun Wholesale",
            description="\n".join(lines),
            color=discord.Color.dark_gold(),
        )
        await send_ephemeral(interaction, embed=embed)

    @discord.ui.button(label="Sell to Customer", style=discord.ButtonStyle.success, emoji="🔫", row=1, custom_id="gunstore:sell_customer")
    async def sell_to_customer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        accessible = _find_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not accessible:
            await send_ephemeral(interaction, 
                "You are not assigned to any store. Ask a Store Owner to add you as an employee.")
            return
        if len(accessible) == 1:
            store_id, store = accessible[0]
            await _open_sell_for_store(cog, interaction, guns_cog, store_id, store)
        else:
            ctx = PanelContext(interaction)
            view = _StorePickerForAction(cog, ctx, accessible, action="sell")
            await send_ephemeral(interaction, 
                "You have access to multiple stores. Select which store to sell from:",
                view=view)

    @discord.ui.button(label="My Store Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=1, custom_id="gunstore:my_inv")
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        stores = _find_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not stores:
            await send_ephemeral(interaction, "You are not assigned to any store.")
            return
        if len(stores) > 1:
            ctx = PanelContext(interaction)
            view = _StorePickerForAction(cog, ctx, stores, action="view_inventory")
            await send_ephemeral(interaction, 
                "📦 **Select which store inventory to view:**", view=view)
            return
        store_id, store = stores[0]
        await _show_gun_inventory(interaction, store, store_id)

    @discord.ui.button(label="Manage Store", style=discord.ButtonStyle.danger, emoji="⚙️", row=2, custom_id="gunstore:manage_store")
    async def manage_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await respond_ephemeral(interaction, 
                "Only Store Owners can manage their store.")
            return
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        store_name = None
        if guns_cog:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
            store = state.get("stores", {}).get(store_id)
            if store:
                store_name = store.get("store_name")
        ctx = PanelContext(interaction)
        view = _ManageGunStoreView(cog, ctx)
        header = "⚙️ **Manage Store"
        if store_name:
            header += f" — {store_name}"
        header += "** — choose an action:"
        await respond_ephemeral(interaction, header, view=view)

    @discord.ui.button(label="Manage Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=2, custom_id="gunstore:manage_employees")
    async def manage_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await respond_ephemeral(interaction, 
                "Only Store Owners can manage employees.")
            return
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ManageEmployeesView(cog, ctx)
        await respond_ephemeral(interaction, 
            "👥 **Manage Employees** — choose an action:", view=view)

    @discord.ui.button(label="Manage Buyers", style=discord.ButtonStyle.secondary, emoji="📝", row=2, custom_id="gunstore:manage_buyers")
    async def manage_buyers(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ManageBuyersView(cog, ctx)
        await respond_ephemeral(interaction, 
            "📝 **Manage Buyers** — choose an action:", view=view)


def _build_black_market_lots(catalog: list[dict]) -> list[dict]:
    multiplier = config.BLACK_MARKET_PRICE_MULTIPLIER
    lots = []
    for entry in catalog:
        if entry.get("status") != "live":
            continue
        if entry.get("restriction") not in ("controlled", "restricted"):
            continue
        lots.append({
            "lot_id": f"bm-{entry['gun_name']}",
            "gun_name": entry["gun_name"],
            "gun_level": entry.get("gun_level", "L"),
            "weapon_type": entry.get("weapon_type", ""),
            "gun_category": entry.get("gun_category", ""),
            "unit_cost": math.ceil(int(entry.get("price", 0)) * multiplier),
            "qty_available": 99,
            "restriction": entry["restriction"],
            "black_market": True,
        })
    lots.sort(key=lambda l: l["gun_name"])
    return lots


class GunBuySelect(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, lots: list, guns_cog,
                 *, black_market: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.guns_cog = guns_cog
        self.black_market = black_market
        options = []
        for i, lot in enumerate(lots[:25]):
            r = lot.get("restriction", "basic")
            r_tag = f" [{r.title()}]" if r != "basic" else ""
            label = f"{lot['gun_name']}{r_tag} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))
        self.select = discord.ui.Select(placeholder="Choose a gun...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        lot = self.lots[idx]
        max_qty = int(lot.get("qty_available", 1))
        qty_view = QtySelectView(interaction.user.id, max_qty)
        await respond_ephemeral(interaction, 
            f"**{lot['gun_name']}** — how many? (max {max_qty})",
            view=qty_view)
        await qty_view.wait()
        if qty_view.result is None:
            await send_ephemeral(interaction, "⏰ Timed out.")
            return
        await _process_gun_buy(self.cog, interaction, self.ctx, lot, self.guns_cog, qty_view.result,
                               black_market=self.black_market)


async def _process_gun_buy(cog, interaction, ctx, lot, guns_cog, qty, *, black_market=False):
    if qty < 1:
        await send_ephemeral(interaction, "Quantity must be at least 1.")
        return
    if qty > int(lot.get("qty_available", 0)):
        await send_ephemeral(interaction, 
            f"Only {lot['qty_available']} available.")
        return

    unit_cost = int(lot["unit_cost"])
    total = unit_cost * qty
    member = ctx.author

    balance = await cog.unbelievaboat.get_balance(member.id)
    if balance is None:
        await send_ephemeral(interaction, "Could not fetch your balance.")
        return
    cash = int(balance.get("cash", 0))
    bank = int(balance.get("bank", 0))
    if cash + bank < total:
        await send_ephemeral(interaction, 
            f"You cannot afford ${total:,} (you have ${cash + bank:,}).")
        return

    cash_deduct = min(max(cash, 0), total)
    bank_deduct = max(0, total - cash_deduct)
    ok = await cog.unbelievaboat.update_balance(
        member.id,
        {"cash": -cash_deduct, "bank": -bank_deduct},
        reason=f"Gun wholesale buy: {lot['gun_name']} x{qty}",
    )
    if not ok:
        await send_ephemeral(interaction, "Payment failed.")
        return

    async with guns_cog.lock:
        state = await guns_cog._load_state()

        if not black_market:
            lots_list = state.get("wholesale_lots", [])
            target_lot = None
            for l in lots_list:
                if l.get("lot_id") == lot.get("lot_id"):
                    target_lot = l
                    break
            if not target_lot or int(target_lot.get("qty_available", 0)) < qty:
                await cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="Gun wholesale refund — stock depleted",
                )
                await send_ephemeral(interaction, "Stock depleted. Refunded.")
                return
            target_lot["qty_available"] = int(target_lot["qty_available"]) - qty

        store_id = guns_cog._store_id(ctx.guild.id, member.id)
        default_type = "black_market" if black_market else "standard"
        store = state.setdefault("stores", {}).setdefault(
            store_id, {"owner_id": member.id, "lots": [], "controlled_buyers": [],
                       "store_type": default_type}
        )
        if black_market and store.get("store_type") != "black_market":
            store["store_type"] = "black_market"
        elif "store_type" not in store:
            store["store_type"] = "standard"
        store_lot_id = f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        item_ids = [str(uuid.uuid4()) for _ in range(qty)]
        store["lots"].append({
            "lot_id": store_lot_id,
            "gun_name": lot["gun_name"],
            "gun_level": lot.get("gun_level", "L"),
            "weapon_type": lot.get("weapon_type", ""),
            "gun_category": lot.get("gun_category", ""),
            "unit_cost": unit_cost,
            "qty_remaining": qty,
            "restriction": lot.get("restriction", "basic"),
            "item_ids": item_ids,
        })
        save_ok = await guns_cog._save_state(state)
        if not save_ok:
            logger.error("gun wholesale buy: _save_state failed after payment — refunding buyer=%s", member.id)
            await cog.unbelievaboat.update_balance(
                member.id,
                {"cash": cash_deduct, "bank": bank_deduct},
                reason="Gun wholesale refund — save failed",
            )
            await send_ephemeral(interaction, 
                "⚠️ Purchase failed (save error). Payment has been refunded. Please try again.")
            return

    event_type = "black_market_buy" if black_market else "wholesale_buy"
    for item_id in item_ids:
        await ih_record_event(
            item_id, event_type,
            actor_id=str(member.id),
            price=unit_cost,
            metadata={
                "gun_name": lot["gun_name"],
                "gun_level": lot.get("gun_level"),
                "lot_id": lot.get("lot_id"),
                "store_lot_id": store_lot_id,
            },
        )

    await send_ephemeral(interaction, 
        f"Purchased **{lot['gun_name']}** ×{qty} for **${total:,}**.")
    log_ch = await cog._log_channel()
    if log_ch:
        bm_label = "🏴 Black Market Purchase" if black_market else "🛒 Gun Wholesale Purchase"
        embed = discord.Embed(
            title=bm_label,
            color=discord.Color.dark_purple() if black_market else discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Store Owner", value=f"{member.mention}", inline=False)
        embed.add_field(name="Gun", value=lot["gun_name"], inline=True)
        embed.add_field(name="Qty", value=str(qty), inline=True)
        embed.add_field(name="Total", value=f"${total:,}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


GUN_APPROVALS_CHANNEL_ID = config.GUN_APPROVALS_CHANNEL_ID


class GunSellSetupView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context,
                 lots: list, store_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.store_id = store_id
        self.selected_customer: Optional[discord.Member] = None
        self.selected_lot_idx: Optional[int] = None
        self.selected_character: Optional[dict] = None
        self._character_select: Optional[discord.ui.Select] = None
        self._stock_select: Optional[discord.ui.Select] = None

        self.truncated = len(lots) > 25
        options = []
        for i, lot in enumerate(lots[:25]):
            restriction = lot.get("restriction", "basic")
            r_tag = f" [{restriction}]" if restriction != "basic" else ""
            qty = int(lot.get("qty_remaining", 0))
            label = f"{lot['gun_name']}{r_tag} (×{qty})"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))
        stock_select = discord.ui.Select(
            placeholder="Choose gun from your stock…",
            options=options,
            row=2,
        )
        stock_select.callback = self._on_stock_select
        self._stock_select = stock_select
        self.add_item(stock_select)

    def _status_content(self) -> str:
        parts = []
        if self.selected_customer:
            parts.append(f"Customer: **{self.selected_customer.display_name}** ✓")
        if self.selected_character:
            parts.append(f"Character: **{self.selected_character['name']}** ✓")
        if self.selected_lot_idx is not None:
            lot = self.lots[self.selected_lot_idx]
            parts.append(f"Gun: **{lot['gun_name']}** ✓")
        if not parts:
            return "Select a customer, character, and gun, then press Continue."
        return " — ".join(parts)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a customer…", row=0)
    async def customer_select(self, interaction: discord.Interaction,
                              select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await respond_ephemeral(interaction, 
                "Please select a server member.")
            return
        if isinstance(user, discord.Member):
            self.selected_customer = user
        else:
            guild = self.ctx.guild
            if guild:
                member = guild.get_member(user.id)
                if member:
                    self.selected_customer = member
                else:
                    await respond_ephemeral(interaction, 
                        "That user doesn't appear to be in this server.")
                    return
            else:
                await respond_ephemeral(interaction, 
                    "Could not resolve server member.")
                return
        self.selected_character = None
        characters = await get_active_characters(str(self.selected_customer.id))
        if not characters:
            await respond_ephemeral(interaction, 
                f"❌ {self.selected_customer.display_name} has no active characters. "
                "They must create a character before receiving items.")
            self.selected_customer = None
            return
        if self._character_select is not None:
            self.remove_item(self._character_select)
        char_options = []
        for ch in characters[:25]:
            char_options.append(discord.SelectOption(
                label=ch["name"][:100],
                value=ch["character_id"],
            ))
        char_select = discord.ui.Select(
            placeholder="Choose character…",
            options=char_options,
            row=1,
        )
        char_select.callback = self._on_character_select
        self._character_select = char_select
        self._characters = characters
        self.add_item(char_select)
        await interaction.response.edit_message(
            content=self._status_content(),
            view=self,
        )

    async def _on_character_select(self, interaction: discord.Interaction):
        char_id = interaction.data["values"][0]
        for ch in self._characters:
            if ch["character_id"] == char_id:
                self.selected_character = ch
                break
        if not self.selected_character:
            await respond_ephemeral(interaction, "Character not found.")
            return
        for opt in self._character_select.options:
            opt.default = (opt.value == char_id)
        await interaction.response.edit_message(
            content=self._status_content(),
            view=self,
        )

    async def _on_stock_select(self, interaction: discord.Interaction):
        self.selected_lot_idx = int(interaction.data["values"][0])
        selected_val = interaction.data["values"][0]
        for opt in self._stock_select.options:
            opt.default = (opt.value == selected_val)
        await interaction.response.edit_message(
            content=self._status_content(),
            view=self,
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if self.selected_customer is None:
            await respond_ephemeral(interaction, 
                "Please select a customer first.")
            return
        if self.selected_character is None:
            await respond_ephemeral(interaction, 
                "Please select a character for the customer.")
            return
        if self.selected_lot_idx is None:
            await respond_ephemeral(interaction, 
                "Please select a gun from your stock first.")
            return
        if not await ensure_character_active(self.selected_character["character_id"]):
            await respond_ephemeral(interaction, 
                f"❌ Character **{self.selected_character['name']}** is no longer active.")
            return
        lot = self.lots[self.selected_lot_idx]
        await interaction.response.defer(ephemeral=True)
        await send_ephemeral(interaction, 
            "📝 **Enter the sale price** (number only, `0` for free), or type `cancel`:")
        price_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if price_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            self.stop()
            return
        try:
            price = int(price_text.replace(",", "").replace("$", "").strip())
        except ValueError:
            await send_ephemeral(interaction, "Price must be a number.")
            self.stop()
            return
        if price < 0:
            await send_ephemeral(interaction, "Price cannot be negative.")
            self.stop()
            return
        await _process_gun_sell(
            self.cog, interaction, self.ctx, self.selected_customer,
            lot, self.store_id, self.selected_character or {}, price,
        )
        self.stop()


async def _process_gun_sell(cog, interaction, ctx, customer, lot, store_id, character, price):
    character_name = character.get("name", "")
    character_id = character.get("character_id")
    if not character_name:
        await send_ephemeral(interaction, "Character selection required.")
        return
    if character_id and not await ensure_character_active(character_id):
        await send_ephemeral(interaction, 
            f"❌ Character **{character_name}** is no longer active.")
        return

    gun_name = lot["gun_name"]
    restriction = lot.get("restriction", "basic")

    guns_cog = cog._guns_cog()
    if not guns_cog:
        await send_ephemeral(interaction, "Gun shop system unavailable.")
        return

    state = await guns_cog._load_state()
    store_data = state.get("stores", {}).get(store_id, {})
    _fallback_owner = int(store_id.split(":")[-1]) if ":" in store_id else ctx.author.id
    owner_id = store_data.get("owner_id", _fallback_owner)
    if isinstance(owner_id, str) and owner_id.isdigit():
        owner_id = int(owner_id)

    if restriction in ("controlled", "restricted"):
        state = await guns_cog._load_state()
        store = state.get("stores", {}).get(store_id)
        approved = store.get("controlled_buyers", []) if store else []
        if not _is_character_approved(approved, character_id, customer.id):
            approve_view = InlineApproveView(
                cog, ctx, guns_cog, store_id, customer,
                character_id=character_id, character_name=character_name,
            )
            approve_msg = await send_ephemeral(interaction, 
                f"**{gun_name}** is **{restriction}**. **{character_name}** ({customer.display_name}) is not on your approved list.\n"
                "Would you like to approve them and proceed?",
                view=approve_view,
                wait=True)
            approve_view.message = approve_msg
            await approve_view.wait()
            if not approve_view.approved:
                return

    if restriction == "restricted":
        fixer_ok = await _request_fixer_approval(
            cog, interaction, ctx, customer, gun_name, lot, price, character_name
        )
        if not fixer_ok:
            return

    confirm_view = GunDMConfirmView(recipient_id=customer.id, timeout=300)
    try:
        dm_msg = await customer.send(
            f"**{ctx.author.display_name}** wants to sell you **{gun_name}** "
            f"for **${price:,}** (character: **{character_name}**).\n"
            "Do you accept?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await send_ephemeral(interaction, 
            f"Cannot DM {customer.display_name}. They may have DMs disabled.")
        return

    await send_ephemeral(interaction, 
        f"Confirmation sent to {customer.display_name} via DM. Waiting...")
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Sale declined or timed out.", view=None)
        except Exception:
            pass
        await send_ephemeral(interaction,
            f"{customer.display_name} declined or didn't respond to the purchase of **{gun_name}**."
        )
        return

    try:
        await dm_msg.edit(view=None)
    except Exception:
        pass

    seller_credited = False
    cash_ded = 0
    bank_ded = 0
    if price > 0:
        balance = await cog.unbelievaboat.get_balance(customer.id)
        if balance is None:
            await send_ephemeral(interaction, f"Could not fetch {customer.display_name}'s balance. Sale cancelled.")
            return
        c_cash = int(balance.get("cash", 0))
        c_bank = int(balance.get("bank", 0))
        if c_cash + c_bank < price:
            await send_ephemeral(interaction,
                f"{customer.display_name} cannot afford ${price:,}. Sale cancelled."
            )
            return
        cash_ded = min(max(c_cash, 0), price)
        bank_ded = max(0, price - cash_ded)
        ok_debit = await cog.unbelievaboat.update_balance(
            customer.id,
            {"cash": -cash_ded, "bank": -bank_ded},
            reason=f"Gun purchase: {gun_name} from {ctx.author.display_name}",
        )
        if not ok_debit:
            await send_ephemeral(interaction, f"Payment failed for {customer.display_name}. Sale cancelled.")
            return
        ok_credit = await cog.unbelievaboat.update_balance(
            owner_id,
            {"cash": price},
            reason=f"Gun sale: {gun_name} to {customer.display_name}",
        )
        if ok_credit:
            seller_credited = True
        else:
            logger.error("gun sell: buyer debited but seller credit failed — creating pending transfer")
            await pt_create({
                "seller_id": str(owner_id),
                "buyer_id": str(customer.id),
                "item_id": str(uuid.uuid4()),
                "amount": price,
                "reason": f"Gun sell credit failed: {gun_name}",
            })
            await send_ephemeral(interaction,
                f"⚠️ Payment from {customer.display_name} succeeded but store owner payout failed. "
                "A pending transfer has been created — an admin will resolve it."
            )

    async with guns_cog.lock:
        state = await guns_cog._load_state()
        store = state.get("stores", {}).get(store_id)
        if not store:
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="Gun sale refund"
                    )
            await send_ephemeral(interaction, "Store not found. Refunded.")
            return
        lot_id = lot.get("lot_id")
        target_lot = None
        for l in store.get("lots", []):
            if l.get("lot_id") == lot_id:
                target_lot = l
                break
        if not target_lot or int(target_lot.get("qty_remaining", 0)) < 1:
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — out of stock"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="Gun sale refund — out of stock"
                    )
            await send_ephemeral(interaction, "Item out of stock. Refunded.")
            return
        target_lot["qty_remaining"] = int(target_lot["qty_remaining"]) - 1
        lot_item_ids = target_lot.get("item_ids", [])
        if lot_item_ids:
            item_id = lot_item_ids.pop(0)
        else:
            item_id = str(uuid.uuid4())
        if target_lot["qty_remaining"] <= 0:
            store["lots"].remove(target_lot)
        save_ok = await guns_cog._save_state(state)
        if not save_ok:
            target_lot["qty_remaining"] = int(target_lot.get("qty_remaining", 0)) + 1
            if target_lot not in store.get("lots", []):
                store.setdefault("lots", []).append(target_lot)
            target_lot.setdefault("item_ids", []).insert(0, item_id)

    if not save_ok:
        logger.error("gun sell: _save_state failed after payment — refunding customer=%s", customer.id)
        if price > 0:
            await cog.unbelievaboat.update_balance(
                customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — save failed"
            )
            if seller_credited:
                await cog.unbelievaboat.update_balance(
                    owner_id, {"bank": -price}, reason="Gun sale refund — save failed"
                )
        await send_ephemeral(interaction, "⚠️ Sale failed (save error). Payment has been refunded.")
        return

    pi_payload = {
        "item_id": item_id,
        "owner_id": str(customer.id),
        "character_name": character_name,
        "character_id": character_id,
        "item_type": "gun",
        "name": gun_name,
        "restriction": restriction,
        "description": "",
        "price_paid": price,
        "seller_id": str(owner_id),
        "seller_name": ctx.author.display_name,
    }
    gl = lot.get("gun_level", "")
    gc = lot.get("gun_category", "")
    wt = lot.get("weapon_type", "")
    level_map = {"L": "low", "M": "medium", "H": "high"}
    if gl:
        pi_payload["power_level"] = level_map.get(gl, gl.lower())
    if gc:
        pi_payload["weapon_subtype"] = gc.lower()
    if wt:
        pi_payload["weapon_type"] = wt
    pi_ok = await pi_add_item(pi_payload)
    if not pi_ok:
        logger.error("gunstore sell: pi_add_item failed — attempting compensation")
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store = state.get("stores", {}).get(store_id)
            if store is not None:
                existing_lot = next((l for l in store.get("lots", []) if l.get("lot_id") == lot_id), None)
                if existing_lot is not None:
                    existing_lot["qty_remaining"] = int(existing_lot.get("qty_remaining", 0)) + 1
                    existing_lot.setdefault("item_ids", []).insert(0, item_id)
                else:
                    store.setdefault("lots", []).append({
                        "lot_id": lot_id,
                        "gun_name": gun_name,
                        "unit_cost": price,
                        "restriction": restriction,
                        "qty_remaining": 1,
                        "item_ids": [item_id],
                        "gun_level": lot.get("gun_level", ""),
                        "weapon_type": lot.get("weapon_type", ""),
                        "gun_category": lot.get("gun_category", ""),
                    })
                await guns_cog._save_state(state)
                logger.info("gunstore sell: restored item_id=%s to store lot=%s", item_id, lot_id)
            else:
                logger.error("gunstore sell: could not restore item_id=%s — store not found for store_id=%s", item_id, store_id)
        if price > 0:
            refund_ok = await cog.unbelievaboat.update_balance(
                customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — item grant failed"
            )
            seller_refund_ok = True
            if seller_credited:
                seller_refund_ok = await cog.unbelievaboat.update_balance(
                    owner_id, {"bank": -price}, reason="Gun sale refund — item grant failed"
                )
            if not refund_ok or not seller_refund_ok:
                logger.critical(
                    "gunstore sell: refund ALSO failed — customer=%s owner=%s amount=%s gun=%s",
                    customer.id, owner_id, price, gun_name,
                )
                await pt_create({
                    "seller_id": str(owner_id),
                    "buyer_id": str(customer.id),
                    "item_id": item_id,
                    "amount": price,
                    "reason": f"Gun sale refund failed: {gun_name}",
                })
        await send_ephemeral(interaction,
            f"⚠️ Failed to add **{gun_name}** to {customer.display_name}'s inventory. "
            "Payment has been refunded. Please contact an admin."
        )
        return

    await ih_record_event(
        item_id, "player_sale",
        actor_id=str(ctx.author.id),
        target_id=str(customer.id),
        price=price,
        metadata={
            "gun_name": gun_name, "character": character_name,
            "restriction": restriction, "store_owner_id": str(owner_id),
        },
    )

    sold_by = ctx.author.display_name
    owner_note = ""
    if owner_id != ctx.author.id:
        owner_note = f" (payment to store owner <@{owner_id}>)"
    await send_ephemeral(interaction, 
        f"Sold **{gun_name}** to **{character_name}** ({customer.display_name}) for **${price:,}**.")
    log_ch = await cog._log_channel()
    if log_ch:
        confirm_text = (
            f"Sold **{gun_name}** to **{character_name}** ({customer.display_name}) for **${price:,}**."
        )
        embed = discord.Embed(
            title="🔫 Gun Sold",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Store Owner", value=f"<@{owner_id}>", inline=True)
        embed.add_field(name="Sold By", value=f"{ctx.author.mention}", inline=True)
        embed.add_field(name="Customer", value=f"{customer.mention} — {character_name}", inline=False)
        embed.add_field(name="Gun", value=gun_name, inline=True)
        embed.add_field(name="Price", value=f"${price:,}", inline=True)
        if restriction != "basic":
            embed.add_field(name="Restriction", value=restriction.title(), inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(content=confirm_text, embed=embed, allowed_mentions=discord.AllowedMentions.none())


class _FixerApprovalView(SafeView):
    def __init__(self, fixer_role_id: int):
        super().__init__(timeout=300)
        self.fixer_role_id = fixer_role_id
        self.approved: Optional[bool] = None
        self.approver: Optional[discord.Member] = None

    async def _check_approver(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not isinstance(user, discord.Member):
            await respond_ephemeral(interaction, "Must be used in a server.")
            return False
        if user.guild_permissions.administrator:
            return True
        if self.fixer_role_id and any(r.id == self.fixer_role_id for r in user.roles):
            return True
        await respond_ephemeral(interaction, "❌ Only Fixers or Admins can approve restricted sales.")
        return False

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_approver(interaction):
            return
        self.approved = True
        self.approver = interaction.user
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_approver(interaction):
            return
        self.approved = False
        self.approver = interaction.user
        await interaction.response.edit_message(view=None)
        self.stop()


async def _request_fixer_approval(cog, interaction, ctx, customer, gun_name, lot, price, character_name):
    channel = cog.bot.get_channel(GUN_APPROVALS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await cog.bot.fetch_channel(GUN_APPROVALS_CHANNEL_ID)
        except Exception:
            await send_ephemeral(interaction, 
                "Gun approvals channel not found. Cannot process restricted sales.")
            return False

    fixer_role_id = getattr(config, "FIXER_ROLE_ID", 0)
    fixer_role_id_int = int(fixer_role_id) if fixer_role_id else 0
    fixer_ping = f"<@&{fixer_role_id}>" if fixer_role_id else "**Fixers**"

    embed = discord.Embed(
        title="🔒 Restricted Gun Sale — Fixer Approval Required",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Seller", value=f"{ctx.author.mention}", inline=True)
    embed.add_field(name="Buyer", value=f"{customer.mention} (character: {character_name})", inline=True)
    embed.add_field(name="Gun", value=f"{gun_name} (Tier {lot.get('gun_level', '?')})", inline=True)
    embed.add_field(name="Price", value=f"${price:,}", inline=True)
    embed.set_footer(text="Click Approve or Deny. Expires in 5 minutes.")

    approval_view = _FixerApprovalView(fixer_role_id_int)

    try:
        msg = await channel.send(
            f"{fixer_ping} — restricted sale requires approval.",
            embed=embed,
            view=approval_view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except Exception:
        logger.exception("Failed to post restricted sale approval request")
        await send_ephemeral(interaction, 
            "Failed to post approval request to the approvals channel.")
        return False

    await send_ephemeral(interaction, 
        "⏳ Restricted sale pending Fixer approval. Waiting up to 5 minutes...")

    await approval_view.wait()

    if approval_view.approved is None:
        log_ch = await cog._log_channel()
        if log_ch:
            await log_ch.send(
                f"{ctx.author.mention} — restricted sale of **{gun_name}** timed out. No Fixer responded within 5 minutes."
            )
        try:
            embed.set_footer(text="EXPIRED — no response.")
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        return False

    if approval_view.approved:
        log_ch = await cog._log_channel()
        if log_ch:
            await log_ch.send(
                f"✅ Restricted sale of **{gun_name}** approved by {approval_view.approver.mention}. Processing..."
            )
        try:
            embed.set_footer(text=f"APPROVED by {approval_view.approver.display_name}")
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        return True
    else:
        log_ch = await cog._log_channel()
        if log_ch:
            await log_ch.send(
                f"❌ Restricted sale of **{gun_name}** denied by {approval_view.approver.mention}."
            )
        try:
            embed.set_footer(text=f"DENIED by {approval_view.approver.display_name}")
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        return False


class InlineApproveView(SafeView):
    def __init__(self, cog, ctx, guns_cog, store_id, customer, character_id=None, character_name=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guns_cog = guns_cog
        self.store_id = store_id
        self.customer = customer
        self.character_id = character_id
        self.character_name = character_name
        self.approved = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="⏰ Approval timed out — sale cancelled.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Approve & Sell", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_and_sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.guns_cog.lock:
            state = await self.guns_cog._load_state()
            store = state.get("stores", {}).get(self.store_id)
            if store:
                approved_list = store.setdefault("controlled_buyers", [])
                if self.character_id and not _is_character_approved(approved_list, self.character_id, self.customer.id):
                    approved_list.append({
                        "user_id": self.customer.id,
                        "character_id": self.character_id,
                        "character_name": self.character_name or "",
                    })
                elif not self.character_id and self.customer.id not in approved_list:
                    approved_list.append(self.customer.id)
                await self.guns_cog._save_state(state)
        self.approved = True
        label = self.character_name or self.customer.display_name
        await interaction.response.edit_message(
            content=f"✅ {label} approved. Proceeding with sale...",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.approved = False
        await interaction.response.edit_message(content="Sale cancelled.", view=None)
        self.stop()


def _is_character_approved(approved_list: list, character_id: str | None, user_id: int) -> bool:
    for entry in approved_list:
        if isinstance(entry, dict):
            if character_id and entry.get("character_id") == character_id:
                return True
        elif isinstance(entry, int) and entry == user_id:
            return True
    return False


def _remove_character_approval(approved_list: list, character_id: str) -> bool:
    for i, entry in enumerate(approved_list):
        if isinstance(entry, dict) and entry.get("character_id") == character_id:
            approved_list.pop(i)
            return True
    return False


class _ApproveBuyerView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, approve: bool = True):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.approve = approve
        self._selected_user: Optional[discord.Member] = None
        self._characters: list[dict] = []
        self._character_select: Optional[discord.ui.Select] = None

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a player…", row=0)
    async def buyer_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await respond_ephemeral(interaction, "Please select a member.")
            return

        guild = self.ctx.guild
        if not guild:
            await respond_ephemeral(interaction, "Must be used in server.")
            return

        if isinstance(raw_user, discord.Member):
            user = raw_user
        else:
            user = guild.get_member(raw_user.id)
            if user is None:
                try:
                    user = await guild.fetch_member(raw_user.id)
                except Exception:
                    await respond_ephemeral(interaction, "Could not find that member.")
                    return

        characters = await get_active_characters(str(user.id))
        if not characters:
            await respond_ephemeral(interaction, 
                f"❌ {user.display_name} has no active characters.")
            return

        self._selected_user = user
        self._characters = characters

        if self._character_select is not None:
            self.remove_item(self._character_select)

        char_options = [
            discord.SelectOption(label=ch["name"][:100], value=ch["character_id"])
            for ch in characters[:25]
        ]
        char_select = discord.ui.Select(
            placeholder="Choose character…",
            options=char_options,
            row=1,
        )
        char_select.callback = self._on_character_select
        self._character_select = char_select
        self.add_item(char_select)

        action_word = "approve" if self.approve else "unapprove"
        await interaction.response.edit_message(
            content=f"Player: **{user.display_name}** ✓ — Now select the character to {action_word}.",
            view=self,
        )

    async def _on_character_select(self, interaction: discord.Interaction):
        char_id = interaction.data["values"][0]
        selected_char = None
        for ch in self._characters:
            if ch["character_id"] == char_id:
                selected_char = ch
                break
        if not selected_char or not self._selected_user:
            await respond_ephemeral(interaction, "Selection error.")
            return

        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await respond_ephemeral(interaction, "Gun shop system unavailable.")
            return

        await interaction.response.defer(ephemeral=True)
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            actor = self.ctx.author
            guild = self.ctx.guild
            if isinstance(actor, discord.Member) and _is_employee_member(actor) and not _is_store_owner_member(actor):
                store_id, store = _find_employee_store(state, guild.id, actor.id)
            else:
                store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
                store = state.get("stores", {}).get(store_id)
            if not store:
                await send_ephemeral(interaction, "No store found. Buy stock first.")
                self.stop()
                return
            approved = store.setdefault("controlled_buyers", [])
            char_name = selected_char["name"]
            if self.approve:
                if _is_character_approved(approved, char_id, self._selected_user.id):
                    await send_ephemeral(interaction, 
                        f"{char_name} is already approved.")
                    self.stop()
                    return
                approved.append({
                    "user_id": self._selected_user.id,
                    "character_id": char_id,
                    "character_name": char_name,
                })
            else:
                if not _remove_character_approval(approved, char_id):
                    await send_ephemeral(interaction, 
                        f"{char_name} is not on your approved list.")
                    self.stop()
                    return
            await guns_cog._save_state(state)

        action = "added to" if self.approve else "removed from"
        log_ch = await self.cog._log_channel()
        if log_ch:
            emoji = "✅" if self.approve else "❌"
            try:
                await log_ch.send(
                    f"{emoji} **Controlled Buyer {'Approved' if self.approve else 'Removed'}** — "
                    f"**{char_name}** ({self._selected_user.display_name}) {action} "
                    f"{self.ctx.author.display_name}'s buyer list."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, 
            f"**{char_name}** ({self._selected_user.display_name}) {action} your controlled-buyer list.")
        self.stop()


class _UnapproveCharacterView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, approved: list, store_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.store_id = store_id
        self.approved = approved

        options = []
        for entry in approved[:25]:
            if isinstance(entry, dict):
                cname = entry.get("character_name", "Unknown")
                uid = entry.get("user_id", 0)
                cid = entry.get("character_id", "")
                options.append(discord.SelectOption(
                    label=cname[:100],
                    description=f"Player ID: {uid}",
                    value=cid,
                ))
            else:
                options.append(discord.SelectOption(
                    label=f"Player {entry} (legacy)",
                    value=f"legacy:{entry}",
                ))
        char_select = discord.ui.Select(
            placeholder="Choose character to unapprove…",
            options=options,
            row=0,
        )
        char_select.callback = self._on_select
        self.add_item(char_select)

    async def _on_select(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]

        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await respond_ephemeral(interaction, "Gun shop system unavailable.")
            return

        await interaction.response.defer(ephemeral=True)
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store = state.get("stores", {}).get(self.store_id)
            if not store:
                await send_ephemeral(interaction, "No store found.")
                self.stop()
                return
            approved = store.setdefault("controlled_buyers", [])
            if value.startswith("legacy:"):
                uid = int(value.split(":", 1)[1])
                if uid in approved:
                    approved.remove(uid)
                    label = f"Player <@{uid}>"
                else:
                    await send_ephemeral(interaction, "That entry is no longer on your list.")
                    self.stop()
                    return
            else:
                entry_name = None
                for e in approved:
                    if isinstance(e, dict) and e.get("character_id") == value:
                        entry_name = e.get("character_name", "Unknown")
                        break
                if not _remove_character_approval(approved, value):
                    await send_ephemeral(interaction, "That character is no longer on your list.")
                    self.stop()
                    return
                label = f"**{entry_name}**"
            await guns_cog._save_state(state)

        log_ch = await self.cog._log_channel()
        if log_ch:
            try:
                await log_ch.send(
                    f"❌ **Controlled Buyer Unapproved** — {label} "
                    f"removed from {self.ctx.author.display_name}'s buyer list."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, 
            f"{label} removed from your controlled-buyer list.")
        self.stop()


class _ManageEmployeesView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Add Employee", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _EmployeePickerView(cog, ctx, add=True)
        await respond_ephemeral(interaction, 
            "➕ **Select a member to add as employee:**", view=view)

    @discord.ui.button(label="Remove Employee", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _EmployeePickerView(cog, ctx, add=False)
        await respond_ephemeral(interaction, 
            "➖ **Select an employee to remove:**", view=view)

    @discord.ui.button(label="View Employees", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(self.ctx.guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(store_id)
        employees = store.get("employees", []) if store else []
        if not employees:
            await send_ephemeral(interaction, "No employees assigned to your store.")
            return
        lines = [f"<@{uid}>" for uid in employees]
        store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Gun Store"
        await send_ephemeral(interaction, 
            f"**{store_name} — Employees ({len(employees)}):**\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none())


class _GunEmployeeDMConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="✅ You accepted the employee offer.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="❌ You declined the employee offer.", view=None)
        self.stop()


class _EmployeePickerView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, add: bool = True):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.add = add

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member…", row=0)
    async def employee_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await respond_ephemeral(interaction, "Please select a member.")
            return
        guild = self.ctx.guild
        if not guild:
            await respond_ephemeral(interaction, "Must be used in server.")
            return
        if isinstance(raw_user, discord.Member):
            user = raw_user
        else:
            user = guild.get_member(raw_user.id)
            if user is None:
                try:
                    user = await guild.fetch_member(raw_user.id)
                except Exception:
                    await respond_ephemeral(interaction, "Could not find that member.")
                    return
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await respond_ephemeral(interaction, "Gun shop system unavailable.")
            return
        await interaction.response.defer(ephemeral=True)
        if self.add:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).get(store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": []})
            employees = store.get("employees", [])
            if user.id in employees:
                await send_ephemeral(interaction, 
                    f"{user.display_name} is already an employee.")
                self.stop()
                return
            if len(employees) >= 25:
                await send_ephemeral(interaction, 
                    "❌ Employee limit reached (25 max). Remove an employee before adding a new one.")
                self.stop()
                return
            store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Gun Store"
            dm_view = _GunEmployeeDMConfirmView(user.id, timeout=300)
            try:
                dm_msg = await user.send(
                    f"📋 **{self.ctx.author.display_name}** wants to hire you as an employee at **{store_name}**.\n"
                    "Do you accept?",
                    view=dm_view,
                )
            except discord.Forbidden:
                await send_ephemeral(interaction, 
                    f"❌ Could not DM {user.display_name}. They may have DMs disabled.")
                self.stop()
                return
            await send_ephemeral(interaction, 
                f"📨 Sent a DM to **{user.display_name}** — waiting for their response…")
            timed_out = await dm_view.wait()
            if timed_out or not dm_view.accepted:
                reason = "timed out" if timed_out else "declined"
                await send_ephemeral(interaction, 
                    f"❌ **{user.display_name}** {reason} the employee offer.")
                if timed_out:
                    try:
                        await dm_msg.edit(content="⏰ Employee offer expired.", view=None)
                    except Exception:
                        pass
                self.stop()
                return
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
                store = state.setdefault("stores", {}).setdefault(
                    store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": [],
                               "store_type": "standard"}
                )
                store.setdefault("store_type", "standard")
                employees = store.setdefault("employees", [])
                if user.id not in employees:
                    employees.append(user.id)
                    await guns_cog._save_state(state)
            emp_role = guild.get_role(GUN_STORE_EMPLOYEE_ROLE_ID)
            if emp_role and emp_role not in user.roles:
                try:
                    await user.add_roles(emp_role, reason=f"Hired as gun store employee at {store_name}")
                except discord.Forbidden:
                    pass
            log_ch = await self.cog._log_channel()
            if log_ch:
                try:
                    await log_ch.send(
                        f"➕ **Gun Store Employee Added** — {user.display_name} ({user.id}) "
                        f"hired at **{store_name}** by {self.ctx.author.display_name}."
                    )
                except Exception:
                    pass
            await send_ephemeral(interaction, 
                f"✅ **{user.display_name}** accepted and has been added as employee at **{store_name}**.")
        else:
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
                store = state.setdefault("stores", {}).setdefault(
                    store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": [],
                               "store_type": "standard"}
                )
                store.setdefault("store_type", "standard")
                employees = store.setdefault("employees", [])
                if user.id not in employees:
                    await send_ephemeral(interaction, 
                        f"{user.display_name} is not an employee.")
                    self.stop()
                    return
                employees.remove(user.id)
                still_employed = False
                prefix = f"{guild.id}:"
                for sid, s in state.get("stores", {}).items():
                    if sid.startswith(prefix) and user.id in s.get("employees", []):
                        still_employed = True
                        break
                await guns_cog._save_state(state)
            store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Gun Store"
            if not still_employed:
                emp_role = guild.get_role(GUN_STORE_EMPLOYEE_ROLE_ID)
                if emp_role and emp_role in user.roles:
                    try:
                        await user.remove_roles(emp_role, reason="Removed as gun store employee")
                    except discord.Forbidden:
                        pass
            log_ch = await self.cog._log_channel()
            if log_ch:
                try:
                    await log_ch.send(
                        f"➖ **Gun Store Employee Removed** — {user.display_name} ({user.id}) "
                        f"removed from **{store_name}** by {self.ctx.author.display_name}."
                    )
                except Exception:
                    pass
            await send_ephemeral(interaction, 
                f"{user.display_name} removed as employee from **{store_name}**.")
        self.stop()


class _StorePickerForAction(SafeView):
    def __init__(self, cog, ctx, stores: list, action: str = "sell"):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.stores = {sid: s for sid, s in stores}
        self.action = action
        options = []
        for sid, s in stores:
            label = s.get("store_name") or f"Store {sid}"
            oid = s.get("owner_id", "")
            desc = f"Owner: {oid}"[:100]
            options.append(discord.SelectOption(label=label[:100], value=sid, description=desc))
        select = discord.ui.Select(placeholder="Choose a store…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        store_id = interaction.data["values"][0]
        store = self.stores.get(store_id)
        if not store:
            await respond_ephemeral(interaction, "Store not found.")
            return
        await interaction.response.defer(ephemeral=True)
        if self.action == "sell":
            guns_cog = self.cog._guns_cog()
            await _open_sell_for_store(self.cog, interaction, guns_cog, store_id, store)
        elif self.action == "view_inventory":
            await _show_gun_inventory(interaction, store, store_id)
        self.stop()


class _ManageBuyersView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Approve Buyer", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def approve_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ApproveBuyerView(cog, ctx, approve=True)
        await respond_ephemeral(interaction, 
            "📝 **Select a player to approve:**", view=view)
        view.message = await interaction.original_response()

    @discord.ui.button(label="Unapprove Buyer", style=discord.ButtonStyle.danger, emoji="🚫", row=0)
    async def unapprove_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await respond_ephemeral(interaction, "Gun shop system unavailable.")
            return
        await interaction.response.defer(ephemeral=True)
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild = interaction.guild
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            store_id, store = _find_employee_store(state, guild.id if guild else 0, member.id)
        else:
            store_id = guns_cog._store_id(guild.id if guild else 0, interaction.user.id)
            store = state.get("stores", {}).get(store_id)
        approved = store.get("controlled_buyers", []) if store else []
        if not approved:
            await send_ephemeral(interaction, "Your approved-buyer list is empty — nothing to remove.")
            return
        ctx = PanelContext(interaction)
        view = _UnapproveCharacterView(cog, ctx, approved, store_id)
        view.message = await send_ephemeral(interaction, 
            "🚫 **Select a character to remove from your approved list:**", view=view, wait=True)

    @discord.ui.button(label="Approved Buyers", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def approved_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            store_id, store = _find_employee_store(state, interaction.guild.id, interaction.user.id)
        else:
            store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
            store = state.get("stores", {}).get(store_id)
        approved = store.get("controlled_buyers", []) if store else []
        if not approved:
            await send_ephemeral(interaction, "Controlled-buyer list is empty.")
            return
        lines = []
        for entry in approved[:25]:
            if isinstance(entry, dict):
                uid = entry.get("user_id", 0)
                cname = entry.get("character_name", "Unknown")
                lines.append(f"• **{cname}** (<@{uid}>)")
            else:
                lines.append(f"• <@{entry}> (legacy — player-level)")
        await send_ephemeral(interaction, 
            "**Approved Controlled Buyers:**\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none())


class _ManageGunStoreView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Create Store", style=discord.ButtonStyle.success, emoji="🏪", row=0)
    async def create_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
        existing = state.get("stores", {}).get(store_id)
        if existing and existing.get("store_name"):
            await send_ephemeral(interaction, 
                f"You already own a gun store: **{existing['store_name']}**.")
            return
        await send_ephemeral(interaction, 
            "🏪 **Enter a name for your new store** (e.g. `Hellfire Arms`), or type `cancel`:")
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        name = text.strip()[:100]
        if not name:
            await send_ephemeral(interaction, "Name cannot be empty.")
            return
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            st = "black_market" if _is_black_market_owner(interaction.user.id) else "standard"
            store = state.setdefault("stores", {}).setdefault(
                store_id, {"owner_id": interaction.user.id, "lots": [], "controlled_buyers": [],
                           "store_type": st}
            )
            store.setdefault("store_type", st)
            store["store_name"] = name
            await guns_cog._save_state(state)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and interaction.guild:
            raw = config.WHOLESALER_STORE_ROLE_IDS
            owner_role_id = int(raw) if isinstance(raw, (int, float, str)) and str(raw).strip().isdigit() else None
            if owner_role_id:
                owner_role = interaction.guild.get_role(owner_role_id)
                if owner_role and owner_role not in member.roles:
                    try:
                        await member.add_roles(owner_role, reason="Created gun store")
                    except discord.Forbidden:
                        pass
        log_ch = await cog._log_channel()
        if log_ch:
            try:
                await log_ch.send(
                    f"🏪 **Gun Store Created** — {interaction.user.display_name} ({interaction.user.id}) "
                    f"created store **{name}**."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, f"✅ Store **{name}** created!")

    @discord.ui.button(label="Change Store Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def change_store_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
        store = state.get("stores", {}).get(store_id)
        current_name = store.get("store_name") if store else None
        prompt = "✏️ "
        if current_name:
            prompt += f"Current name: **{current_name}**\n"
        prompt += "**Enter a new store name**, or type `cancel`:"
        await send_ephemeral(interaction, prompt)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        name = text.strip()[:100]
        if not name:
            await send_ephemeral(interaction, "Name cannot be empty.")
            return
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store = state.setdefault("stores", {}).setdefault(
                store_id, {"owner_id": interaction.user.id, "lots": [], "controlled_buyers": [],
                           "store_type": "standard"}
            )
            store.setdefault("store_type", "standard")
            store["store_name"] = name
            await guns_cog._save_state(state)
        log_ch = await cog._log_channel()
        if log_ch:
            old_label = f" (was **{current_name}**)" if current_name else ""
            try:
                await log_ch.send(
                    f"✏️ **Gun Store Renamed** — {interaction.user.display_name} ({interaction.user.id}) "
                    f"renamed store to **{name}**{old_label}."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, f"Store name changed to **{name}**.")

    @discord.ui.button(label="Transfer Ownership", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def transfer_ownership(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _GunTransferOwnerView(cog, ctx)
        await respond_ephemeral(interaction, 
            "🔄 **Select the new owner for your gun store:**", view=view)

    @discord.ui.button(label="Close Store", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def close_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _GunCloseConfirmView(cog, ctx)
        await respond_ephemeral(interaction, 
            "⚠️ **Are you sure you want to close your gun store?**\n"
            "This will:\n"
            "• Remove all employees\n"
            "• Delete the store name\n"
            "• Return a random 20% of inventory to wholesale at 75% of original price\n"
            "• Delete the remaining inventory\n\n"
            "**This action cannot be undone.**",
            view=view)


class _GunTransferDMConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="✅ You accepted the ownership transfer.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="❌ You declined the ownership transfer.", view=None)
        self.stop()


class _GunTransferOwnerView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the new owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await respond_ephemeral(interaction, "Please select a member.")
            return
        guild = self.ctx.guild
        if not guild:
            await respond_ephemeral(interaction, "Must be used in server.")
            return
        new_owner = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if new_owner is None:
            try:
                new_owner = await guild.fetch_member(raw_user.id)
            except Exception:
                await respond_ephemeral(interaction, "Could not find that member.")
                return
        if new_owner.id == self.ctx.author.id:
            await respond_ephemeral(interaction, "You already own this store.")
            return
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        new_store_id = guns_cog._store_id(guild.id, new_owner.id)
        existing = state.get("stores", {}).get(new_store_id)
        if existing and existing.get("store_name"):
            await send_ephemeral(interaction, 
                f"{new_owner.display_name} already owns a gun store.")
            return
        old_store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(old_store_id)
        if not store:
            await send_ephemeral(interaction, "You don't have a store to transfer.")
            return
        store_name = store.get("store_name") or "Gun Store"
        confirm_view = _GunTransferDMConfirmView(new_owner.id, timeout=300)
        try:
            dm = await new_owner.send(
                f"🔄 **{self.ctx.author.display_name}** wants to transfer **{store_name}** to you.\n"
                "Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await send_ephemeral(interaction, 
                f"Could not DM {new_owner.display_name}. They may have DMs disabled.")
            return
        await send_ephemeral(interaction, 
            f"📩 Sent a DM to {new_owner.display_name} for confirmation. Waiting…")
        await confirm_view.wait()
        if not confirm_view.accepted:
            reason = "declined" if confirm_view.accepted is False else "timed out"
            await send_ephemeral(interaction, 
                f"❌ Transfer {reason} by {new_owner.display_name}.")
            return
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            old_store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).pop(old_store_id, None)
            if not store:
                await send_ephemeral(interaction, "You don't have a store to transfer.")
                return
            new_store_id = guns_cog._store_id(guild.id, new_owner.id)
            store["owner_id"] = new_owner.id
            state.setdefault("stores", {})[new_store_id] = store
            await guns_cog._save_state(state)
        store_name = store.get("store_name") or "Gun Store"
        owner_role = guild.get_role(config.GUN_STORE_OWNER_ROLE_ID) if hasattr(config, "GUN_STORE_OWNER_ROLE_ID") else None
        if owner_role:
            old_owner_member = guild.get_member(self.ctx.author.id)
            if old_owner_member:
                try:
                    await old_owner_member.remove_roles(owner_role, reason=f"Gun store transferred to {new_owner.display_name}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            try:
                await new_owner.add_roles(owner_role, reason=f"Gun store transferred from {self.ctx.author.display_name}")
            except (discord.Forbidden, discord.HTTPException):
                pass
        await send_ephemeral(interaction, 
            f"✅ **{store_name}** has been transferred to {new_owner.display_name}.")
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔄 Gun Store Transferred",
                color=discord.Color.dark_gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store", value=store_name, inline=False)
            embed.add_field(name="From", value=f"{self.ctx.author.mention}", inline=True)
            embed.add_field(name="To", value=f"{new_owner.mention}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


class _GunCloseConfirmView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await send_ephemeral(interaction, "Gun shop system unavailable.")
            return
        guild = self.ctx.guild
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).pop(store_id, None)
            if not store:
                await send_ephemeral(interaction, "No store found to close.")
                return
            store_name = store.get("store_name") or "Gun Store"
            lots = [l for l in store.get("lots", []) if int(l.get("qty_remaining", 0)) > 0]
            returned_lots = []
            if lots:
                return_count = max(1, math.ceil(len(lots) * 0.2))
                to_return = random.sample(lots, min(return_count, len(lots)))
                wh_lots = state.setdefault("wholesale_lots", [])
                for lot in to_return:
                    new_lot = {
                        "lot_id": str(uuid.uuid4()),
                        "gun_name": lot["gun_name"],
                        "gun_level": lot.get("gun_level", "?"),
                        "restriction": lot.get("restriction", "basic"),
                        "unit_cost": int(int(lot.get("unit_cost", 0)) * 0.75),
                        "qty_available": int(lot.get("qty_remaining", 0)),
                    }
                    wh_lots.append(new_lot)
                    returned_lots.append(new_lot)
            await guns_cog._save_state(state)

        employees = store.get("employees", [])
        owner_role = guild.get_role(config.GUN_STORE_OWNER_ROLE_ID) if hasattr(config, "GUN_STORE_OWNER_ROLE_ID") else None
        emp_role = guild.get_role(GUN_STORE_EMPLOYEE_ROLE_ID)
        owner_member = guild.get_member(self.ctx.author.id)
        if owner_role and owner_member:
            try:
                await owner_member.remove_roles(owner_role, reason=f"Gun store {store_name} closed")
            except (discord.Forbidden, discord.HTTPException):
                pass
        for emp_id in employees:
            if emp_role:
                emp_member = guild.get_member(emp_id)
                if emp_member:
                    try:
                        await emp_member.remove_roles(emp_role, reason=f"Gun store {store_name} closed")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
        from NightCityBot.utils.db import cancel_pending_transfers_for_store
        try:
            cancelled = await cancel_pending_transfers_for_store(store_id)
            if cancelled:
                logger.info("Cancelled %d pending transfer(s) for closed store %s", cancelled, store_id)
        except Exception:
            logger.warning("Failed to cancel pending transfers for store %s", store_id, exc_info=True)

        summary = f"✅ **{store_name}** has been closed.\n"
        summary += f"• {len(employees)} employee(s) disassociated\n"
        summary += f"• {len(lots)} lot(s) in store\n"
        if returned_lots:
            returned_names = [f"**{l['gun_name']}** ×{l['qty_available']} @ ${l['unit_cost']:,}" for l in returned_lots]
            summary += f"• {len(returned_lots)} lot(s) returned to wholesale:\n  " + "\n  ".join(returned_names)
        else:
            summary += "• No items returned to wholesale"
        await send_ephemeral(interaction, summary)

        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Gun Store Closed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store", value=store_name, inline=False)
            embed.add_field(name="Owner", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Items Returned", value=str(len(returned_lots)), inline=True)
            embed.add_field(name="Items Deleted", value=str(max(0, len(lots) - len(returned_lots))), inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Store closure cancelled.", view=None)
        self.stop()


class GunDMConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="You accepted the purchase.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="You declined the purchase.", view=None)
        self.stop()


class GunstoreHub(commands.Cog, name="GunstoreHub"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.unbelievaboat = bot.unbelievaboat
        self._panel_view = GunstoreMenuView()
        bot.add_view(self._panel_view)

    def _guns_cog(self):
        return self.bot.cogs.get("GunsShopCog")

    async def _log_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "GUN_LOG_CHANNEL_ID", 0)
        if not ch_id:
            return None
        ch = self.bot.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(ch_id))
            except Exception:
                pass
        return ch


    @staticmethod
    def _panel_embed() -> discord.Embed:
        return discord.Embed(
            title="🔫 Gun Store",
            description=(
                "Welcome to the Gun Store panel.\n\n"
                "**Buy from Wholesale** — Purchase guns from wholesale *(owners only)*\n"
                "**Wholesale List** — Browse available wholesale stock\n"
                "**Sell to Customer** — Sell a gun to a customer (DM confirmation)\n"
                "**My Store Inventory** — View your store stock\n"
                "**Manage Store** — Create/rename store, transfer or close *(owners only)*\n"
                "**Manage Employees** — Add/remove store employees *(owners only)*\n"
                "**Manage Buyers** — Approve/unapprove/list controlled buyers"
            ),
            color=discord.Color.dark_gold(),
        )

    @staticmethod
    def _guide_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📘 Gun Store — How It Works",
            description=(
                "This panel is for gun store owners and employees. "
                "Use the buttons below to stock your store, sell to customers, and manage your team. "
                "All responses are private and **auto-delete after 5 minutes**."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="🛒 Buy from Wholesale",
            value="Purchase gun lots from the wholesale market to add to your store's stock. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="📋 Wholesale List",
            value="Browse what's currently available to buy from wholesale — no purchase required.",
            inline=False,
        )
        embed.add_field(
            name="🔫 Sell to Customer",
            value="Sell a gun from your store stock to a player. The customer will get a DM to confirm the purchase.",
            inline=False,
        )
        embed.add_field(
            name="📦 My Store Inventory",
            value="View your store's current stock and quantities.",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Manage Store",
            value="Create your store, change its name, transfer ownership, or close it down. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="👥 Manage Employees",
            value="Add or remove employees who can sell on your behalf. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="📝 Manage Buyers",
            value="Approve or remove players from your controlled-buyer list for restricted weapons.",
            inline=False,
        )
        return embed

    @commands.hybrid_command(name="gunstore")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def gunstore_hub(self, ctx: commands.Context):
        """Post (or refresh) the persistent Gun Store panel in the designated channel."""
        channel = self.bot.get_channel(config.GUN_HUB_CHANNEL_ID)
        if channel is None:
            await ctx.send("❌ Gun store hub channel not found.", ephemeral=True)
            return
        view = GunstoreMenuView()
        await channel.send(embed=self._guide_embed(), view=view)
        await ctx.send("✅ Gun Store panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GunstoreHub(bot))
