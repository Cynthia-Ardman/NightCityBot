"""Unified !ripperdoc hub command — interactive cyberware shop interface.

Consolidates the separate cw_* command set into a single interactive hub
with Discord dropdowns, buttons, and modals.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.db import (
    cw_catalog_get_all,
    pi_add_item,
    pi_get_by_owner,
    ih_record_event,
    ih_get_history,
    pt_create,
)
from NightCityBot.utils.permissions import is_ripperdoc, is_fixer

logger = logging.getLogger(__name__)


class RipperdocMenuView(discord.ui.View):
    def __init__(self, cog: "RipperdocHub", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass

    @discord.ui.button(label="Buy from Wholesale", style=discord.ButtonStyle.primary, emoji="🛒", row=0)
    async def buy_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        lots = state.get("cw_wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await interaction.followup.send("No wholesale stock available this week.", ephemeral=True)
            return
        options = []
        for i, lot in enumerate(available[:25]):
            label = f"{lot['item_name']} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(i),
                description=f"Lot: {lot.get('lot_id', '?')}"[:100],
            ))
        view = WholesaleBuySelect(self.cog, self.ctx, available, cw_cog)
        await interaction.followup.send("Select an item to buy:", view=view, ephemeral=True)

    @discord.ui.button(label="Sell to Patient", style=discord.ButtonStyle.success, emoji="💉", row=0)
    async def sell_to_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        inventory = await cw_cog._load_inventory(self.ctx.author.id)
        if not inventory:
            await interaction.followup.send("Your cyberware stock is empty. Buy from wholesale first.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(self.cog, self.ctx, groups, mode="sell")
        msg = "**Step 1** — Select the patient and the item to sell:"
        if view.truncated:
            msg += (
                f"\n⚠️ Showing first 25 of {len(groups)} item groups. "
                "Use `!cw_sell @patient <row> <price> <name>` for items beyond 25."
            )
        await interaction.followup.send(msg, view=view, ephemeral=True)

    @discord.ui.button(label="Install on Patient", style=discord.ButtonStyle.success, emoji="🔧", row=0)
    async def install_on_patient(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        inventory = await cw_cog._load_inventory(self.ctx.author.id)
        if not inventory:
            await interaction.followup.send("Your cyberware stock is empty. Buy from wholesale first.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        view = SellSetupView(self.cog, self.ctx, groups, mode="install")
        msg = "**Step 1** — Select the patient and the item to install:"
        if view.truncated:
            msg += (
                f"\n⚠️ Showing first 25 of {len(groups)} item groups. "
                "Use `!cw_install @patient <row> <fee> <name>` for items beyond 25."
            )
        await interaction.followup.send(msg, view=view, ephemeral=True)

    @discord.ui.button(label="My Stock", style=discord.ButtonStyle.secondary, emoji="📦", row=1)
    async def view_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        inventory = await cw_cog._load_inventory(self.ctx.author.id)
        if not inventory:
            await interaction.followup.send("Your cyberware stock is empty.", ephemeral=True)
            return
        groups = cw_cog._grouped_inventory(inventory)
        lines = []
        for i, g in enumerate(groups, 1):
            qty_str = f" ×{g['count']}" if g["count"] > 1 else ""
            lines.append(f"`{i}.` **{g['name']}**{qty_str}")
        embed = discord.Embed(
            title=f"📦 {self.ctx.author.display_name}'s CW Stock",
            description="\n".join(lines[:30]),
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"{len(inventory)} item(s) total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Wholesale List", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
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
            if qty > 0:
                lines.append(f"`{i}.` **{lot['item_name']}** — ${price:,} × {qty}")
            else:
                lines.append(f"~~`{i}.` {lot['item_name']}~~ — Sold out")
        embed = discord.Embed(
            title="🔩 Cyberware Wholesale",
            description="\n".join(lines[:30]),
            color=discord.Color.teal(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class WholesaleBuySelect(discord.ui.View):
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
        await interaction.response.send_modal(BuyQtyModal(self.cog, self.ctx, lot, self.cw_cog))


class BuyQtyModal(discord.ui.Modal, title="Buy Cyberware"):
    qty_input = discord.ui.TextInput(label="Quantity", default="1", max_length=3)

    def __init__(self, cog: "RipperdocHub", ctx: commands.Context, lot: dict, cw_cog):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.lot = lot
        self.cw_cog = cw_cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            qty = int(self.qty_input.value)
        except ValueError:
            await interaction.followup.send("Invalid quantity.", ephemeral=True)
            return
        if qty < 1:
            await interaction.followup.send("Quantity must be at least 1.", ephemeral=True)
            return
        if qty > int(self.lot.get("qty_available", 0)):
            await interaction.followup.send(
                f"Only {self.lot['qty_available']} available.", ephemeral=True
            )
            return

        unit_cost = int(self.lot["unit_cost"])
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
            reason=f"CW wholesale buy: {self.lot['item_name']} x{qty}",
        )
        if not ok:
            await interaction.followup.send("Payment failed.", ephemeral=True)
            return

        async with self.cw_cog.lock:
            state = await self.cw_cog._load_state()
            lots = state.get("cw_wholesale_lots", [])
            target_lot = self.cw_cog._lookup_lot(lots, self.lot["item_name"])
            if not target_lot or int(target_lot.get("qty_available", 0)) < qty:
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="CW wholesale buy refund — stock depleted",
                )
                await interaction.followup.send("Stock depleted. Refunded.", ephemeral=True)
                return
            target_lot["qty_available"] = int(target_lot["qty_available"]) - qty
            await self.cw_cog._save_state(state)

            inventory = await self.cw_cog._load_inventory(member.id)
            for _ in range(qty):
                item_id = str(uuid.uuid4())
                inventory.append({
                    "item_id": item_id,
                    "name": self.lot["item_name"],
                    "price_paid": unit_cost,
                    "purchased_at": datetime.now(timezone.utc).isoformat(),
                })
                await ih_record_event(
                    item_id, "cw_wholesale_buy",
                    actor_id=str(member.id),
                    price=unit_cost,
                    metadata={"item_name": self.lot["item_name"], "lot_id": self.lot.get("lot_id")},
                )
            await self.cw_cog._save_inventory(member.id, inventory)

        await interaction.followup.send(
            f"Purchased **{self.lot['item_name']}** ×{qty} for **${total:,}**.",
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
            embed.add_field(name="Item", value=self.lot["item_name"], inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Total", value=f"${total:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class SellSetupView(discord.ui.View):
    def __init__(self, cog: "RipperdocHub", ctx: commands.Context,
                 groups: list[dict], *, mode: str = "sell"):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.groups = groups
        self.mode = mode
        self.selected_patient: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None

        self.truncated = len(groups) > 25
        options = []
        for i, g in enumerate(groups[:25]):
            qty_str = f" ×{g['count']}" if g["count"] > 1 else ""
            label = f"{g['name']}{qty_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))
        stock_select = discord.ui.Select(
            placeholder="Choose item from your stock…",
            options=options,
            row=1,
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
        await interaction.response.send_message(
            f"Patient: **{self.selected_patient.display_name}** ✓", ephemeral=True
        )

    async def _on_stock_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        item_name = self.groups[self.selected_group_idx]["name"]
        await interaction.response.send_message(
            f"Item: **{item_name}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=2)
    async def continue_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if self.selected_patient is None:
            await interaction.response.send_message(
                "Please select a patient first.", ephemeral=True
            )
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message(
                "Please select an item from your stock first.", ephemeral=True
            )
            return
        group = self.groups[self.selected_group_idx]
        if self.mode == "install":
            modal = InstallDetailsModal(
                self.cog, self.ctx, self.selected_patient,
                group, self.selected_group_idx + 1,
            )
        else:
            modal = SellDetailsModal(
                self.cog, self.ctx, self.selected_patient,
                group, self.selected_group_idx + 1,
            )
        await interaction.response.send_modal(modal)
        self.stop()


class SellDetailsModal(discord.ui.Modal, title="Sell — Finalize Details"):
    character_input = discord.ui.TextInput(label="Patient Character Name", placeholder="V")
    price_input = discord.ui.TextInput(
        label="Price to Charge (0 = gift)",
        placeholder="5000 (enter 0 to gift for free)",
    )

    def __init__(self, cog: "RipperdocHub", ctx: commands.Context,
                 patient: discord.Member, group: dict, inv_row: int):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.patient = patient
        self.group = group
        self.inv_row = inv_row

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            price = int(self.price_input.value)
        except ValueError:
            await interaction.followup.send("Price must be a number.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("Price cannot be negative.", ephemeral=True)
            return

        character_name = self.character_input.value.strip()
        if not character_name:
            await interaction.followup.send("Character name required.", ephemeral=True)
            return

        patient = self.patient
        item_name = self.group["name"]
        selected = self.group["items"][0]
        item_id = selected.get("item_id", str(uuid.uuid4()))

        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return

        confirm_view = DMConfirmView(timeout=60)
        try:
            dm_msg = await patient.send(
                f"**{self.ctx.author.display_name}** wants to sell you **{item_name}** "
                f"for **${price:,}** (character: **{character_name}**).\n"
                "Do you accept?",
                view=confirm_view,
            )
        except discord.Forbidden:
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
            await self.ctx.send(
                f"{self.ctx.author.mention} — {patient.display_name} declined or didn't respond to the sale of **{item_name}**."
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
            balance = await self.cog.unbelievaboat.get_balance(patient.id)
            if balance is None:
                await self.ctx.send(f"Could not fetch {patient.display_name}'s balance. Sale cancelled.")
                return
            p_cash = int(balance.get("cash", 0))
            p_bank = int(balance.get("bank", 0))
            if p_cash + p_bank < price:
                await self.ctx.send(
                    f"{patient.display_name} cannot afford ${price:,} (has ${p_cash + p_bank:,}). Sale cancelled."
                )
                return
            cash_ded = min(max(p_cash, 0), price)
            bank_ded = max(0, price - cash_ded)
            ok_debit = await self.cog.unbelievaboat.update_balance(
                patient.id,
                {"cash": -cash_ded, "bank": -bank_ded},
                reason=f"CW purchase: {item_name} from {self.ctx.author.display_name}",
            )
            if not ok_debit:
                await self.ctx.send(f"Payment failed for {patient.display_name}. Sale cancelled.")
                return
            ok_credit = await self.cog.unbelievaboat.update_balance(
                self.ctx.author.id,
                {"cash": price},
                reason=f"CW sale: {item_name} to {patient.display_name}",
            )
            if ok_credit:
                seller_credited = True
            else:
                logger.error("cw sell: buyer debited but seller credit failed — creating pending transfer")
                await pt_create({
                    "seller_id": str(self.ctx.author.id),
                    "buyer_id": str(patient.id),
                    "item_id": item_id,
                    "amount": price,
                    "reason": f"CW sell credit failed: {item_name}",
                })
                await self.ctx.send(
                    f"⚠️ Payment from {patient.display_name} succeeded but seller payout failed. "
                    "A pending transfer has been created — an admin will resolve it."
                )

        async with cw_cog.lock:
            inv = await cw_cog._load_inventory(self.ctx.author.id)
            inv_updated = [it for it in inv if it.get("item_id") != item_id]
            if len(inv_updated) == len(inv):
                if price > 0:
                    await self.cog.unbelievaboat.update_balance(
                        patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW sale refund — item missing"
                    )
                    if seller_credited:
                        await self.cog.unbelievaboat.update_balance(
                            self.ctx.author.id, {"cash": -price}, reason="CW sale refund — item missing"
                        )
                await self.ctx.send("Item no longer in stock. Refunded.")
                return
            await cw_cog._save_inventory(self.ctx.author.id, inv_updated)

        pi_ok = await pi_add_item({
            "item_id": item_id,
            "owner_id": str(patient.id),
            "character_name": character_name,
            "item_type": "cyberware",
            "name": item_name,
            "restriction": "basic",
            "description": "",
            "price_paid": price,
            "seller_id": str(self.ctx.author.id),
            "seller_name": self.ctx.author.display_name,
        })
        if not pi_ok:
            logger.error("ripperdoc sell: pi_add_item failed — attempting compensation")
            if price > 0:
                await self.cog.unbelievaboat.update_balance(
                    patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW sale refund — item grant failed"
                )
                if seller_credited:
                    await self.cog.unbelievaboat.update_balance(
                        self.ctx.author.id, {"cash": -price}, reason="CW sale refund — item grant failed"
                    )
            await self.ctx.send(
                f"⚠️ Failed to add **{item_name}** to {patient.display_name}'s inventory. "
                "Payment has been refunded. Please contact an admin."
            )
            return

        await ih_record_event(
            item_id, "cw_sold",
            actor_id=str(self.ctx.author.id),
            target_id=str(patient.id),
            price=price,
            metadata={"item_name": item_name, "character": character_name},
        )

        await self.ctx.send(
            f"Sold **{item_name}** to **{character_name}** ({patient.display_name}) "
            f"for **${price:,}**."
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="💉 Cyberware Sold",
                color=discord.Color.dark_teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Ripperdoc", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Patient", value=f"{patient.mention} — {character_name}", inline=False)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Price", value=f"${price:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class InstallDetailsModal(discord.ui.Modal, title="Install — Finalize Details"):
    character_input = discord.ui.TextInput(label="Patient Character Name", placeholder="V")
    price_input = discord.ui.TextInput(
        label="Install Fee (0 = free)",
        placeholder="0 (enter 0 for free install)",
        default="0",
    )

    def __init__(self, cog: "RipperdocHub", ctx: commands.Context,
                 patient: discord.Member, group: dict, inv_row: int):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.patient = patient
        self.group = group
        self.inv_row = inv_row

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            price = int(self.price_input.value)
        except ValueError:
            await interaction.followup.send("Price must be a number.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("Price cannot be negative.", ephemeral=True)
            return

        character_name = self.character_input.value.strip()
        if not character_name:
            await interaction.followup.send("Character name required.", ephemeral=True)
            return

        patient = self.patient
        item_name = self.group["name"]
        selected = self.group["items"][0]
        item_id = selected.get("item_id", str(uuid.uuid4()))

        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return

        price_text = f" for **${price:,}**" if price > 0 else " (free install)"
        confirm_view = DMConfirmView(timeout=60)
        try:
            dm_msg = await patient.send(
                f"**{self.ctx.author.display_name}** wants to install **{item_name}** on "
                f"**{character_name}**{price_text}.\n"
                "This will consume the cyberware. Do you accept?",
                view=confirm_view,
            )
        except discord.Forbidden:
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
            await self.ctx.send(
                f"{self.ctx.author.mention} — {patient.display_name} declined or didn't respond to the install of **{item_name}**."
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
            balance = await self.cog.unbelievaboat.get_balance(patient.id)
            if balance is None:
                await self.ctx.send(f"Could not fetch {patient.display_name}'s balance. Install cancelled.")
                return
            p_cash = int(balance.get("cash", 0))
            p_bank = int(balance.get("bank", 0))
            if p_cash + p_bank < price:
                await self.ctx.send(
                    f"{patient.display_name} cannot afford ${price:,} (has ${p_cash + p_bank:,}). Install cancelled."
                )
                return
            cash_ded = min(max(p_cash, 0), price)
            bank_ded = max(0, price - cash_ded)
            ok_debit = await self.cog.unbelievaboat.update_balance(
                patient.id,
                {"cash": -cash_ded, "bank": -bank_ded},
                reason=f"CW install: {item_name} by {self.ctx.author.display_name}",
            )
            if not ok_debit:
                await self.ctx.send(f"Payment failed for {patient.display_name}. Install cancelled.")
                return
            ok_credit = await self.cog.unbelievaboat.update_balance(
                self.ctx.author.id,
                {"cash": price},
                reason=f"CW install fee: {item_name} for {patient.display_name}",
            )
            if ok_credit:
                seller_credited = True
            else:
                logger.error("cw install: patient debited but ripperdoc credit failed — creating pending transfer")
                await pt_create({
                    "seller_id": str(self.ctx.author.id),
                    "buyer_id": str(patient.id),
                    "item_id": item_id,
                    "amount": price,
                    "reason": f"CW install credit failed: {item_name}",
                })
                await self.ctx.send(
                    f"⚠️ Payment from {patient.display_name} succeeded but ripperdoc payout failed. "
                    "A pending transfer has been created — an admin will resolve it."
                )

        async with cw_cog.lock:
            inv = await cw_cog._load_inventory(self.ctx.author.id)
            inv_updated = [it for it in inv if it.get("item_id") != item_id]
            if len(inv_updated) == len(inv):
                if price > 0:
                    await self.cog.unbelievaboat.update_balance(
                        patient.id, {"cash": cash_ded, "bank": bank_ded}, reason="CW install refund — item missing"
                    )
                    if seller_credited:
                        await self.cog.unbelievaboat.update_balance(
                            self.ctx.author.id, {"cash": -price}, reason="CW install refund — item missing"
                        )
                await self.ctx.send("Item no longer in stock. Refunded.")
                return
            await cw_cog._save_inventory(self.ctx.author.id, inv_updated)

        await ih_record_event(
            item_id, "cw_installed",
            actor_id=str(self.ctx.author.id),
            target_id=str(patient.id),
            price=price if price > 0 else None,
            metadata={"item_name": item_name, "character": character_name},
        )

        await interaction.followup.send(
            f"Installed **{item_name}** on **{character_name}** ({patient.display_name}).",
            ephemeral=True,
        )
        await self.ctx.send(
            f"💉 **{item_name}** installed on **{character_name}** ({patient.display_name}) "
            f"by {self.ctx.author.display_name}."
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="💉 Cyberware Installed",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Ripperdoc", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Patient", value=f"{patient.mention} — {character_name}", inline=False)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class DMConfirmView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.accepted: Optional[bool] = None

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

    @commands.command(name="ripperdoc")
    @is_ripperdoc()
    async def ripperdoc_hub(self, ctx: commands.Context):
        """Open the Ripperdoc interactive shop panel.

        Actions: Buy from wholesale, Sell to patient, Install, View stock.
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in the server.")
            return

        embed = discord.Embed(
            title="💉 Ripperdoc Shop",
            description=(
                "Welcome, Ripperdoc. Choose an action below.\n\n"
                "**Buy** — Purchase cyberware from this week's wholesale\n"
                "**Sell** — Sell cyberware to a patient (DM confirmation)\n"
                "**Install** — Install cyberware on a patient (consumes item)\n"
                "**My Stock** — View your current inventory\n"
                "**Wholesale List** — Browse this week's wholesale catalog"
            ),
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"Ripperdoc: {ctx.author.display_name}")

        view = RipperdocMenuView(self, ctx)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

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
            ts = str(entry.get("created_at", ""))[:19].replace("T", " ")
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
