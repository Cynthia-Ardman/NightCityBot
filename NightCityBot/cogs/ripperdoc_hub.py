"""Unified !ripperdoc hub command — interactive cyberware shop interface.

Consolidates the separate cw_* command set into a single interactive hub
with Discord dropdowns, buttons, and inline component flows.
"""
import logging
import math
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView
from NightCityBot.utils.db import (
    cw_catalog_get_all,
    pi_add_item,
    pi_get_by_owner,
    ih_record_event,
    ih_get_history,
    pt_create,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active
from NightCityBot.utils.inline_helpers import collect_text_input, QtySelectView
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.panel_context import PanelContext

logger = logging.getLogger(__name__)


def _is_ripperdoc_owner(member: discord.Member) -> bool:
    owner_role_id = getattr(config, "RIPPERDOC_OWNER_ROLE_ID", 0)
    return any(r.id == owner_role_id for r in member.roles)


def _is_ripperdoc_employee(member: discord.Member) -> bool:
    emp_role_id = getattr(config, "RIPPERDOC_EMPLOYEE_ROLE_ID", 0)
    return any(r.id == emp_role_id for r in member.roles)


def _rd_store_id(guild_id: int, owner_id: int) -> str:
    return f"rd:{guild_id}:{owner_id}"


def _find_rd_accessible_stores(state: dict, guild_id: int, user_id: int, member) -> list:
    results = []
    is_owner = _is_ripperdoc_owner(member) if member else False
    if is_owner:
        sid = _rd_store_id(guild_id, user_id)
        store = state.get("ripperdoc_stores", {}).get(sid)
        if store:
            results.append((sid, store))
        else:
            results.append((sid, {"owner_id": user_id, "employees": []}))
    prefix = f"rd:{guild_id}:"
    if member and _is_ripperdoc_employee(member):
        for sid, s in state.get("ripperdoc_stores", {}).items():
            if sid.startswith(prefix) and user_id in s.get("employees", []):
                if not any(r[0] == sid for r in results):
                    results.append((sid, s))
    return results


def _get_rd_owner_id(state: dict, store_id: str, fallback_id: int) -> int:
    store = state.get("ripperdoc_stores", {}).get(store_id, {})
    oid = store.get("owner_id", fallback_id)
    if isinstance(oid, str) and oid.isdigit():
        oid = int(oid)
    return oid


async def _show_rd_stock(interaction, cw_cog, store, store_id, owner_id):
    inventory = await cw_cog._load_inventory(owner_id)
    if not inventory:
        await interaction.followup.send("Store cyberware stock is empty.", ephemeral=True)
        return
    groups = cw_cog._grouped_inventory(inventory)
    lines = []
    for i, g in enumerate(groups, 1):
        qty_str = f" ×{g['count']}" if g["count"] > 1 else ""
        sample = g["items"][0] if g.get("items") else {}
        cwp = sample.get("cwp", "")
        slot = sample.get("slot", "")
        detail_parts = []
        if cwp:
            detail_parts.append(f"CWP:{cwp}")
        if slot:
            detail_parts.append(slot)
        detail_tag = f" ({', '.join(detail_parts)})" if detail_parts else ""
        lines.append(f"`{i}.` **{g['name']}**{detail_tag}{qty_str}")
    store_name = store.get("store_name") or f"Store {store_id}"
    embed = discord.Embed(
        title=f"📦 {store_name}",
        description="\n".join(lines[:30]),
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"{len(inventory)} item(s) total")
    await interaction.followup.send(embed=embed, ephemeral=True)


class RipperdocMenuView(SafeView):
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
        if _is_ripperdoc_owner(member) or _is_ripperdoc_employee(member) or member.guild_permissions.administrator:
            return True
        if any(r.id == config.RIPPERDOC_ROLE_ID for r in member.roles):
            return True
        await interaction.response.send_message("This panel is for Ripperdocs only.", ephemeral=True)
        return False

    @discord.ui.button(label="Buy from Wholesale", style=discord.ButtonStyle.primary, emoji="🛒", row=0, custom_id="ripperdoc:buy_wholesale")
    async def buy_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await interaction.response.send_message(
                "Only Ripperdoc Owners can buy from wholesale.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        guild = interaction.guild
        if guild:
            store_id = _rd_store_id(guild.id, interaction.user.id)
            rd_store = state.get("ripperdoc_stores", {}).get(store_id)
            if not rd_store:
                await interaction.followup.send(
                    "❌ You don't have an initialized ripperdoc store. "
                    "Please set up your store first before buying from wholesale.",
                    ephemeral=True,
                )
                return
        lots = state.get("cw_wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await interaction.followup.send("No wholesale stock available this week.", ephemeral=True)
            return
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = WholesaleBuySelect(cog, ctx, available, cw_cog)
        await interaction.followup.send("Select an item to buy:", view=view, ephemeral=True)

    @discord.ui.button(label="Sell to Patient", style=discord.ButtonStyle.success, emoji="💉", row=1, custom_id="ripperdoc:sell_patient")
    async def sell_to_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        state = await cw_cog._load_state()
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        accessible = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if len(accessible) > 1:
            view = _RDStorePickerForAction(cog, ctx, accessible, action="sell", cw_cog=cw_cog)
            await interaction.followup.send(
                "You have access to multiple stores. Select which store to sell from:",
                view=view, ephemeral=True,
            )
            return
        inv_owner_id = interaction.user.id
        if accessible:
            store_id, store_data = accessible[0]
            inv_owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        else:
            store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        inventory = await cw_cog._load_inventory(inv_owner_id)
        if not inventory:
            await interaction.followup.send("Store cyberware stock is empty. Buy from wholesale first.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(cog, ctx, groups, mode="sell", store_id=store_id, inv_owner_id=inv_owner_id)
        msg = "**Step 1** — Select the patient and the item to sell:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await interaction.followup.send(msg, view=view, ephemeral=True)

    @discord.ui.button(label="Install on Patient", style=discord.ButtonStyle.success, emoji="🔧", row=1, custom_id="ripperdoc:install_patient")
    async def install_on_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        state = await cw_cog._load_state()
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        accessible = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if len(accessible) > 1:
            view = _RDStorePickerForAction(cog, ctx, accessible, action="install", cw_cog=cw_cog)
            await interaction.followup.send(
                "You have access to multiple stores. Select which store to install from:",
                view=view, ephemeral=True,
            )
            return
        inv_owner_id = interaction.user.id
        if accessible:
            store_id, store_data = accessible[0]
            inv_owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        else:
            store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        inventory = await cw_cog._load_inventory(inv_owner_id)
        if not inventory:
            await interaction.followup.send("Store cyberware stock is empty. Buy from wholesale first.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(cog, ctx, groups, mode="install", store_id=store_id, inv_owner_id=inv_owner_id)
        msg = "**Step 1** — Select the patient and the item to install:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await interaction.followup.send(msg, view=view, ephemeral=True)

    @discord.ui.button(label="Wholesale List", style=discord.ButtonStyle.secondary, emoji="📋", row=0, custom_id="ripperdoc:wholesale_list")
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        lots = state.get("cw_wholesale_lots", [])
        if not lots:
            await interaction.followup.send("No wholesale stock this week.", ephemeral=True)
            return
        lines = []
        for i, lot in enumerate(cw_cog._sorted_lots(lots), 1):
            qty = int(lot["qty_available"])
            price = int(lot["unit_cost"])
            cwp = lot.get("cwp", "")
            slot = lot.get("slot", "")
            detail_parts = []
            if cwp:
                detail_parts.append(f"CWP:{cwp}")
            if slot:
                detail_parts.append(slot)
            detail_tag = f" ({', '.join(detail_parts)})" if detail_parts else ""
            if qty > 0:
                lines.append(f"`{i}.` **{lot['item_name']}**{detail_tag} — ${price:,} × {qty}")
            else:
                lines.append(f"~~`{i}.` {lot['item_name']}~~ — Sold out")
        embed = discord.Embed(
            title="🔩 Cyberware Wholesale",
            description="\n".join(lines[:30]),
            color=discord.Color.teal(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Manage Store", style=discord.ButtonStyle.danger, emoji="⚙️", row=2, custom_id="ripperdoc:manage_store")
    async def manage_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await interaction.response.send_message(
                "Only Ripperdoc Owners can manage their store.", ephemeral=True
            )
            return
        cw_cog = interaction.client.get_cog("CyberwareShop")
        store_name = None
        if cw_cog:
            state = await cw_cog._load_state()
            store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
            store = state.get("ripperdoc_stores", {}).get(store_id)
            if store:
                store_name = store.get("store_name")
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _ManageRDStoreView(cog, ctx)
        header = "⚙️ **Manage Store"
        if store_name:
            header += f" — {store_name}"
        header += "** — choose an action:"
        await interaction.response.send_message(header, view=view, ephemeral=True)

    @discord.ui.button(label="Manage Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=2, custom_id="ripperdoc:manage_employees")
    async def manage_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await interaction.response.send_message(
                "Only Ripperdoc Owners can manage employees.", ephemeral=True
            )
            return
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDManageEmployeesView(cog, ctx)
        await interaction.response.send_message(
            "👥 **Manage Employees** — choose an action:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Checkup", style=discord.ButtonStyle.primary, emoji="🩺", row=3, custom_id="ripperdoc:checkup")
    async def checkup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            await interaction.followup.send("⚠️ The cyberware system is currently disabled.", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        if role is None:
            await interaction.followup.send("⚠️ Checkup role is not configured.", ephemeral=True)
            return
        view = _CheckupPatientSelectView(interaction.user.id)
        await interaction.followup.send(
            "🩺 **Checkup** — Select the patient to check up on:", view=view, ephemeral=True
        )


class _CheckupPatientSelectView(SafeView):
    def __init__(self, ripperdoc_id: int):
        super().__init__(timeout=60)
        self._ripperdoc_id = ripperdoc_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ripperdoc_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a patient…", row=0)
    async def patient_select(self, interaction: discord.Interaction,
                             select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        patient = select.values[0]
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        member = guild.get_member(patient.id)
        if not member:
            try:
                member = await guild.fetch_member(patient.id)
            except Exception:
                await interaction.followup.send("❌ Could not find that member.", ephemeral=True)
                return

        role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        if role is None:
            await interaction.followup.send("⚠️ Checkup role is not configured.", ephemeral=True)
            return
        if role not in member.roles:
            await interaction.followup.send(
                f"{member.display_name} does not have the checkup role.", ephemeral=True
            )
            return

        try:
            await member.remove_roles(role, reason="Cyberware check-up completed via Ripperdoc Hub")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Could not remove checkup role: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Removed checkup role from {member.display_name}.", ephemeral=True
        )

        log_channel = guild.get_channel(config.RIPPERDOC_LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(
                    f"Ripperdoc {interaction.user.display_name} did a checkup on {member.display_name}"
                )
            except Exception:
                pass

        from NightCityBot.utils.db import cyberware_status_upsert
        try:
            await cyberware_status_upsert(str(member.id), 0, None)
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Role removed but DB update failed: {e}", ephemeral=True
            )
            self.stop()
            return
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog and hasattr(cw_cog, "data"):
            cw_cog.data[str(member.id)] = {"weeks": 0, "last": None}
        self.stop()


class WholesaleBuySelect(SafeView):
    def __init__(self, cog: "RipperdocHub", ctx: commands.Context, lots: list, cw_cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.cw_cog = cw_cog
        options = []
        for i, lot in enumerate(lots[:25]):
            label = f"{lot['item_name']} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(i),
            ))
        self.select = discord.ui.Select(placeholder="Choose an item...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        lot = self.lots[idx]
        max_qty = int(lot.get("qty_available", 1))
        qty_view = QtySelectView(interaction.user.id, max_qty)
        await interaction.response.send_message(
            f"**{lot['item_name']}** — how many? (max {max_qty})",
            view=qty_view,
            ephemeral=True,
        )
        await qty_view.wait()
        if qty_view.result is None:
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)
            return

        qty = qty_view.result
        unit_cost = int(lot["unit_cost"])
        total = unit_cost * qty
        member = self.ctx.author

        balance = await self.cog.unbelievaboat.get_balance(member.id)
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
        ok = await self.cog.unbelievaboat.update_balance(
            member.id,
            {"cash": -cash_deduct, "bank": -bank_deduct},
            reason=f"CW wholesale buy: {lot['item_name']} x{qty}",
        )
        if not ok:
            await interaction.followup.send("Payment failed.", ephemeral=True)
            return

        async with self.cw_cog._locks.pin("state"):
            state = await self.cw_cog._load_state()
            lots = state.get("cw_wholesale_lots", [])
            target_lot = None
            lot_id = lot.get("lot_id")
            if lot_id:
                for l in lots:
                    if l.get("lot_id") == lot_id:
                        target_lot = l
                        break
            if target_lot is None:
                target_lot = self.cw_cog._lookup_lot(lots, lot["item_name"])
            if not target_lot or int(target_lot.get("qty_available", 0)) < qty:
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="CW wholesale buy refund — stock depleted",
                )
                await interaction.followup.send("Stock depleted. Refunded.", ephemeral=True)
                return
            target_lot["qty_available"] = int(target_lot["qty_available"]) - qty
            save_ok = await self.cw_cog._save_state(state)
            if not save_ok:
                target_lot["qty_available"] = int(target_lot["qty_available"]) + qty
                logger.error("cw wholesale buy: _save_state failed — refunding buyer=%s", member.id)
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="CW wholesale refund — save failed",
                )
                await interaction.followup.send(
                    "⚠️ Purchase failed (save error). Payment has been refunded. Please try again.",
                    ephemeral=True,
                )
                return

        async with self.cw_cog._locks.acquire(str(member.id)):
            inventory = await self.cw_cog._load_inventory(member.id)
            for _ in range(qty):
                item_id = str(uuid.uuid4())
                inv_item = {
                    "item_id": item_id,
                    "name": lot["item_name"],
                    "price_paid": unit_cost,
                    "purchased_at": datetime.now(timezone.utc).isoformat(),
                }
                if lot.get("cwp"):
                    inv_item["cwp"] = lot["cwp"]
                if lot.get("slot"):
                    inv_item["slot"] = lot["slot"]
                inventory.append(inv_item)
                await ih_record_event(
                    item_id, "cw_wholesale_buy",
                    actor_id=str(member.id),
                    price=unit_cost,
                    metadata={"item_name": lot["item_name"], "lot_id": lot.get("lot_id")},
                )
            inv_ok = await self.cw_cog._save_inventory(member.id, inventory)
            if not inv_ok:
                logger.error("cw wholesale buy: _save_inventory failed — refunding buyer=%s", member.id)
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="CW wholesale refund — inventory save failed",
                )
                await interaction.followup.send(
                    "⚠️ Purchase failed (inventory save error). Payment has been refunded. Please try again.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            f"Purchased **{lot['item_name']}** ×{qty} for **${total:,}**.",
            ephemeral=True,
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🛒 CW Wholesale Purchase",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Ripperdoc", value=f"{member.mention}", inline=False)
            embed.add_field(name="Item", value=lot["item_name"], inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Total", value=f"${total:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class SellSetupView(SafeView):
    def __init__(self, cog: "RipperdocHub", ctx: commands.Context,
                 groups: list[dict], *, mode: str = "sell",
                 store_id: str = "", inv_owner_id: int = 0):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.groups = groups
        self.mode = mode
        self.store_id = store_id
        self.inv_owner_id = inv_owner_id or ctx.author.id
        self.selected_patient: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None
        self.selected_character: Optional[dict] = None
        self._character_select: Optional[discord.ui.Select] = None

        self.truncated = len(groups) > 25
        options = []
        for i, g in enumerate(groups[:25]):
            qty_str = f" ×{g['count']}" if g["count"] > 1 else ""
            label = f"{g['name']}{qty_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))
        stock_select = discord.ui.Select(
            placeholder="Choose item from your stock…",
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

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a patient…", row=0)
    async def patient_select(self, interaction: discord.Interaction,
                             select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await interaction.response.send_message(
                "Please select a server member.", ephemeral=True
            )
            return
        if isinstance(user, discord.Member):
            self.selected_patient = user
        else:
            guild = self.ctx.guild
            if guild:
                member = guild.get_member(user.id)
                if member:
                    self.selected_patient = member
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
        characters = await get_active_characters(str(self.selected_patient.id))
        if not characters:
            await interaction.response.send_message(
                f"❌ {self.selected_patient.display_name} has no active characters. "
                "They must create a character before receiving items.",
                ephemeral=True,
            )
            self.selected_patient = None
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
            content=f"Patient: **{self.selected_patient.display_name}** ✓ — Now select their character.",
            view=self,
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
        self.selected_group_idx = int(interaction.data["values"][0])
        item_name = self.groups[self.selected_group_idx]["name"]
        await interaction.response.send_message(
            f"Item: **{item_name}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if self.selected_patient is None:
            await interaction.response.send_message(
                "Please select a patient first.", ephemeral=True
            )
            return
        if self.selected_character is None:
            await interaction.response.send_message(
                "Please select a character for the patient.", ephemeral=True
            )
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message(
                "Please select an item from your stock first.", ephemeral=True
            )
            return
        if not await ensure_character_active(self.selected_character["character_id"]):
            await interaction.response.send_message(
                f"❌ Character **{self.selected_character['name']}** is no longer active.",
                ephemeral=True,
            )
            return
        group = self.groups[self.selected_group_idx]
        label = "install fee" if self.mode == "install" else "price to charge"
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            f"📝 **Enter the {label}** (number only, `0` for free), or type `cancel`:",
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
        if self.mode == "install":
            await _process_cw_install(
                self.cog, interaction, self.ctx, self.selected_patient,
                group, self.selected_character or {}, price,
                store_id=self.store_id, inv_owner_id=self.inv_owner_id,
            )
        else:
            await _process_cw_sell(
                self.cog, interaction, self.ctx, self.selected_patient,
                group, self.selected_character or {}, price,
                store_id=self.store_id, inv_owner_id=self.inv_owner_id,
            )
        self.stop()


async def _process_cw_sell(cog, interaction, ctx, patient, group, character, price,
                          *, store_id: str = "", inv_owner_id: int = 0):
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

    item_name = group["name"]
    selected = group["items"][0]
    item_id = selected.get("item_id", str(uuid.uuid4()))

    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
        return

    owner_id = inv_owner_id or ctx.author.id
    if store_id:
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, owner_id)

    confirm_view = DMConfirmView(recipient_id=patient.id, timeout=60)
    try:
        dm_msg = await patient.send(
            f"**{ctx.author.display_name}** wants to sell you **{item_name}** "
            f"for **${price:,}** (character: **{character_name}**).\n"
            "Do you accept?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            f"Cannot DM {patient.display_name}. They may have DMs disabled.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Confirmation sent to {patient.display_name} via DM. Waiting...", ephemeral=True
    )
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Trade declined or timed out.", view=None)
        except Exception:
            pass
        await ctx.send(
            f"{ctx.author.mention} — {patient.display_name} declined or didn't respond to the sale of **{item_name}**."
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
        balance = await cog.unbelievaboat.get_balance(patient.id)
        if balance is None:
            await ctx.send(f"Could not fetch {patient.display_name}'s balance. Sale cancelled.")
            return
        p_cash = int(balance.get("cash", 0))
        p_bank = int(balance.get("bank", 0))
        if p_cash + p_bank < price:
            await ctx.send(
                f"{patient.display_name} cannot afford ${price:,} (has ${p_cash + p_bank:,}). Sale cancelled."
            )
            return
        cash_ded = min(max(p_cash, 0), price)
        bank_ded = max(0, price - cash_ded)
        ok_debit = await cog.unbelievaboat.update_balance(
            patient.id,
            {"cash": -cash_ded, "bank": -bank_ded},
            reason=f"CW purchase: {item_name} from {ctx.author.display_name}",
        )
        if not ok_debit:
            await ctx.send(f"Payment failed for {patient.display_name}. Sale cancelled.")
            return
        ok_credit = await cog.unbelievaboat.update_balance(
            owner_id,
            {"cash": price},
            reason=f"CW sale: {item_name} to {patient.display_name}",
        )
        if ok_credit:
            seller_credited = True
        else:
            logger.error("cw sell: buyer debited but seller credit failed — creating pending transfer")
            await pt_create({
                "seller_id": str(owner_id),
                "buyer_id": str(patient.id),
                "item_id": item_id,
                "amount": price,
                "reason": f"CW sale credit failed: {item_name}",
            })
            await ctx.send(
                f"⚠️ Payment from {patient.display_name} succeeded but seller payout failed. "
                "A pending transfer has been created — an admin will resolve it."
            )

    async with cw_cog._locks.acquire(str(owner_id)):
        inv = await cw_cog._load_inventory(owner_id)
        inv_updated = [it for it in inv if it.get("item_id") != item_id]
        if len(inv_updated) == len(inv):
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW sale refund — item missing"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="CW sale refund — item missing"
                    )
            await ctx.send("Item no longer in stock. Refunded.")
            return
        inv_save_ok = await cw_cog._save_inventory(owner_id, inv_updated)
        if not inv_save_ok:
            logger.error("ripperdoc sell: _save_inventory failed — refunding patient=%s", patient.id)
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW sale refund — save failed"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="CW sale refund — save failed"
                    )
            await ctx.send("⚠️ Sale failed (save error). Payment has been refunded.")
            return

    pi_payload = {
        "item_id": item_id,
        "owner_id": str(patient.id),
        "character_name": character_name,
        "character_id": character_id,
        "item_type": "cyberware",
        "name": item_name,
        "restriction": "basic",
        "description": "",
        "price_paid": price,
        "seller_id": str(ctx.author.id),
        "seller_name": ctx.author.display_name,
    }
    if selected.get("cwp"):
        pi_payload["cwp"] = selected["cwp"]
    if selected.get("slot"):
        pi_payload["slot"] = selected["slot"]
    pi_ok = await pi_add_item(pi_payload)
    if not pi_ok:
        logger.error("ripperdoc sell: pi_add_item failed — attempting compensation")
        async with cw_cog._locks.acquire(str(owner_id)):
            inv_restore = await cw_cog._load_inventory(owner_id)
            inv_restore.append({
                "item_id": item_id,
                "name": item_name,
                "price_paid": price,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            })
            await cw_cog._save_inventory(owner_id, inv_restore)
            logger.info("ripperdoc sell: restored item_id=%s to ripperdoc=%s stock", item_id, owner_id)
        if price > 0:
            await cog.unbelievaboat.update_balance(
                patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW sale refund — item grant failed"
            )
            if seller_credited:
                await cog.unbelievaboat.update_balance(
                    owner_id, {"bank": -price}, reason="CW sale refund — item grant failed"
                )
        await ctx.send(
            f"⚠️ Failed to add **{item_name}** to {patient.display_name}'s inventory. "
            "Payment has been refunded and item has been restored to stock. Please contact an admin."
        )
        return

    await ih_record_event(
        item_id, "cw_sold",
        actor_id=str(ctx.author.id),
        target_id=str(patient.id),
        price=price,
        metadata={"item_name": item_name, "character": character_name},
    )

    await interaction.followup.send(
        f"Sold **{item_name}** to **{character_name}** ({patient.display_name}) "
        f"for **${price:,}**.",
        ephemeral=True,
    )
    log_ch = await cog._log_channel()
    if log_ch:
        confirm_text = (
            f"Sold **{item_name}** to **{character_name}** ({patient.display_name}) "
            f"for **${price:,}**."
        )
        embed = discord.Embed(
            title="💉 Cyberware Sold",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )
        if owner_id != ctx.author.id:
            embed.add_field(name="Store Owner", value=f"<@{owner_id}>", inline=False)
            embed.add_field(name="Sold By", value=f"{ctx.author.mention}", inline=False)
        else:
            embed.add_field(name="Ripperdoc", value=f"{ctx.author.mention}", inline=False)
        embed.add_field(name="Patient", value=f"{patient.mention} — {character_name}", inline=False)
        embed.add_field(name="Item", value=item_name, inline=True)
        embed.add_field(name="Price", value=f"${price:,}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(content=confirm_text, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _process_cw_install(cog, interaction, ctx, patient, group, character, price,
                              *, store_id: str = "", inv_owner_id: int = 0):
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

    item_name = group["name"]
    selected = group["items"][0]
    item_id = selected.get("item_id", str(uuid.uuid4()))

    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
        return

    owner_id = inv_owner_id or ctx.author.id
    if store_id:
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, owner_id)

    price_text = f" for **${price:,}**" if price > 0 else " (free install)"
    confirm_view = DMConfirmView(recipient_id=patient.id, timeout=60)
    try:
        dm_msg = await patient.send(
            f"**{ctx.author.display_name}** wants to install **{item_name}** on "
            f"**{character_name}**{price_text}.\n"
            "This will consume the cyberware. Do you accept?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            f"Cannot DM {patient.display_name}. They may have DMs disabled.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Confirmation sent to {patient.display_name} via DM. Waiting...", ephemeral=True
    )
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Installation declined or timed out.", view=None)
        except Exception:
            pass
        await ctx.send(
            f"{ctx.author.mention} — {patient.display_name} declined or didn't respond to the install of **{item_name}**."
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
        balance = await cog.unbelievaboat.get_balance(patient.id)
        if balance is None:
            await ctx.send(f"Could not fetch {patient.display_name}'s balance. Install cancelled.")
            return
        p_cash = int(balance.get("cash", 0))
        p_bank = int(balance.get("bank", 0))
        if p_cash + p_bank < price:
            await ctx.send(
                f"{patient.display_name} cannot afford ${price:,} (has ${p_cash + p_bank:,}). Install cancelled."
            )
            return
        cash_ded = min(max(p_cash, 0), price)
        bank_ded = max(0, price - cash_ded)
        ok_debit = await cog.unbelievaboat.update_balance(
            patient.id,
            {"cash": -cash_ded, "bank": -bank_ded},
            reason=f"CW install: {item_name} by {ctx.author.display_name}",
        )
        if not ok_debit:
            await ctx.send(f"Payment failed for {patient.display_name}. Install cancelled.")
            return
        ok_credit = await cog.unbelievaboat.update_balance(
            owner_id,
            {"cash": price},
            reason=f"CW install fee: {item_name} for {patient.display_name}",
        )
        if ok_credit:
            seller_credited = True
        else:
            logger.error("cw install: patient debited but ripperdoc credit failed — creating pending transfer")
            await pt_create({
                "seller_id": str(owner_id),
                "buyer_id": str(patient.id),
                "item_id": item_id,
                "amount": price,
                "reason": f"CW install credit failed: {item_name}",
            })
            await ctx.send(
                f"⚠️ Payment from {patient.display_name} succeeded but ripperdoc payout failed. "
                "A pending transfer has been created — an admin will resolve it."
            )

    async with cw_cog._locks.acquire(str(owner_id)):
        inv = await cw_cog._load_inventory(owner_id)
        inv_updated = [it for it in inv if it.get("item_id") != item_id]
        if len(inv_updated) == len(inv):
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW install refund — item missing"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="CW install refund — item missing"
                    )
            await ctx.send("Item no longer in stock. Refunded.")
            return
        inv_save_ok = await cw_cog._save_inventory(owner_id, inv_updated)
        if not inv_save_ok:
            logger.error("cw install: _save_inventory failed — refunding patient=%s", patient.id)
            if price > 0:
                await cog.unbelievaboat.update_balance(
                    patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW install refund — save failed"
                )
                if seller_credited:
                    await cog.unbelievaboat.update_balance(
                        owner_id, {"bank": -price}, reason="CW install refund — save failed"
                    )
            await ctx.send("⚠️ Install failed (save error). Payment has been refunded.")
            return

    await ih_record_event(
        item_id, "cw_installed",
        actor_id=str(ctx.author.id),
        target_id=str(patient.id),
        price=price if price > 0 else None,
        metadata={"item_name": item_name, "character": character_name},
    )

    await interaction.followup.send(
        f"Installed **{item_name}** on **{character_name}** ({patient.display_name}).",
        ephemeral=True,
    )
    log_ch = await cog._log_channel()
    if log_ch:
        confirm_text = (
            f"💉 **{item_name}** installed on **{character_name}** ({patient.display_name}) "
            f"by {ctx.author.display_name}."
        )
        embed = discord.Embed(
            title="💉 Cyberware Installed",
            color=discord.Color.teal(),
        )
        if owner_id != ctx.author.id:
            embed.add_field(name="Store Owner", value=f"<@{owner_id}>", inline=False)
            embed.add_field(name="Installed By", value=f"{ctx.author.mention}", inline=False)
        else:
            embed.add_field(name="Ripperdoc", value=f"{ctx.author.mention}", inline=False)
        embed.add_field(name="Patient", value=f"{patient.mention} — {character_name}", inline=False)
        embed.add_field(name="Item", value=item_name, inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(content=confirm_text, embed=embed, allowed_mentions=discord.AllowedMentions.none())


class _RDStorePickerForAction(SafeView):
    def __init__(self, cog, ctx, stores: list, *, action: str = "sell", cw_cog=None):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.stores = {sid: s for sid, s in stores}
        self.action = action
        self.cw_cog = cw_cog
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
        cw_cog = self.cw_cog or interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, self.ctx.author.id)
        if self.action == "view_stock":
            await _show_rd_stock(interaction, cw_cog, store, store_id, owner_id)
            self.stop()
            return
        inventory = await cw_cog._load_inventory(owner_id)
        if not inventory:
            await interaction.followup.send("Store cyberware stock is empty.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        mode = "install" if self.action == "install" else "sell"
        view = SellSetupView(self.cog, self.ctx, groups, mode=mode,
                             store_id=store_id, inv_owner_id=owner_id)
        label = "install" if mode == "install" else "sell"
        msg = f"**Step 1** — Select the patient and the item to {label}:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await interaction.followup.send(msg, view=view, ephemeral=True)
        self.stop()


class _RDManageEmployeesView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add Employee", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDAddEmployeeView(cog, ctx)
        await interaction.response.send_message(
            "➕ **Select the member to add as employee:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Remove Employee", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id, {})
        employees = store.get("employees", [])
        if not employees:
            await interaction.followup.send("No employees to remove.", ephemeral=True)
            return
        options = []
        for eid in employees[:25]:
            options.append(discord.SelectOption(label=str(eid), value=str(eid)))
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDRemoveEmployeeView(cog, ctx, options)
        await interaction.followup.send(
            "➖ **Select the employee to remove:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="View Employees", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id, {})
        employees = store.get("employees", [])
        if not employees:
            await interaction.followup.send("No employees registered.", ephemeral=True)
            return
        lines = [f"<@{eid}>" for eid in employees]
        await interaction.followup.send(
            f"👥 **Employees** ({len(employees)}):\n" + "\n".join(lines),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )


class _EmployeeDMConfirmView(SafeView):
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


class _RDAddEmployeeView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member…", row=0)
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a member.", ephemeral=True)
            return
        guild = self.ctx.guild
        new_emp = raw_user if isinstance(raw_user, discord.Member) else (guild.get_member(raw_user.id) if guild else None)
        if new_emp is None:
            try:
                new_emp = await guild.fetch_member(raw_user.id)
            except Exception:
                await interaction.response.send_message("Could not find that member.", ephemeral=True)
                return
        if new_emp.id == self.ctx.author.id:
            await interaction.response.send_message("You can't add yourself as an employee.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(guild.id, self.ctx.author.id)
        stores = state.get("ripperdoc_stores", {})
        store = stores.get(store_id, {"owner_id": self.ctx.author.id, "employees": []})
        employees = store.get("employees", [])
        if new_emp.id in employees:
            await interaction.followup.send(f"{new_emp.display_name} is already an employee.", ephemeral=True)
            self.stop()
            return
        store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Ripperdoc"
        dm_view = _EmployeeDMConfirmView(new_emp.id, timeout=60)
        try:
            dm_msg = await new_emp.send(
                f"📋 **{self.ctx.author.display_name}** wants to hire you as an employee at **{store_name}**.\n"
                "Do you accept?",
                view=dm_view,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Could not DM {new_emp.display_name}. They may have DMs disabled.", ephemeral=True
            )
            self.stop()
            return
        await interaction.followup.send(
            f"📨 Sent a DM to **{new_emp.display_name}** — waiting for their response…", ephemeral=True
        )
        timed_out = await dm_view.wait()
        if timed_out or not dm_view.accepted:
            reason = "timed out" if timed_out else "declined"
            await interaction.followup.send(
                f"❌ **{new_emp.display_name}** {reason} the employee offer.", ephemeral=True
            )
            if timed_out:
                try:
                    await dm_msg.edit(content="⏰ Employee offer expired.", view=None)
                except Exception:
                    pass
            self.stop()
            return
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            stores = state.setdefault("ripperdoc_stores", {})
            store = stores.setdefault(store_id, {"owner_id": self.ctx.author.id, "employees": []})
            employees = store.setdefault("employees", [])
            if new_emp.id not in employees:
                employees.append(new_emp.id)
                await cw_cog._save_state(state)
        emp_role = guild.get_role(config.RIPPERDOC_EMPLOYEE_ROLE_ID) if guild else None
        if emp_role and emp_role not in new_emp.roles:
            try:
                await new_emp.add_roles(emp_role, reason=f"Hired as ripperdoc employee at {store_name}")
            except discord.Forbidden:
                pass
        await interaction.followup.send(
            f"✅ **{new_emp.display_name}** accepted and has been added as employee at **{store_name}**.",
            ephemeral=True,
        )
        self.stop()


class _RDRemoveEmployeeView(SafeView):
    def __init__(self, cog, ctx, options):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        select = discord.ui.Select(placeholder="Select employee to remove…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        emp_id = int(interaction.data["values"][0])
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        still_employed_elsewhere = False
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            store_id = _rd_store_id(interaction.guild.id, self.ctx.author.id)
            store = state.get("ripperdoc_stores", {}).get(store_id, {})
            employees = store.get("employees", [])
            if emp_id in employees:
                employees.remove(emp_id)
                await cw_cog._save_state(state)
                prefix = f"rd:{interaction.guild.id}:"
                for sid, s in state.get("ripperdoc_stores", {}).items():
                    if sid.startswith(prefix) and emp_id in s.get("employees", []):
                        still_employed_elsewhere = True
                        break
                await interaction.followup.send(f"✅ Employee <@{emp_id}> removed.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.followup.send("Employee not found.", ephemeral=True)
                self.stop()
                return
        if not still_employed_elsewhere and interaction.guild:
            emp_role = interaction.guild.get_role(config.RIPPERDOC_EMPLOYEE_ROLE_ID)
            if emp_role:
                member = interaction.guild.get_member(emp_id)
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(emp_id)
                    except Exception:
                        member = None
                if member and emp_role in member.roles:
                    try:
                        await member.remove_roles(emp_role, reason="Removed as ripperdoc employee")
                    except discord.Forbidden:
                        pass
        self.stop()


class _ManageRDStoreView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Store", style=discord.ButtonStyle.success, emoji="🏪", row=0)
    async def create_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        existing = state.get("ripperdoc_stores", {}).get(store_id)
        if existing and existing.get("store_name"):
            await interaction.followup.send(
                f"You already own a ripperdoc store: **{existing['store_name']}**.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "🏪 **Enter a name for your new store** (e.g. `Chrome Cathedral`), or type `cancel`:",
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
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            stores = state.setdefault("ripperdoc_stores", {})
            store = stores.setdefault(store_id, {"owner_id": interaction.user.id, "employees": []})
            store["store_name"] = name
            await cw_cog._save_state(state)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and interaction.guild:
            owner_role = interaction.guild.get_role(config.RIPPERDOC_OWNER_ROLE_ID)
            if owner_role and owner_role not in member.roles:
                try:
                    await member.add_roles(owner_role, reason="Created ripperdoc store")
                except discord.Forbidden:
                    pass
        await interaction.followup.send(f"✅ Store **{name}** created!", ephemeral=True)

    @discord.ui.button(label="Change Store Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def change_store_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id)
        current_name = store.get("store_name") if store else None
        prompt = "✏️ "
        if current_name:
            prompt += f"Current name: **{current_name}**\n"
        prompt += "**Enter a new store name**, or type `cancel`:"
        await interaction.followup.send(prompt, ephemeral=True)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        name = text.strip()[:100]
        if not name:
            await interaction.followup.send("Name cannot be empty.", ephemeral=True)
            return
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            stores = state.setdefault("ripperdoc_stores", {})
            store = stores.setdefault(store_id, {"owner_id": interaction.user.id, "employees": []})
            store["store_name"] = name
            await cw_cog._save_state(state)
        await interaction.followup.send(f"Store name changed to **{name}**.", ephemeral=True)

    @discord.ui.button(label="My Stock", style=discord.ButtonStyle.secondary, emoji="📦", row=0)
    async def view_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        stores = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not stores:
            await interaction.followup.send("You are not assigned to any store.", ephemeral=True)
            return
        if len(stores) > 1:
            cog = interaction.client.get_cog("RipperdocHub")
            ctx = PanelContext(interaction)
            view = _RDStorePickerForAction(cog, ctx, stores, action="view_stock", cw_cog=cw_cog)
            await interaction.followup.send(
                "📦 **Select which store stock to view:**", view=view, ephemeral=True
            )
            return
        store_id, store = stores[0]
        owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        await _show_rd_stock(interaction, cw_cog, store, store_id, owner_id)

    @discord.ui.button(label="Transfer Ownership", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def transfer_ownership(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDTransferOwnerView(cog, ctx)
        await interaction.response.send_message(
            "🔄 **Select the new owner for your ripperdoc store:**", view=view, ephemeral=True
        )

    @discord.ui.button(label="Close Store", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def close_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDCloseConfirmView(cog, ctx)
        await interaction.response.send_message(
            "⚠️ **Are you sure you want to close your ripperdoc store?**\n"
            "This will:\n"
            "• Remove all employees\n"
            "• Delete the store name\n"
            "• Return a random 20% of cyberware inventory to wholesale at 75% of original price\n"
            "• Delete the remaining inventory\n\n"
            "**This action cannot be undone.**",
            view=view,
            ephemeral=True,
        )


class _RDTransferDMConfirmView(SafeView):
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


class _RDTransferOwnerView(SafeView):
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
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        new_store_id = _rd_store_id(guild.id, new_owner.id)
        existing = state.get("ripperdoc_stores", {}).get(new_store_id)
        if existing and existing.get("store_name"):
            await interaction.followup.send(
                f"{new_owner.display_name} already owns a ripperdoc store.", ephemeral=True
            )
            return
        old_store_id = _rd_store_id(guild.id, self.ctx.author.id)
        store = state.get("ripperdoc_stores", {}).get(old_store_id)
        if not store:
            await interaction.followup.send("You don't have a store to transfer.", ephemeral=True)
            return
        store_name = store.get("store_name") or "Ripperdoc Store"
        confirm_view = _RDTransferDMConfirmView(new_owner.id, timeout=120)
        try:
            dm = await new_owner.send(
                f"🔄 **{self.ctx.author.display_name}** wants to transfer **{store_name}** to you.\n"
                "Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                f"Could not DM {new_owner.display_name}. They may have DMs disabled.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"📩 Sent a DM to {new_owner.display_name} for confirmation. Waiting…", ephemeral=True
        )
        await confirm_view.wait()
        if not confirm_view.accepted:
            reason = "declined" if confirm_view.accepted is False else "timed out"
            await interaction.followup.send(
                f"❌ Transfer {reason} by {new_owner.display_name}.", ephemeral=True
            )
            return
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            old_store_id = _rd_store_id(guild.id, self.ctx.author.id)
            store = state.get("ripperdoc_stores", {}).pop(old_store_id, None)
            if not store:
                await interaction.followup.send("You don't have a store to transfer.", ephemeral=True)
                return
            new_store_id = _rd_store_id(guild.id, new_owner.id)
            store["owner_id"] = new_owner.id
            state.setdefault("ripperdoc_stores", {})[new_store_id] = store
            old_inv = await cw_cog._load_inventory(self.ctx.author.id)
            if old_inv:
                new_inv = await cw_cog._load_inventory(new_owner.id)
                new_inv.extend(old_inv)
                await cw_cog._save_inventory(new_owner.id, new_inv)
                await cw_cog._save_inventory(self.ctx.author.id, [])
            await cw_cog._save_state(state)
        store_name = store.get("store_name") or "Ripperdoc Store"
        owner_role = guild.get_role(config.RIPPERDOC_OWNER_ROLE_ID) if hasattr(config, "RIPPERDOC_OWNER_ROLE_ID") else None
        if owner_role:
            old_owner_member = guild.get_member(self.ctx.author.id)
            if old_owner_member:
                try:
                    await old_owner_member.remove_roles(owner_role, reason=f"Ripperdoc store transferred to {new_owner.display_name}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            try:
                await new_owner.add_roles(owner_role, reason=f"Ripperdoc store transferred from {self.ctx.author.display_name}")
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(
            f"✅ **{store_name}** has been transferred to {new_owner.display_name}.",
            ephemeral=True,
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔄 Ripperdoc Store Transferred",
                color=discord.Color.dark_gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store", value=store_name, inline=False)
            embed.add_field(name="From", value=f"{self.ctx.author.mention}", inline=True)
            embed.add_field(name="To", value=f"{new_owner.mention}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


class _RDCloseConfirmView(SafeView):
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
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        guild = self.ctx.guild
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            store_id = _rd_store_id(guild.id, self.ctx.author.id)
            store = state.get("ripperdoc_stores", {}).pop(store_id, None)
            if not store:
                await interaction.followup.send("No store found to close.", ephemeral=True)
                return
            store_name = store.get("store_name") or "Ripperdoc Store"
            inventory = await cw_cog._load_inventory(self.ctx.author.id)
            returned_items = []
            if inventory:
                return_count = max(1, math.ceil(len(inventory) * 0.2))
                to_return = random.sample(inventory, min(return_count, len(inventory)))
                wh_lots = state.setdefault("cw_wholesale_lots", [])
                for item in to_return:
                    new_lot = {
                        "lot_id": str(uuid.uuid4()),
                        "item_name": item.get("name", "Unknown"),
                        "unit_cost": int(int(item.get("price_paid", 0)) * 0.75),
                        "qty_available": 1,
                    }
                    wh_lots.append(new_lot)
                    returned_items.append(new_lot)
                await cw_cog._save_inventory(self.ctx.author.id, [])
            await cw_cog._save_state(state)

        employees = store.get("employees", [])
        owner_role = guild.get_role(config.RIPPERDOC_OWNER_ROLE_ID) if hasattr(config, "RIPPERDOC_OWNER_ROLE_ID") else None
        emp_role_id = getattr(config, "RIPPERDOC_EMPLOYEE_ROLE_ID", 0)
        emp_role = guild.get_role(emp_role_id) if emp_role_id else None
        owner_member = guild.get_member(self.ctx.author.id)
        if owner_role and owner_member:
            try:
                await owner_member.remove_roles(owner_role, reason=f"Ripperdoc store {store_name} closed")
            except (discord.Forbidden, discord.HTTPException):
                pass
        for emp_id in employees:
            if emp_role:
                emp_member = guild.get_member(emp_id)
                if emp_member:
                    try:
                        await emp_member.remove_roles(emp_role, reason=f"Ripperdoc store {store_name} closed")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
        from NightCityBot.utils.db import cancel_pending_transfers_for_store
        try:
            cancelled = await cancel_pending_transfers_for_store(store_id)
            if cancelled:
                logger.info("Cancelled %d pending transfer(s) for closed ripperdoc store %s", cancelled, store_id)
        except Exception:
            logger.warning("Failed to cancel pending transfers for ripperdoc store %s", store_id, exc_info=True)

        summary = f"✅ **{store_name}** has been closed.\n"
        summary += f"• {len(employees)} employee(s) disassociated\n"
        summary += f"• {len(inventory)} item(s) in store\n"
        if returned_items:
            returned_names = [f"**{l['item_name']}** @ ${l['unit_cost']:,}" for l in returned_items]
            summary += f"• {len(returned_items)} item(s) returned to wholesale:\n  " + "\n  ".join(returned_names)
        else:
            summary += "• No items returned to wholesale"
        await interaction.followup.send(summary, ephemeral=True)

        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Ripperdoc Store Closed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store", value=store_name, inline=False)
            embed.add_field(name="Owner", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Items Returned", value=str(len(returned_items)), inline=True)
            embed.add_field(name="Items Deleted", value=str(max(0, len(inventory) - len(returned_items))), inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Store closure cancelled.", view=None)
        self.stop()


class DMConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="You accepted the trade.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="You declined the trade.", view=None)
        self.stop()


class RipperdocHub(commands.Cog, name="RipperdocHub"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.unbelievaboat = bot.unbelievaboat
        self._panel_view = RipperdocMenuView()
        bot.add_view(self._panel_view)

    async def _log_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "CYBERWARE_LOG_CHANNEL_ID", 0)
        if not ch_id:
            return None
        ch = self.bot.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(ch_id))
            except Exception:
                pass
        return ch

    async def _resolve_member_from_input(self, guild: discord.Guild, raw: str) -> Optional[discord.Member]:
        import re
        raw = raw.strip()
        match = re.match(r"<@!?(\d+)>", raw)
        if match:
            uid = int(match.group(1))
        elif raw.isdigit():
            uid = int(raw)
        else:
            return None
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                return None
        return member

    @staticmethod
    def _panel_embed() -> discord.Embed:
        return discord.Embed(
            title="💉 Ripperdoc Shop",
            description=(
                "Welcome, Ripperdoc. Choose an action below.\n\n"
                "**Buy from Wholesale** — Purchase cyberware from this week's wholesale *(owners only)*\n"
                "**Wholesale List** — Browse this week's wholesale catalog\n"
                "**Sell to Patient** — Sell cyberware to a patient (DM confirmation)\n"
                "**Install on Patient** — Install cyberware on a patient (consumes item)\n"
                "**Manage Store** — Create/rename store, view stock, transfer or close *(owners only)*\n"
                "**Manage Employees** — Add/remove store employees *(owners only)*"
            ),
            color=discord.Color.teal(),
        )

    @commands.hybrid_command(name="ripperdoc")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ripperdoc_hub(self, ctx: commands.Context):
        """Post (or refresh) the persistent Ripperdoc panel in the designated channel."""
        channel = self.bot.get_channel(config.RIPPERDOC_HUB_CHANNEL_ID)
        if channel is None:
            await ctx.send("❌ Ripperdoc hub channel not found.", ephemeral=True)
            return
        view = RipperdocMenuView()
        await channel.send(embed=self._panel_embed(), view=view)
        await ctx.send("✅ Ripperdoc panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @commands.command(name="item_history")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def item_history_cmd(self, ctx: commands.Context, item_id: str):
        """Look up the full audit trail for an item by its UUID (admin/fixer only).

        Usage: !item_history <item_uuid>
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in the server.")
            return

        history = await ih_get_history(item_id.strip(), limit=50)
        if not history:
            await ctx.send(f"No history found for item `{item_id}`.")
            return

        lines = []
        for entry in history:
            ts = str(entry.get("timestamp", entry.get("created_at", "")))[:19].replace("T", " ")
            event = entry.get("event_type", "?")
            actor = entry.get("actor_id", "—")
            target = entry.get("target_id", "")
            price = entry.get("price")
            meta = entry.get("metadata", {})
            detail = ""
            if target:
                detail += f" → <@{target}>"
            if price is not None:
                detail += f" ${price:,}"
            if meta.get("item_name"):
                detail += f" ({meta['item_name']})"
            lines.append(f"`{ts}` **{event}** by <@{actor}>{detail}")

        embed = discord.Embed(
            title=f"📜 Item History — `{item_id[:8]}...`",
            description="\n".join(lines[:25]),
            color=discord.Color.greyple(),
        )
        embed.set_footer(text=f"{len(history)} event(s) total")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(RipperdocHub(bot))
