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
from NightCityBot.utils.interaction_safety import SafeView
from NightCityBot.utils.db import (
    pi_add_item,
    ih_record_event,
    pt_create,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active
from NightCityBot.utils.inline_helpers import collect_text_input, QtySelectView
from NightCityBot.utils.panel_context import PanelContext

logger = logging.getLogger(__name__)

GUN_STORE_EMPLOYEE_ROLE_ID = 1489618157722275910


def _is_store_owner_member(member: discord.Member) -> bool:
    raw = config.WHOLESALER_STORE_ROLE_IDS
    store_ids = {int(raw)} if isinstance(raw, (int, float, str)) and str(raw).strip().isdigit() else {int(x) for x in raw}
    return any(r.id in store_ids for r in member.roles)


def _is_employee_member(member: discord.Member) -> bool:
    return any(r.id == GUN_STORE_EMPLOYEE_ROLE_ID for r in member.roles)


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
        await interaction.followup.send("Store inventory is empty.", ephemeral=True)
        return
    lines = []
    for i, lot in enumerate(store["lots"], 1):
        qty = int(lot.get("qty_remaining", 0))
        if qty <= 0:
            continue
        restriction = lot.get("restriction", "basic")
        r_tag = f" [{restriction}]" if restriction != "basic" else ""
        lines.append(
            f"`{i}.` **{lot['gun_name']}**{r_tag} [{lot.get('gun_level', '?')}] "
            f"— ${int(lot.get('unit_cost', 0)):,} × {qty}"
        )
    if not lines:
        await interaction.followup.send("Store inventory is empty.", ephemeral=True)
        return
    store_name = store.get("store_name") or f"Store {store_id}"
    embed = discord.Embed(
        title=f"📦 {store_name}",
        description="\n".join(lines[:30]),
        color=discord.Color.dark_gold(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _open_sell_for_store(cog, interaction, guns_cog, store_id, store):
    if not store or not store.get("lots"):
        await interaction.followup.send("Store inventory is empty. Buy from wholesale first.", ephemeral=True)
        return
    available = [l for l in store["lots"] if int(l.get("qty_remaining", 0)) > 0]
    if not available:
        await interaction.followup.send("Store inventory is empty.", ephemeral=True)
        return
    ctx = PanelContext(interaction)
    view = GunSellSetupView(cog, ctx, available, store_id)
    msg = "**Step 1** — Select the customer and the gun to sell:"
    if view.truncated:
        msg += f"\n⚠️ Showing first 25 of {len(available)} lots."
    await interaction.followup.send(msg, view=view, ephemeral=True)


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
            await interaction.response.send_message("Could not verify your role.", ephemeral=True)
            return False
        if _is_store_owner_member(member) or _is_employee_member(member) or member.guild_permissions.administrator:
            return True
        await interaction.response.send_message("This panel is for Store Owners and Employees only.", ephemeral=True)
        return False

    @discord.ui.button(label="Buy from Wholesale", style=discord.ButtonStyle.primary, emoji="🛒", row=0, custom_id="gunstore:buy_wholesale")
    async def buy_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await interaction.response.send_message(
                "Only Store Owners can buy from wholesale.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        lots = [l for l in state.get("wholesale_lots", []) if int(l.get("qty_available", 0)) > 0]
        if not lots:
            await interaction.followup.send("No wholesale stock available.", ephemeral=True)
            return
        ctx = PanelContext(interaction)
        view = GunBuySelect(cog, ctx, lots, guns_cog)
        await interaction.followup.send("Select a gun to buy:", view=view, ephemeral=True)

    @discord.ui.button(label="Sell to Customer", style=discord.ButtonStyle.success, emoji="🔫", row=0, custom_id="gunstore:sell_customer")
    async def sell_to_customer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        accessible = _find_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not accessible:
            await interaction.followup.send(
                "You are not assigned to any store. Ask a Store Owner to add you as an employee.",
                ephemeral=True,
            )
            return
        if len(accessible) == 1:
            store_id, store = accessible[0]
            await _open_sell_for_store(cog, interaction, guns_cog, store_id, store)
        else:
            ctx = PanelContext(interaction)
            view = _StorePickerForAction(cog, ctx, accessible, action="sell")
            await interaction.followup.send(
                "You have access to multiple stores. Select which store to sell from:",
                view=view,
                ephemeral=True,
            )

    @discord.ui.button(label="My Store Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=1, custom_id="gunstore:my_inv")
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        stores = _find_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not stores:
            await interaction.followup.send("You are not assigned to any store.", ephemeral=True)
            return
        if len(stores) > 1:
            ctx = PanelContext(interaction)
            view = _StorePickerForAction(cog, ctx, stores, action="view_inventory")
            await interaction.followup.send(
                "📦 **Select which store inventory to view:**", view=view, ephemeral=True
            )
            return
        store_id, store = stores[0]
        await _show_gun_inventory(interaction, store, store_id)

    @discord.ui.button(label="Approve Buyer", style=discord.ButtonStyle.secondary, emoji="✅", row=1, custom_id="gunstore:approve_buyer")
    async def approve_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ApproveBuyerView(cog, ctx, approve=True)
        await interaction.response.send_message(
            "📝 **Select a buyer to approve:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Unapprove Buyer", style=discord.ButtonStyle.secondary, emoji="🚫", row=2, custom_id="gunstore:unapprove_buyer")
    async def unapprove_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ApproveBuyerView(cog, ctx, approve=False)
        await interaction.response.send_message(
            "📝 **Select a buyer to remove from your approved list:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Wholesale List", style=discord.ButtonStyle.secondary, emoji="📋", row=2, custom_id="gunstore:wholesale_list")
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        lots = state.get("wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await interaction.followup.send("No wholesale stock available.", ephemeral=True)
            return
        lines = []
        for i, lot in enumerate(available[:30], 1):
            r = lot.get("restriction", "basic")
            r_tag = f" [{r}]" if r != "basic" else ""
            lines.append(
                f"`{i}.` **{lot['gun_name']}**{r_tag} [{lot.get('gun_level', '?')}] "
                f"— ${int(lot['unit_cost']):,} × {lot['qty_available']}"
            )
        embed = discord.Embed(
            title="🔫 Gun Wholesale",
            description="\n".join(lines),
            color=discord.Color.dark_gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Approved Buyers", style=discord.ButtonStyle.secondary, emoji="📝", row=2, custom_id="gunstore:approved_list")
    async def approved_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
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
            await interaction.followup.send("Controlled-buyer list is empty.", ephemeral=True)
            return
        lines = [f"<@{uid}>" for uid in approved[:25]]
        await interaction.followup.send(
            "**Approved Controlled Buyers:**\n" + "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Set Store Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=3, custom_id="gunstore:set_name")
    async def set_store_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await interaction.response.send_message(
                "Only Store Owners can set the store name.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "✏️ **Enter your store name** (e.g. `Hellfire Arms`), or type `cancel`:",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        name = text.strip()[:100]
        if not name:
            await interaction.followup.send("Name cannot be empty.", ephemeral=True)
            return
        cog = interaction.client.get_cog("GunstoreHub")
        guns_cog = cog._guns_cog() if cog else None
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(interaction.guild.id, interaction.user.id)
            store = state.setdefault("stores", {}).setdefault(
                store_id, {"owner_id": interaction.user.id, "lots": [], "controlled_buyers": []}
            )
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
                        await member.add_roles(owner_role, reason="Set gun store name")
                    except discord.Forbidden:
                        pass
        await interaction.followup.send(
            f"Store name set to **{name}**.", ephemeral=True
        )

    @discord.ui.button(label="Manage Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=3, custom_id="gunstore:manage_employees")
    async def manage_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await interaction.response.send_message(
                "Only Store Owners can manage employees.", ephemeral=True
            )
            return
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ManageEmployeesView(cog, ctx)
        await interaction.response.send_message(
            "👥 **Manage Employees** — choose an action:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Manage Store", style=discord.ButtonStyle.danger, emoji="⚙️", row=4, custom_id="gunstore:manage_store")
    async def manage_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_employee_member(member) and not _is_store_owner_member(member):
            await interaction.response.send_message(
                "Only Store Owners can manage their store.", ephemeral=True
            )
            return
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _ManageGunStoreView(cog, ctx)
        await interaction.response.send_message(
            "⚙️ **Manage Store** — choose an action:", view=view, ephemeral=True
        )


class GunBuySelect(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, lots: list, guns_cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.guns_cog = guns_cog
        options = []
        for i, lot in enumerate(lots[:25]):
            label = f"{lot['gun_name']} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))
        self.select = discord.ui.Select(placeholder="Choose a gun...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        lot = self.lots[idx]
        max_qty = int(lot.get("qty_available", 1))
        qty_view = QtySelectView(interaction.user.id, max_qty)
        await interaction.response.send_message(
            f"**{lot['gun_name']}** — how many? (max {max_qty})",
            view=qty_view,
            ephemeral=True,
        )
        await qty_view.wait()
        if qty_view.result is None:
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)
            return
        await _process_gun_buy(self.cog, interaction, self.ctx, lot, self.guns_cog, qty_view.result)


async def _process_gun_buy(cog, interaction, ctx, lot, guns_cog, qty):
    if qty < 1:
        await interaction.followup.send("Quantity must be at least 1.", ephemeral=True)
        return
    if qty > int(lot.get("qty_available", 0)):
        await interaction.followup.send(
            f"Only {lot['qty_available']} available.", ephemeral=True
        )
        return

    unit_cost = int(lot["unit_cost"])
    total = unit_cost * qty
    member = ctx.author

    balance = await cog.unbelievaboat.get_balance(member.id)
    if balance is None:
        await interaction.followup.send("Could not fetch your balance.", ephemeral=True)
        return
    cash = int(balance.get("cash", 0))
    bank = int(balance.get("bank", 0))
    if cash + bank < total:
        await interaction.followup.send(
            f"You cannot afford ${total:,} (you have ${cash + bank:,}).", ephemeral=True
        )
        return

    cash_deduct = min(max(cash, 0), total)
    bank_deduct = max(0, total - cash_deduct)
    ok = await cog.unbelievaboat.update_balance(
        member.id,
        {"cash": -cash_deduct, "bank": -bank_deduct},
        reason=f"Gun wholesale buy: {lot['gun_name']} x{qty}",
    )
    if not ok:
        await interaction.followup.send("Payment failed.", ephemeral=True)
        return

    async with guns_cog.lock:
        state = await guns_cog._load_state()
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
            await interaction.followup.send("Stock depleted. Refunded.", ephemeral=True)
            return
        target_lot["qty_available"] = int(target_lot["qty_available"]) - qty
        store_id = guns_cog._store_id(ctx.guild.id, member.id)
        store = state.setdefault("stores", {}).setdefault(
            store_id, {"owner_id": member.id, "lots": [], "controlled_buyers": []}
        )
        store_lot_id = f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        item_ids = [str(uuid.uuid4()) for _ in range(qty)]
        store["lots"].append({
            "lot_id": store_lot_id,
            "gun_name": lot["gun_name"],
            "gun_level": lot.get("gun_level", "L"),
            "weapon_type": lot.get("weapon_type", ""),
            "unit_cost": unit_cost,
            "qty_remaining": qty,
            "restriction": lot.get("restriction", "basic"),
            "item_ids": item_ids,
        })
        await guns_cog._save_state(state)

    for item_id in item_ids:
        await ih_record_event(
            item_id, "wholesale_buy",
            actor_id=str(member.id),
            price=unit_cost,
            metadata={
                "gun_name": lot["gun_name"],
                "gun_level": lot.get("gun_level"),
                "lot_id": lot.get("lot_id"),
                "store_lot_id": store_lot_id,
            },
        )

    await interaction.followup.send(
        f"Purchased **{lot['gun_name']}** ×{qty} for **${total:,}**.",
        ephemeral=True,
    )
    log_ch = await cog._log_channel()
    if log_ch:
        embed = discord.Embed(
            title="🛒 Gun Wholesale Purchase",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Store Owner", value=f"{member.mention}", inline=False)
        embed.add_field(name="Gun", value=lot["gun_name"], inline=True)
        embed.add_field(name="Qty", value=str(qty), inline=True)
        embed.add_field(name="Total", value=f"${total:,}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


GUN_APPROVALS_CHANNEL_ID = 1489460511199465693


class GunSellSetupView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context,
                 lots: list, store_id: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.store_id = store_id
        self.selected_customer: Optional[discord.Member] = None
        self.selected_lot_idx: Optional[int] = None
        self.selected_character: Optional[dict] = None
        self._character_select: Optional[discord.ui.Select] = None

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
        self.add_item(stock_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a customer…", row=0)
    async def customer_select(self, interaction: discord.Interaction,
                              select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await interaction.response.send_message(
                "Please select a server member.", ephemeral=True
            )
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
                    await interaction.response.send_message(
                        "That user doesn't appear to be in this server.", ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message(
                    "Could not resolve server member.", ephemeral=True
                )
                return
        self.selected_character = None
        characters = await get_active_characters(str(self.selected_customer.id))
        if not characters:
            await interaction.response.send_message(
                f"❌ {self.selected_customer.display_name} has no active characters. "
                "They must create a character before receiving items.",
                ephemeral=True,
            )
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
        await interaction.response.send_message(
            f"Customer: **{self.selected_customer.display_name}** ✓ — Now select their character.",
            ephemeral=True,
        )

    async def _on_character_select(self, interaction: discord.Interaction):
        char_id = interaction.data["values"][0]
        for ch in self._characters:
            if ch["character_id"] == char_id:
                self.selected_character = ch
                break
        if self.selected_character:
            await interaction.response.send_message(
                f"Character: **{self.selected_character['name']}** ✓", ephemeral=True
            )
        else:
            await interaction.response.send_message("Character not found.", ephemeral=True)

    async def _on_stock_select(self, interaction: discord.Interaction):
        self.selected_lot_idx = int(interaction.data["values"][0])
        lot = self.lots[self.selected_lot_idx]
        await interaction.response.send_message(
            f"Gun: **{lot['gun_name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if self.selected_customer is None:
            await interaction.response.send_message(
                "Please select a customer first.", ephemeral=True
            )
            return
        if self.selected_character is None:
            await interaction.response.send_message(
                "Please select a character for the customer.", ephemeral=True
            )
            return
        if self.selected_lot_idx is None:
            await interaction.response.send_message(
                "Please select a gun from your stock first.", ephemeral=True
            )
            return
        if not await ensure_character_active(self.selected_character["character_id"]):
            await interaction.response.send_message(
                f"❌ Character **{self.selected_character['name']}** is no longer active.",
                ephemeral=True,
            )
            return
        lot = self.lots[self.selected_lot_idx]
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter the sale price** (number only, `0` for free), or type `cancel`:",
            ephemeral=True,
        )
        price_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if price_text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            self.stop()
            return
        try:
            price = int(price_text.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("Price must be a number.", ephemeral=True)
            self.stop()
            return
        if price < 0:
            await interaction.followup.send("Price cannot be negative.", ephemeral=True)
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
        await interaction.followup.send("Character selection required.", ephemeral=True)
        return
    if character_id and not await ensure_character_active(character_id):
        await interaction.followup.send(
            f"❌ Character **{character_name}** is no longer active.", ephemeral=True
        )
        return

    gun_name = lot["gun_name"]
    restriction = lot.get("restriction", "basic")

    guns_cog = cog._guns_cog()
    if not guns_cog:
        await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
        return

    state = await guns_cog._load_state()
    store_data = state.get("stores", {}).get(store_id, {})
    owner_id = store_data.get("owner_id", ctx.author.id)
    if isinstance(owner_id, str) and owner_id.isdigit():
        owner_id = int(owner_id)

    if restriction in ("controlled", "restricted"):
        state = await guns_cog._load_state()
        store = state.get("stores", {}).get(store_id)
        approved = store.get("controlled_buyers", []) if store else []
        if customer.id not in approved:
            approve_view = InlineApproveView(
                cog, ctx, guns_cog, store_id, customer
            )
            approve_msg = await interaction.followup.send(
                f"**{gun_name}** is **{restriction}**. {customer.display_name} is not on your approved list.\n"
                "Would you like to approve them and proceed?",
                view=approve_view,
                ephemeral=True,
                wait=True,
            )
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

    confirm_view = GunDMConfirmView(recipient_id=customer.id, timeout=60)
    try:
        dm_msg = await customer.send(
            f"**{ctx.author.display_name}** wants to sell you **{gun_name}** "
            f"for **${price:,}** (character: **{character_name}**).\n"
            "Do you accept?",
            view=confirm_view,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"Cannot DM {customer.display_name}. They may have DMs disabled.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Confirmation sent to {customer.display_name} via DM. Waiting...", ephemeral=True
    )
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Sale declined or timed out.", view=None)
        except Exception:
            pass
        await ctx.send(
            f"{ctx.author.mention} — {customer.display_name} declined or didn't respond to the purchase of **{gun_name}**."
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
            await ctx.send(f"Could not fetch {customer.display_name}'s balance. Sale cancelled.")
            return
        c_cash = int(balance.get("cash", 0))
        c_bank = int(balance.get("bank", 0))
        if c_cash + c_bank < price:
            await ctx.send(
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
            await ctx.send(f"Payment failed for {customer.display_name}. Sale cancelled.")
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
            await ctx.send(
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
                        owner_id, {"cash": -price}, reason="Gun sale refund"
                    )
            await ctx.send("Store not found. Refunded.")
            return
        target_lot = None
        for l in store.get("lots", []):
            if l.get("lot_id") == lot.get("lot_id"):
                target_lot = l
                break
        if not target_lot or int(target_lot.get("qty_remaining", 0)) < 1:
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — out of stock"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"cash": -price}, reason="Gun sale refund — out of stock"
                    )
            await ctx.send("Item out of stock. Refunded.")
            return
        target_lot["qty_remaining"] = int(target_lot["qty_remaining"]) - 1
        lot_item_ids = target_lot.get("item_ids", [])
        if lot_item_ids:
            item_id = lot_item_ids.pop(0)
        else:
            item_id = str(uuid.uuid4())
        if target_lot["qty_remaining"] <= 0:
            store["lots"].remove(target_lot)
        await guns_cog._save_state(state)

    pi_ok = await pi_add_item({
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
    })
    if not pi_ok:
        logger.error("gunstore sell: pi_add_item failed — attempting compensation")
        if price > 0:
            await cog.unbelievaboat.update_balance(
                customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — item grant failed"
            )
            if seller_credited:
                await cog.unbelievaboat.update_balance(
                    owner_id, {"cash": -price}, reason="Gun sale refund — item grant failed"
                )
        await ctx.send(
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
    await ctx.send(
        f"Sold **{gun_name}** to **{character_name}** ({customer.display_name}) for **${price:,}**."
    )
    log_ch = await cog._log_channel()
    if log_ch:
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
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _request_fixer_approval(cog, interaction, ctx, customer, gun_name, lot, price, character_name):
    channel = cog.bot.get_channel(GUN_APPROVALS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await cog.bot.fetch_channel(GUN_APPROVALS_CHANNEL_ID)
        except Exception:
            await interaction.followup.send(
                "Gun approvals channel not found. Cannot process restricted sales.", ephemeral=True
            )
            return False

    fixer_role_id = getattr(config, "FIXER_ROLE_ID", 0)
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
    embed.set_footer(text="React ✅ to approve or ❌ to deny. Expires in 5 minutes.")

    try:
        msg = await channel.send(
            f"{fixer_ping} — restricted sale requires approval.",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except Exception:
        logger.exception("Failed to post restricted sale approval request")
        await interaction.followup.send(
            "Failed to post approval request to the approvals channel.", ephemeral=True
        )
        return False

    await interaction.followup.send(
        "⏳ Restricted sale pending Fixer approval. Waiting up to 5 minutes...", ephemeral=True
    )

    fixer_role_id_int = int(fixer_role_id) if fixer_role_id else 0

    def check(reaction, user):
        if reaction.message.id != msg.id:
            return False
        if str(reaction.emoji) not in ("✅", "❌"):
            return False
        if user.bot:
            return False
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            if fixer_role_id_int and any(r.id == fixer_role_id_int for r in user.roles):
                return True
        return False

    try:
        reaction, approver = await cog.bot.wait_for("reaction_add", timeout=300.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(
            f"{ctx.author.mention} — restricted sale of **{gun_name}** timed out. No Fixer responded within 5 minutes."
        )
        try:
            await msg.edit(embed=embed.set_footer(text="EXPIRED — no response."))
        except Exception:
            pass
        return False

    if str(reaction.emoji) == "✅":
        await ctx.send(
            f"✅ Restricted sale of **{gun_name}** approved by {approver.mention}. Processing..."
        )
        try:
            await msg.edit(embed=embed.set_footer(text=f"APPROVED by {approver.display_name}"))
        except Exception:
            pass
        return True
    else:
        await ctx.send(
            f"❌ Restricted sale of **{gun_name}** denied by {approver.mention}."
        )
        try:
            await msg.edit(embed=embed.set_footer(text=f"DENIED by {approver.display_name}"))
        except Exception:
            pass
        return False


class InlineApproveView(SafeView):
    def __init__(self, cog, ctx, guns_cog, store_id, customer):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.guns_cog = guns_cog
        self.store_id = store_id
        self.customer = customer
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
                if self.customer.id not in approved_list:
                    approved_list.append(self.customer.id)
                await self.guns_cog._save_state(state)
        self.approved = True
        await interaction.response.edit_message(
            content=f"✅ {self.customer.display_name} approved. Proceeding with sale...",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.approved = False
        await interaction.response.edit_message(content="Sale cancelled.", view=None)
        self.stop()


class _ApproveBuyerView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, approve: bool = True):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.approve = approve

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a buyer…", row=0)
    async def buyer_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a member.", ephemeral=True)
            return

        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in server.", ephemeral=True)
            return

        if isinstance(raw_user, discord.Member):
            user = raw_user
        else:
            user = guild.get_member(raw_user.id)
            if user is None:
                try:
                    user = await guild.fetch_member(raw_user.id)
                except Exception:
                    await interaction.response.send_message("Could not find that member.", ephemeral=True)
                    return

        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.response.send_message("Gun shop system unavailable.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            actor = self.ctx.author
            if isinstance(actor, discord.Member) and _is_employee_member(actor) and not _is_store_owner_member(actor):
                store_id, store = _find_employee_store(state, guild.id, actor.id)
            else:
                store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
                store = state.get("stores", {}).get(store_id)
            if not store:
                await interaction.followup.send("No store found. Buy stock first.", ephemeral=True)
                self.stop()
                return
            approved = store.setdefault("controlled_buyers", [])
            if self.approve:
                if user.id in approved:
                    await interaction.followup.send(
                        f"{user.display_name} is already approved.", ephemeral=True
                    )
                    self.stop()
                    return
                approved.append(user.id)
            else:
                if user.id not in approved:
                    await interaction.followup.send(
                        f"{user.display_name} is not on your list.", ephemeral=True
                    )
                    self.stop()
                    return
                approved.remove(user.id)
            await guns_cog._save_state(state)

        action = "added to" if self.approve else "removed from"
        await interaction.followup.send(
            f"{user.display_name} {action} your controlled-buyer list.", ephemeral=True
        )
        self.stop()


class _ManageEmployeesView(SafeView):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add Employee", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _EmployeePickerView(cog, ctx, add=True)
        await interaction.response.send_message(
            "➕ **Select a member to add as employee:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Remove Employee", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _EmployeePickerView(cog, ctx, add=False)
        await interaction.response.send_message(
            "➖ **Select an employee to remove:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="View Employees", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(self.ctx.guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(store_id)
        employees = store.get("employees", []) if store else []
        if not employees:
            await interaction.followup.send("No employees assigned to your store.", ephemeral=True)
            return
        lines = [f"<@{uid}>" for uid in employees]
        store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Gun Store"
        await interaction.followup.send(
            f"**{store_name} — Employees ({len(employees)}):**\n" + "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.add = add

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member…", row=0)
    async def employee_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a member.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in server.", ephemeral=True)
            return
        if isinstance(raw_user, discord.Member):
            user = raw_user
        else:
            user = guild.get_member(raw_user.id)
            if user is None:
                try:
                    user = await guild.fetch_member(raw_user.id)
                except Exception:
                    await interaction.response.send_message("Could not find that member.", ephemeral=True)
                    return
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.response.send_message("Gun shop system unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self.add:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).get(store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": []})
            employees = store.get("employees", [])
            if user.id in employees:
                await interaction.followup.send(
                    f"{user.display_name} is already an employee.", ephemeral=True
                )
                self.stop()
                return
            if len(employees) >= 25:
                await interaction.followup.send(
                    "❌ Employee limit reached (25 max). Remove an employee before adding a new one.",
                    ephemeral=True,
                )
                self.stop()
                return
            store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Gun Store"
            dm_view = _GunEmployeeDMConfirmView(user.id, timeout=60)
            try:
                dm_msg = await user.send(
                    f"📋 **{self.ctx.author.display_name}** wants to hire you as an employee at **{store_name}**.\n"
                    "Do you accept?",
                    view=dm_view,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Could not DM {user.display_name}. They may have DMs disabled.", ephemeral=True
                )
                self.stop()
                return
            await interaction.followup.send(
                f"📨 Sent a DM to **{user.display_name}** — waiting for their response…", ephemeral=True
            )
            timed_out = await dm_view.wait()
            if timed_out or not dm_view.accepted:
                reason = "timed out" if timed_out else "declined"
                await interaction.followup.send(
                    f"❌ **{user.display_name}** {reason} the employee offer.", ephemeral=True
                )
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
                    store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": []}
                )
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
            await interaction.followup.send(
                f"✅ **{user.display_name}** accepted and has been added as employee at **{store_name}**.",
                ephemeral=True,
            )
        else:
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
                store = state.setdefault("stores", {}).setdefault(
                    store_id, {"owner_id": self.ctx.author.id, "lots": [], "controlled_buyers": []}
                )
                employees = store.setdefault("employees", [])
                if user.id not in employees:
                    await interaction.followup.send(
                        f"{user.display_name} is not an employee.", ephemeral=True
                    )
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
            await interaction.followup.send(
                f"{user.display_name} removed as employee from **{store_name}**.", ephemeral=True
            )
        self.stop()


class _StorePickerForAction(SafeView):
    def __init__(self, cog, ctx, stores: list, action: str = "sell"):
        super().__init__(timeout=60)
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
            await interaction.response.send_message("Store not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self.action == "sell":
            guns_cog = self.cog._guns_cog()
            await _open_sell_for_store(self.cog, interaction, guns_cog, store_id, store)
        elif self.action == "view_inventory":
            await _show_gun_inventory(interaction, store, store_id)
        self.stop()


class _ManageGunStoreView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Transfer Ownership", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def transfer_ownership(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _GunTransferOwnerView(cog, ctx)
        await interaction.response.send_message(
            "🔄 **Select the new owner for your gun store:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Close Store", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def close_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GunstoreHub")
        ctx = PanelContext(interaction)
        view = _GunCloseConfirmView(cog, ctx)
        await interaction.response.send_message(
            "⚠️ **Are you sure you want to close your gun store?**\n"
            "This will:\n"
            "• Remove all employees\n"
            "• Delete the store name\n"
            "• Return a random 20% of inventory to wholesale at 75% of original price\n"
            "• Delete the remaining inventory\n\n"
            "**This action cannot be undone.**",
            view=view,
            ephemeral=True,
        )


class _GunTransferOwnerView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the new owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a member.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in server.", ephemeral=True)
            return
        new_owner = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if new_owner is None:
            try:
                new_owner = await guild.fetch_member(raw_user.id)
            except Exception:
                await interaction.response.send_message("Could not find that member.", ephemeral=True)
                return
        if new_owner.id == self.ctx.author.id:
            await interaction.response.send_message("You already own this store.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            old_store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).pop(old_store_id, None)
            if not store:
                await interaction.followup.send("You don't have a store to transfer.", ephemeral=True)
                return
            new_store_id = guns_cog._store_id(guild.id, new_owner.id)
            store["owner_id"] = new_owner.id
            state.setdefault("stores", {})[new_store_id] = store
            await guns_cog._save_state(state)
        store_name = store.get("store_name") or "Gun Store"
        await interaction.followup.send(
            f"✅ **{store_name}** has been transferred to {new_owner.display_name}.",
            ephemeral=True,
        )
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
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        guild = self.ctx.guild
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).pop(store_id, None)
            if not store:
                await interaction.followup.send("No store found to close.", ephemeral=True)
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

        summary = f"✅ **{store_name}** has been closed.\n"
        summary += f"• {len(store.get('employees', []))} employee(s) disassociated\n"
        summary += f"• {len(lots)} lot(s) in store\n"
        if returned_lots:
            returned_names = [f"**{l['gun_name']}** ×{l['qty_available']} @ ${l['unit_cost']:,}" for l in returned_lots]
            summary += f"• {len(returned_lots)} lot(s) returned to wholesale:\n  " + "\n  ".join(returned_names)
        else:
            summary += "• No items returned to wholesale"
        await interaction.followup.send(summary, ephemeral=True)

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
                "**Buy** — Purchase guns from wholesale *(owners only)*\n"
                "**Sell** — Sell a gun to a customer (DM confirmation)\n"
                "**Inventory** — View your store stock\n"
                "**Approve/Unapprove** — Manage controlled-buyer list\n"
                "**Wholesale List** — Browse available wholesale stock\n"
                "**Approved Buyers** — See your approved buyer list\n"
                "**Set Store Name** — Give your store a custom name *(owners only)*\n"
                "**Manage Employees** — Add/remove store employees *(owners only)*\n"
                "**Manage Store** — Transfer ownership or close store *(owners only)*"
            ),
            color=discord.Color.dark_gold(),
        )

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
        await channel.send(embed=self._panel_embed(), view=view)
        await ctx.send("✅ Gun Store panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GunstoreHub(bot))
