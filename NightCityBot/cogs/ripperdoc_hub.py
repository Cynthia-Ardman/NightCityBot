"""Unified !ripperdoc hub command — interactive cyberware shop interface.

Consolidates the separate cw_* command set into a single interactive hub
with Discord dropdowns, buttons, and inline component flows.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView, send_ephemeral, respond_ephemeral, log_panel_failure
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
        await send_ephemeral(interaction, "Store cyberware stock is empty.")
        return
    from NightCityBot.utils.helpers import format_cw_lines_grouped
    store_lots = []
    groups = cw_cog._grouped_inventory(inventory)
    for g in groups:
        sample = g["items"][0] if g.get("items") else {}
        store_lots.append({
            "item_name": g["name"],
            "cwp": sample.get("cwp", ""),
            "slot": sample.get("slot", ""),
            "unit_cost": int(g.get("price_paid") or 0),
            "qty_available": g["count"],
        })
    lines = format_cw_lines_grouped(store_lots, max_items=30)
    store_name = store.get("store_name") or f"Store {store_id}"
    embed = discord.Embed(
        title=f"📦 {store_name}",
        description="\n".join(lines) if lines else "Empty",
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"{len(inventory)} item(s) total")
    await send_ephemeral(interaction, embed=embed)


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
            await respond_ephemeral(interaction, "Could not verify your role.")
            return False
        if _is_ripperdoc_owner(member) or _is_ripperdoc_employee(member) or member.guild_permissions.administrator:
            return True
        if any(r.id == config.RIPPERDOC_ROLE_ID for r in member.roles):
            return True
        await respond_ephemeral(interaction, "This panel is for Ripperdocs only.")
        await log_panel_failure(interaction.client, "CYBERWARE_LOG_CHANNEL_ID", "Ripperdoc Panel", interaction.user, "Missing ripperdoc role")
        return False

    @discord.ui.button(label="Buy from Catalogue", style=discord.ButtonStyle.primary, emoji="🛒", row=0, custom_id="ripperdoc:buy_wholesale")
    async def buy_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await respond_ephemeral(interaction, 
                "Only Ripperdoc Owners can buy from the catalogue.")
            await log_panel_failure(interaction.client, "CYBERWARE_LOG_CHANNEL_ID", "Buy CW Catalogue", interaction.user, "Employee tried to buy from catalogue (owner-only)")
            return
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        guild = interaction.guild
        if guild:
            store_id = _rd_store_id(guild.id, interaction.user.id)
            rd_store = state.get("ripperdoc_stores", {}).get(store_id)
            if not rd_store:
                await send_ephemeral(interaction, 
                    "❌ You don't have an initialized ripperdoc store. "
                    "Please set up your store first before buying from the catalogue.")
                return
        catalog = await cw_catalog_get_all()
        lots = _build_cw_catalog_lots(catalog)
        if not lots:
            await send_ephemeral(interaction, "No cyberware available in the catalog.")
            return
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = CatalogueBuySelect(cog, ctx, lots, cw_cog)
        await send_ephemeral(interaction, "Select an item to buy:", view=view)

    @discord.ui.button(label="Sell to Patient", style=discord.ButtonStyle.success, emoji="💉", row=1, custom_id="ripperdoc:sell_patient")
    async def sell_to_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        state = await cw_cog._load_state()
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        accessible = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if len(accessible) > 1:
            view = _RDStorePickerForAction(cog, ctx, accessible, action="sell", cw_cog=cw_cog)
            await send_ephemeral(interaction, 
                "You have access to multiple stores. Select which store to sell from:",
                view=view)
            return
        inv_owner_id = interaction.user.id
        if accessible:
            store_id, store_data = accessible[0]
            inv_owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        else:
            store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        inventory = await cw_cog._load_inventory(inv_owner_id)
        if not inventory:
            await send_ephemeral(interaction, "Store cyberware stock is empty. Buy from the catalogue first.")
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(cog, ctx, groups, mode="sell", store_id=store_id, inv_owner_id=inv_owner_id)
        msg = "**Step 1** — Select the patient and the item to sell:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await send_ephemeral(interaction, msg, view=view)

    @discord.ui.button(label="Install on Patient", style=discord.ButtonStyle.success, emoji="🔧", row=1, custom_id="ripperdoc:install_patient")
    async def install_on_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        state = await cw_cog._load_state()
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        accessible = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if len(accessible) > 1:
            view = _RDStorePickerForAction(cog, ctx, accessible, action="install", cw_cog=cw_cog)
            await send_ephemeral(interaction, 
                "You have access to multiple stores. Select which store to install from:",
                view=view)
            return
        inv_owner_id = interaction.user.id
        if accessible:
            store_id, store_data = accessible[0]
            inv_owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        else:
            store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        inventory = await cw_cog._load_inventory(inv_owner_id)
        if not inventory:
            await send_ephemeral(interaction, "Store cyberware stock is empty. Buy from the catalogue first.")
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(cog, ctx, groups, mode="install", store_id=store_id, inv_owner_id=inv_owner_id)
        msg = "**Step 1** — Select the patient and the item to install:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await send_ephemeral(interaction, msg, view=view)

    @discord.ui.button(label="Catalogue List", style=discord.ButtonStyle.secondary, emoji="📋", row=0, custom_id="ripperdoc:wholesale_list")
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        catalog = await cw_catalog_get_all()
        lots = _build_cw_catalog_lots(catalog)
        if not lots:
            await send_ephemeral(interaction, "No cyberware available in the catalog.")
            return
        from NightCityBot.utils.helpers import format_cw_lines_grouped
        lines = format_cw_lines_grouped(lots, max_items=len(lots), show_sold_out=False)
        text = "\n".join(lines) if lines else "Empty"
        if len(text) <= 4096:
            embed = discord.Embed(
                title="🔩 Cyberware Catalog",
                description=text,
                color=discord.Color.teal(),
            )
            await send_ephemeral(interaction, embed=embed)
        else:
            mid = len(lines) // 2
            embed1 = discord.Embed(
                title="🔩 Cyberware Catalog (1/2)",
                description="\n".join(lines[:mid]),
                color=discord.Color.teal(),
            )
            embed2 = discord.Embed(
                title="🔩 Cyberware Catalog (2/2)",
                description="\n".join(lines[mid:]),
                color=discord.Color.teal(),
            )
            await send_ephemeral(interaction, embeds=[embed1, embed2])

    @discord.ui.button(label="Manage Store", style=discord.ButtonStyle.danger, emoji="⚙️", row=2, custom_id="ripperdoc:manage_store")
    async def manage_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await respond_ephemeral(interaction, 
                "Only Ripperdoc Owners can manage their store.")
            return
        await interaction.response.defer(ephemeral=True)
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
        await send_ephemeral(interaction, header, view=view)

    @discord.ui.button(label="Manage Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=2, custom_id="ripperdoc:manage_employees")
    async def manage_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and _is_ripperdoc_employee(member) and not _is_ripperdoc_owner(member):
            await respond_ephemeral(interaction, 
                "Only Ripperdoc Owners can manage employees.")
            return
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDManageEmployeesView(cog, ctx)
        await respond_ephemeral(interaction, 
            "👥 **Manage Employees** — choose an action:", view=view)

    @discord.ui.button(label="Checkup", style=discord.ButtonStyle.primary, emoji="🩺", row=3, custom_id="ripperdoc:checkup")
    async def checkup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("cyberware"):
            await send_ephemeral(interaction, "⚠️ The cyberware system is currently disabled.")
            return
        guild = interaction.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in a server.")
            return
        role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        if role is None:
            await send_ephemeral(interaction, "⚠️ Checkup role is not configured.")
            return
        view = _CheckupPatientSelectView(interaction.user.id)
        await send_ephemeral(interaction, 
            "🩺 **Checkup** — Select the patient to check up on:", view=view)


class _CheckupPatientSelectView(SafeView):
    def __init__(self, ripperdoc_id: int):
        super().__init__(timeout=300)
        self._ripperdoc_id = ripperdoc_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ripperdoc_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a patient…", row=0)
    async def patient_select(self, interaction: discord.Interaction,
                             select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        patient = select.values[0]
        guild = interaction.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in a server.")
            return
        member = guild.get_member(patient.id)
        if not member:
            try:
                member = await guild.fetch_member(patient.id)
            except Exception:
                await send_ephemeral(interaction, "❌ Could not find that member.")
                return

        role = guild.get_role(config.CYBER_CHECKUP_ROLE_ID)
        if role is None:
            await send_ephemeral(interaction, "⚠️ Checkup role is not configured.")
            return
        if role not in member.roles:
            await send_ephemeral(interaction, 
                f"{member.display_name} does not have the checkup role.")
            return

        try:
            await member.remove_roles(role, reason="Cyberware check-up completed via Ripperdoc Hub")
        except (discord.Forbidden, discord.HTTPException) as e:
            await send_ephemeral(interaction, f"❌ Could not remove checkup role: {e}")
            return
        await send_ephemeral(interaction, 
            f"✅ Removed checkup role from {member.display_name}.")

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
            await send_ephemeral(interaction, 
                f"⚠️ Role removed but DB update failed: {e}")
            self.stop()
            return
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog and hasattr(cw_cog, "data"):
            cw_cog.data[str(member.id)] = {"weeks": 0, "last": None}
        self.stop()


def _build_cw_catalog_lots(catalog: list[dict]) -> list[dict]:
    lots = []
    for item in catalog:
        lots.append({
            "lot_id": f"cat-{item['name']}",
            "item_name": item["name"],
            "unit_cost": int(item.get("price", 0)),
            "cwp": item.get("cwp", ""),
            "slot": item.get("slot", ""),
            "qty_available": 99,
        })
    lots.sort(key=lambda l: l["item_name"])
    return lots


class CatalogueBuySelect(SafeView):
    PAGE_SIZE = 25

    def __init__(self, cog: "RipperdocHub", ctx: commands.Context, lots: list, cw_cog, *, page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.lots = lots
        self.cw_cog = cw_cog
        self.page = page
        self._build_page()

    def _build_page(self):
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        page_lots = self.lots[start:start + self.PAGE_SIZE]
        options = []
        for i, lot in enumerate(page_lots):
            label = f"{lot['item_name']} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(start + i),
            ))
        total_pages = max(1, (len(self.lots) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        placeholder = f"Choose an item... (page {self.page + 1}/{total_pages})" if total_pages > 1 else "Choose an item..."
        self.select = discord.ui.Select(placeholder=placeholder, options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        if total_pages > 1:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
            prev_btn.callback = self._prev_page
            self.add_item(prev_btn)
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= total_pages - 1)
            next_btn.callback = self._next_page
            self.add_item(next_btn)

    async def _prev_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build_page()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction):
        total_pages = max(1, (len(self.lots) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(total_pages - 1, self.page + 1)
        self._build_page()
        await interaction.response.edit_message(view=self)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        lot = self.lots[idx]
        max_qty = int(lot.get("qty_available", 1))
        qty_view = QtySelectView(interaction.user.id, max_qty)
        await respond_ephemeral(interaction, 
            f"**{lot['item_name']}** — how many? (max {max_qty})",
            view=qty_view)
        await qty_view.wait()
        if qty_view.result is None:
            await send_ephemeral(interaction, "⏰ Timed out.")
            return

        qty = qty_view.result
        unit_cost = int(lot["unit_cost"])
        total = unit_cost * qty
        member = self.ctx.author

        balance = await self.cog.unbelievaboat.get_balance(member.id)
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
        ok = await self.cog.unbelievaboat.update_balance(
            member.id,
            {"cash": -cash_deduct, "bank": -bank_deduct},
            reason=f"CW catalogue buy: {lot['item_name']} x{qty}",
        )
        if not ok:
            await send_ephemeral(interaction, "Payment failed.")
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
                logger.error("cw catalogue buy: _save_inventory failed — refunding buyer=%s", member.id)
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="CW catalogue refund — inventory save failed",
                )
                await send_ephemeral(interaction, 
                    "⚠️ Purchase failed (inventory save error). Payment has been refunded. Please try again.")
                return

        await send_ephemeral(interaction, 
            f"Purchased **{lot['item_name']}** ×{qty} for **${total:,}**.")
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🛒 Cyberware Catalogue Purchase",
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
        super().__init__(timeout=300)
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

    def _status_content(self) -> str:
        parts = []
        if self.selected_patient:
            parts.append(f"Patient: **{self.selected_patient.display_name}** ✓")
        if self.selected_character:
            parts.append(f"Character: **{self.selected_character['name']}** ✓")
        if self.selected_group_idx is not None:
            parts.append(f"Item: **{self.groups[self.selected_group_idx]['name']}** ✓")
        if not parts:
            return "Select a patient, character, and item, then press Continue."
        return " — ".join(parts)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a patient…", row=0)
    async def patient_select(self, interaction: discord.Interaction,
                             select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        if user is None:
            await send_ephemeral(interaction, 
                "Please select a server member.")
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
                    await send_ephemeral(interaction, 
                        "That user doesn't appear to be in this server.")
                    return
            else:
                await send_ephemeral(interaction, 
                    "Could not resolve server member.")
                return
        self.selected_character = None
        characters = await get_active_characters(str(self.selected_patient.id))
        if not characters:
            await send_ephemeral(interaction, 
                f"❌ {self.selected_patient.display_name} has no active characters. "
                "They must create a character before receiving items.")
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
        await interaction.edit_original_response(
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
        self.selected_group_idx = int(interaction.data["values"][0])
        selected_val = interaction.data["values"][0]
        stock_select = next(
            (item for item in self.children
             if isinstance(item, discord.ui.Select) and item.row == 2),
            None,
        )
        if stock_select:
            for opt in stock_select.options:
                opt.default = (opt.value == selected_val)
        await interaction.response.edit_message(
            content=self._status_content(),
            view=self,
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        # Defer first — ensure_character_active hits the DB and could otherwise
        # blow past the 3s interaction ack deadline.
        await interaction.response.defer(ephemeral=True)
        if self.selected_patient is None:
            await send_ephemeral(interaction,
                "Please select a patient first.")
            return
        if self.selected_character is None:
            await send_ephemeral(interaction,
                "Please select a character for the patient.")
            return
        if self.selected_group_idx is None:
            await send_ephemeral(interaction,
                "Please select an item from your stock first.")
            return
        if not await ensure_character_active(self.selected_character["character_id"]):
            await send_ephemeral(interaction,
                f"❌ Character **{self.selected_character['name']}** is no longer active.")
            return
        group = self.groups[self.selected_group_idx]
        label = "install fee" if self.mode == "install" else "price to charge"
        await send_ephemeral(interaction,
            f"📝 **Enter the {label}** (number only, `0` for free), or type `cancel`:")
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
        await send_ephemeral(interaction, "Character selection required.")
        return
    if character_id and not await ensure_character_active(character_id):
        await send_ephemeral(interaction, 
            f"❌ Character **{character_name}** is no longer active.")
        return

    item_name = group["name"]
    selected = group["items"][0]
    item_id = selected.get("item_id", str(uuid.uuid4()))

    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await send_ephemeral(interaction, "Cyberware system unavailable.")
        return

    owner_id = inv_owner_id or ctx.author.id
    if store_id:
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, owner_id)

    confirm_view = DMConfirmView(recipient_id=patient.id, timeout=300)
    try:
        dm_msg = await patient.send(
            f"**{ctx.author.display_name}** wants to sell you **{item_name}** "
            f"for **${price:,}** (character: **{character_name}**).\n"
            "Do you accept?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await send_ephemeral(interaction, 
            f"Cannot DM {patient.display_name}. They may have DMs disabled.")
        return

    await send_ephemeral(interaction, 
        f"Confirmation sent to {patient.display_name} via DM. Waiting...")
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Trade declined or timed out.", view=None)
        except Exception:
            pass
        await send_ephemeral(interaction,
            f"{patient.display_name} declined or didn't respond to the sale of **{item_name}**."
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
            await send_ephemeral(interaction, f"Could not fetch {patient.display_name}'s balance. Sale cancelled.")
            return
        p_cash = int(balance.get("cash", 0))
        p_bank = int(balance.get("bank", 0))
        if p_cash + p_bank < price:
            await send_ephemeral(interaction,
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
            await send_ephemeral(interaction, f"Payment failed for {patient.display_name}. Sale cancelled.")
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
            await send_ephemeral(interaction,
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
            await send_ephemeral(interaction, "Item no longer in stock. Refunded.")
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
            await send_ephemeral(interaction, "⚠️ Sale failed (save error). Payment has been refunded.")
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
            restore_entry = {
                "item_id": item_id,
                "name": item_name,
                "price_paid": price,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            }
            if selected.get("cwp"):
                restore_entry["cwp"] = selected["cwp"]
            if selected.get("slot"):
                restore_entry["slot"] = selected["slot"]
            inv_restore.append(restore_entry)
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
        await send_ephemeral(interaction,
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

    await send_ephemeral(interaction, 
        f"Sold **{item_name}** to **{character_name}** ({patient.display_name}) "
        f"for **${price:,}**.")
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
        await send_ephemeral(interaction, "Character selection required.")
        return
    if character_id and not await ensure_character_active(character_id):
        await send_ephemeral(interaction, 
            f"❌ Character **{character_name}** is no longer active.")
        return

    item_name = group["name"]
    selected = group["items"][0]
    item_id = selected.get("item_id", str(uuid.uuid4()))

    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await send_ephemeral(interaction, "Cyberware system unavailable.")
        return

    owner_id = inv_owner_id or ctx.author.id
    if store_id:
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, owner_id)

    price_text = f" for **${price:,}**" if price > 0 else " (free install)"
    confirm_view = DMConfirmView(recipient_id=patient.id, timeout=300)
    try:
        dm_msg = await patient.send(
            f"**{ctx.author.display_name}** wants to install **{item_name}** on "
            f"**{character_name}**{price_text}.\n"
            "This will consume the cyberware. Do you accept?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await send_ephemeral(interaction, 
            f"Cannot DM {patient.display_name}. They may have DMs disabled.")
        return

    await send_ephemeral(interaction, 
        f"Confirmation sent to {patient.display_name} via DM. Waiting...")
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Installation declined or timed out.", view=None)
        except Exception:
            pass
        await send_ephemeral(interaction,
            f"{patient.display_name} declined or didn't respond to the install of **{item_name}**."
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
            await send_ephemeral(interaction, f"Could not fetch {patient.display_name}'s balance. Install cancelled.")
            return
        p_cash = int(balance.get("cash", 0))
        p_bank = int(balance.get("bank", 0))
        if p_cash + p_bank < price:
            await send_ephemeral(interaction,
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
            await send_ephemeral(interaction, f"Payment failed for {patient.display_name}. Install cancelled.")
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
            await send_ephemeral(interaction,
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
            await send_ephemeral(interaction, "Item no longer in stock. Refunded.")
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
            await send_ephemeral(interaction, "⚠️ Install failed (save error). Payment has been refunded.")
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
        logger.error("cw install: pi_add_item failed — attempting compensation")
        async with cw_cog._locks.acquire(str(owner_id)):
            inv_restore = await cw_cog._load_inventory(owner_id)
            restore_entry = {
                "item_id": item_id,
                "name": item_name,
                "price_paid": price,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            }
            if selected.get("cwp"):
                restore_entry["cwp"] = selected["cwp"]
            if selected.get("slot"):
                restore_entry["slot"] = selected["slot"]
            inv_restore.append(restore_entry)
            await cw_cog._save_inventory(owner_id, inv_restore)
            logger.info("cw install: restored item_id=%s to ripperdoc=%s stock", item_id, owner_id)
        if price > 0:
            await cog.unbelievaboat.update_balance(
                patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW install refund — item grant failed"
            )
            if seller_credited:
                await cog.unbelievaboat.update_balance(
                    owner_id, {"bank": -price}, reason="CW install refund — item grant failed"
                )
        await send_ephemeral(interaction,
            f"⚠️ Failed to add **{item_name}** to {patient.display_name}'s inventory. "
            "Payment has been refunded and item has been restored to stock. Please contact an admin."
        )
        return

    await ih_record_event(
        item_id, "cw_installed",
        actor_id=str(ctx.author.id),
        target_id=str(patient.id),
        price=price if price > 0 else None,
        metadata={"item_name": item_name, "character": character_name},
    )

    await send_ephemeral(interaction, 
        f"Installed **{item_name}** on **{character_name}** ({patient.display_name}).")
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
        super().__init__(timeout=300)
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
            await respond_ephemeral(interaction, "Store not found.")
            return
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cw_cog or interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        owner_id = _get_rd_owner_id(state, store_id, self.ctx.author.id)
        if self.action == "view_stock":
            await _show_rd_stock(interaction, cw_cog, store, store_id, owner_id)
            self.stop()
            return
        inventory = await cw_cog._load_inventory(owner_id)
        if not inventory:
            await send_ephemeral(interaction, "Store cyberware stock is empty.")
            return
        groups = cw_cog._grouped_inventory(inventory)
        mode = "install" if self.action == "install" else "sell"
        view = SellSetupView(self.cog, self.ctx, groups, mode=mode,
                             store_id=store_id, inv_owner_id=owner_id)
        label = "install" if mode == "install" else "sell"
        msg = f"**Step 1** — Select the patient and the item to {label}:"
        if view.truncated:
            msg += f"\n⚠️ Showing first 25 of {len(groups)} item groups."
        await send_ephemeral(interaction, msg, view=view)
        self.stop()


class _RDManageEmployeesView(SafeView):
    def __init__(self, cog, ctx):
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
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDAddEmployeeView(cog, ctx)
        await respond_ephemeral(interaction, 
            "➕ **Select the member to add as employee:**", view=view)

    @discord.ui.button(label="Remove Employee", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id, {})
        employees = store.get("employees", [])
        if not employees:
            await send_ephemeral(interaction, "No employees to remove.")
            return
        options = []
        for eid in employees[:25]:
            options.append(discord.SelectOption(label=str(eid), value=str(eid)))
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDRemoveEmployeeView(cog, ctx, options)
        await send_ephemeral(interaction, 
            "➖ **Select the employee to remove:**", view=view)

    @discord.ui.button(label="View Employees", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id, {})
        employees = store.get("employees", [])
        if not employees:
            await send_ephemeral(interaction, "No employees registered.")
            return
        lines = [f"<@{eid}>" for eid in employees]
        await send_ephemeral(interaction, 
            f"👥 **Employees** ({len(employees)}):\n" + "\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


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
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member…", row=0)
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await respond_ephemeral(interaction, "Please select a member.")
            return
        guild = self.ctx.guild
        new_emp = raw_user if isinstance(raw_user, discord.Member) else (guild.get_member(raw_user.id) if guild else None)
        if new_emp is None:
            try:
                new_emp = await guild.fetch_member(raw_user.id)
            except Exception:
                await respond_ephemeral(interaction, "Could not find that member.")
                return
        if new_emp.id == self.ctx.author.id:
            await respond_ephemeral(interaction, "You can't add yourself as an employee.")
            return
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(guild.id, self.ctx.author.id)
        stores = state.get("ripperdoc_stores", {})
        store = stores.get(store_id, {"owner_id": self.ctx.author.id, "employees": []})
        employees = store.get("employees", [])
        if new_emp.id in employees:
            await send_ephemeral(interaction, f"{new_emp.display_name} is already an employee.")
            self.stop()
            return
        store_name = store.get("store_name") or f"{self.ctx.author.display_name}'s Ripperdoc"
        dm_view = _EmployeeDMConfirmView(new_emp.id, timeout=300)
        try:
            dm_msg = await new_emp.send(
                f"📋 **{self.ctx.author.display_name}** wants to hire you as an employee at **{store_name}**.\n"
                "Do you accept?",
                view=dm_view,
            )
        except discord.Forbidden:
            await send_ephemeral(interaction, 
                f"❌ Could not DM {new_emp.display_name}. They may have DMs disabled.")
            self.stop()
            return
        await send_ephemeral(interaction, 
            f"📨 Sent a DM to **{new_emp.display_name}** — waiting for their response…")
        timed_out = await dm_view.wait()
        if timed_out or not dm_view.accepted:
            reason = "timed out" if timed_out else "declined"
            await send_ephemeral(interaction, 
                f"❌ **{new_emp.display_name}** {reason} the employee offer.")
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
        log_ch = await self.cog._log_channel()
        if log_ch:
            try:
                await log_ch.send(
                    f"➕ **Ripperdoc Employee Added** — {new_emp.display_name} ({new_emp.id}) "
                    f"hired at **{store_name}** by {self.ctx.author.display_name}."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, 
            f"✅ **{new_emp.display_name}** accepted and has been added as employee at **{store_name}**.")
        self.stop()


class _RDRemoveEmployeeView(SafeView):
    def __init__(self, cog, ctx, options):
        super().__init__(timeout=300)
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
            await send_ephemeral(interaction, "Cyberware system unavailable.")
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
                await send_ephemeral(interaction, f"✅ Employee <@{emp_id}> removed.",
                                                allowed_mentions=discord.AllowedMentions.none())
            else:
                await send_ephemeral(interaction, "Employee not found.")
                self.stop()
                return
        log_ch = await self.cog._log_channel()
        if log_ch:
            try:
                await log_ch.send(
                    f"➖ **Ripperdoc Employee Removed** — <@{emp_id}> "
                    f"removed by {self.ctx.author.display_name}."
                )
            except Exception:
                pass
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
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        existing = state.get("ripperdoc_stores", {}).get(store_id)
        if existing and existing.get("store_name"):
            await send_ephemeral(interaction, 
                f"You already own a ripperdoc store: **{existing['store_name']}**.")
            return
        await send_ephemeral(interaction, 
            "🏪 **Enter a name for your new store** (e.g. `Chrome Cathedral`), or type `cancel`:")
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        name = text.strip()[:100]
        if not name:
            await send_ephemeral(interaction, "Name cannot be empty.")
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
        log_ch = await self.cog._log_channel()
        if log_ch:
            try:
                await log_ch.send(
                    f"🏪 **Ripperdoc Store Created** — {interaction.user.display_name} ({interaction.user.id}) "
                    f"created store **{name}**."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, f"✅ Store **{name}** created!")

    @discord.ui.button(label="Change Store Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def change_store_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        store_id = _rd_store_id(interaction.guild.id, interaction.user.id)
        store = state.get("ripperdoc_stores", {}).get(store_id)
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
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            stores = state.setdefault("ripperdoc_stores", {})
            store = stores.setdefault(store_id, {"owner_id": interaction.user.id, "employees": []})
            store["store_name"] = name
            await cw_cog._save_state(state)
        log_ch = await self.cog._log_channel()
        if log_ch:
            old_label = f" (was **{current_name}**)" if current_name else ""
            try:
                await log_ch.send(
                    f"✏️ **Ripperdoc Store Renamed** — {interaction.user.display_name} ({interaction.user.id}) "
                    f"renamed store to **{name}**{old_label}."
                )
            except Exception:
                pass
        await send_ephemeral(interaction, f"Store name changed to **{name}**.")

    @discord.ui.button(label="My Stock", style=discord.ButtonStyle.secondary, emoji="📦", row=0)
    async def view_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        stores = _find_rd_accessible_stores(state, interaction.guild.id, interaction.user.id, member)
        if not stores:
            await send_ephemeral(interaction, "You are not assigned to any store.")
            return
        if len(stores) > 1:
            cog = interaction.client.get_cog("RipperdocHub")
            ctx = PanelContext(interaction)
            view = _RDStorePickerForAction(cog, ctx, stores, action="view_stock", cw_cog=cw_cog)
            await send_ephemeral(interaction, 
                "📦 **Select which store stock to view:**", view=view)
            return
        store_id, store = stores[0]
        owner_id = _get_rd_owner_id(state, store_id, interaction.user.id)
        await _show_rd_stock(interaction, cw_cog, store, store_id, owner_id)

    @discord.ui.button(label="Transfer Ownership", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def transfer_ownership(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDTransferOwnerView(cog, ctx)
        await respond_ephemeral(interaction, 
            "🔄 **Select the new owner for your ripperdoc store:**", view=view)

    @discord.ui.button(label="Close Store", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def close_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RipperdocHub")
        ctx = PanelContext(interaction)
        view = _RDCloseConfirmView(cog, ctx)
        await respond_ephemeral(interaction, 
            "⚠️ **Are you sure you want to close your ripperdoc store?**\n"
            "This will:\n"
            "• Remove all employees\n"
            "• Delete the store name\n"
            "• Clear all inventory\n\n"
            "**This action cannot be undone.**",
            view=view)


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
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        state = await cw_cog._load_state()
        new_store_id = _rd_store_id(guild.id, new_owner.id)
        existing = state.get("ripperdoc_stores", {}).get(new_store_id)
        if existing and existing.get("store_name"):
            await send_ephemeral(interaction, 
                f"{new_owner.display_name} already owns a ripperdoc store.")
            return
        old_store_id = _rd_store_id(guild.id, self.ctx.author.id)
        store = state.get("ripperdoc_stores", {}).get(old_store_id)
        if not store:
            await send_ephemeral(interaction, "You don't have a store to transfer.")
            return
        store_name = store.get("store_name") or "Ripperdoc Store"
        confirm_view = _RDTransferDMConfirmView(new_owner.id, timeout=300)
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
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            old_store_id = _rd_store_id(guild.id, self.ctx.author.id)
            store = state.get("ripperdoc_stores", {}).pop(old_store_id, None)
            if not store:
                await send_ephemeral(interaction, "You don't have a store to transfer.")
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
        await send_ephemeral(interaction, 
            f"✅ **{store_name}** has been transferred to {new_owner.display_name}.")
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
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await send_ephemeral(interaction, "Cyberware system unavailable.")
            return
        guild = self.ctx.guild
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            store_id = _rd_store_id(guild.id, self.ctx.author.id)
            store = state.get("ripperdoc_stores", {}).pop(store_id, None)
            if not store:
                await send_ephemeral(interaction, "No store found to close.")
                return
            store_name = store.get("store_name") or "Ripperdoc Store"
            inventory = await cw_cog._load_inventory(self.ctx.author.id)
            cleared_count = len(inventory) if inventory else 0
            if inventory:
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
        summary += f"• {cleared_count} item(s) cleared from inventory"
        await send_ephemeral(interaction, summary)

        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Ripperdoc Store Closed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store", value=store_name, inline=False)
            embed.add_field(name="Owner", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Items Cleared", value=str(cleared_count), inline=True)
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
                "**Buy from Catalogue** — Purchase cyberware from the full catalog *(owners only)*\n"
                "**Catalogue List** — Browse the full cyberware catalog\n"
                "**Sell to Patient** — Sell cyberware to a patient (DM confirmation)\n"
                "**Install on Patient** — Install cyberware on a patient (consumes item)\n"
                "**Manage Store** — Create/rename store, view stock, transfer or close *(owners only)*\n"
                "**Manage Employees** — Add/remove store employees *(owners only)*"
            ),
            color=discord.Color.teal(),
        )

    @staticmethod
    def _guide_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📘 Ripperdoc Shop — How It Works",
            description=(
                "This panel is for ripperdoc owners and employees. "
                "Use the buttons below to stock cyberware, sell or install on patients, and manage your clinic. "
                "All responses are private and **auto-delete after 5 minutes**."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="🛒 Buy from Catalogue",
            value="Purchase cyberware from the full catalog to add to your clinic stock. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="📋 Catalogue List",
            value="Browse what's currently available in the catalogue — no purchase required.",
            inline=False,
        )
        embed.add_field(
            name="💉 Sell to Patient",
            value="Sell cyberware from your stock to a patient. They'll get a DM to confirm the purchase.",
            inline=False,
        )
        embed.add_field(
            name="🔧 Install on Patient",
            value="Install a piece of cyberware directly on a patient. This consumes the item from your stock and applies it to their character.",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Manage Store",
            value="Create your clinic, change its name, view stock, transfer ownership, or close it. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="👥 Manage Employees",
            value="Add or remove employees who can sell and install on your behalf. *(Owners only)*",
            inline=False,
        )
        embed.add_field(
            name="🩺 Checkup",
            value="Perform a cyberware checkup on a patient — removes the Checkup Required role and resets their timer.",
            inline=False,
        )
        return embed

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
        await channel.send(embed=self._guide_embed(), view=view)
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
