"""Unified !gunstore hub command — interactive gun store interface.

Consolidates the separate guns_wh_* command set into a single interactive hub
with Discord dropdowns, buttons, and modals.
"""
import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.db import (
    pi_add_item,
    ih_record_event,
    pt_create,
)
from NightCityBot.utils.permissions import is_store_owner, is_fixer

logger = logging.getLogger(__name__)


class GunstoreMenuView(discord.ui.View):
    def __init__(self, cog: "GunstoreHub", ctx: commands.Context):
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
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        lots = [l for l in state.get("wholesale_lots", []) if int(l.get("qty_available", 0)) > 0]
        if not lots:
            await interaction.followup.send("No wholesale stock available.", ephemeral=True)
            return
        options = []
        for i, lot in enumerate(lots[:25]):
            restriction = lot.get("restriction", "basic")
            r_tag = f" [{restriction}]" if restriction != "basic" else ""
            label = f"{lot['gun_name']}{r_tag} — ${int(lot['unit_cost']):,} (×{lot['qty_available']})"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(i),
            ))
        view = GunBuySelect(self.cog, self.ctx, lots, guns_cog)
        await interaction.followup.send("Select a gun to buy:", view=view, ephemeral=True)

    @discord.ui.button(label="Sell to Customer", style=discord.ButtonStyle.success, emoji="🔫", row=0)
    async def sell_to_customer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GunSellModal(self.cog, self.ctx))

    @discord.ui.button(label="My Store Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=1)
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(self.ctx.guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(store_id)
        if not store or not store.get("lots"):
            await interaction.followup.send("Your store inventory is empty.", ephemeral=True)
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
            await interaction.followup.send("Your store inventory is empty.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"📦 {self.ctx.author.display_name}'s Gun Store",
            description="\n".join(lines[:30]),
            color=discord.Color.dark_gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Approve Buyer", style=discord.ButtonStyle.secondary, emoji="✅", row=1)
    async def approve_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApproveBuyerModal(self.cog, self.ctx, approve=True))

    @discord.ui.button(label="Unapprove Buyer", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def unapprove_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApproveBuyerModal(self.cog, self.ctx, approve=False))

    @discord.ui.button(label="Wholesale List", style=discord.ButtonStyle.secondary, emoji="📋", row=2)
    async def wholesale_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
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

    @discord.ui.button(label="Approved Buyers", style=discord.ButtonStyle.secondary, emoji="📝", row=2)
    async def approved_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(self.ctx.guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(store_id)
        approved = store.get("controlled_buyers", []) if store else []
        if not approved:
            await interaction.followup.send("Your controlled-buyer list is empty.", ephemeral=True)
            return
        lines = [f"<@{uid}>" for uid in approved[:25]]
        await interaction.followup.send(
            "**Approved Controlled Buyers:**\n" + "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GunBuySelect(discord.ui.View):
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
        await interaction.response.send_modal(GunBuyQtyModal(self.cog, self.ctx, lot, self.guns_cog))


class GunBuyQtyModal(discord.ui.Modal, title="Buy Gun from Wholesale"):
    qty_input = discord.ui.TextInput(label="Quantity", default="1", max_length=3)

    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, lot: dict, guns_cog):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.lot = lot
        self.guns_cog = guns_cog

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
            reason=f"Gun wholesale buy: {self.lot['gun_name']} x{qty}",
        )
        if not ok:
            await interaction.followup.send("Payment failed.", ephemeral=True)
            return

        async with self.guns_cog.lock:
            state = await self.guns_cog._load_state()
            lots = state.get("wholesale_lots", [])
            target_lot = None
            for l in lots:
                if l.get("lot_id") == self.lot.get("lot_id"):
                    target_lot = l
                    break
            if not target_lot or int(target_lot.get("qty_available", 0)) < qty:
                await self.cog.unbelievaboat.update_balance(
                    member.id,
                    {"cash": cash_deduct, "bank": bank_deduct},
                    reason="Gun wholesale refund — stock depleted",
                )
                await interaction.followup.send("Stock depleted. Refunded.", ephemeral=True)
                return
            target_lot["qty_available"] = int(target_lot["qty_available"]) - qty
            store_id = self.guns_cog._store_id(self.ctx.guild.id, member.id)
            store = state.setdefault("stores", {}).setdefault(
                store_id, {"owner_id": member.id, "lots": [], "controlled_buyers": []}
            )
            store_lot_id = f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            item_ids = [str(uuid.uuid4()) for _ in range(qty)]
            store["lots"].append({
                "lot_id": store_lot_id,
                "gun_name": self.lot["gun_name"],
                "gun_level": self.lot.get("gun_level", "L"),
                "weapon_type": self.lot.get("weapon_type", ""),
                "unit_cost": unit_cost,
                "qty_remaining": qty,
                "restriction": self.lot.get("restriction", "basic"),
                "item_ids": item_ids,
            })
            await self.guns_cog._save_state(state)

        for item_id in item_ids:
            await ih_record_event(
                item_id, "wholesale_buy",
                actor_id=str(member.id),
                price=unit_cost,
                metadata={
                    "gun_name": self.lot["gun_name"],
                    "gun_level": self.lot.get("gun_level"),
                    "lot_id": self.lot.get("lot_id"),
                    "store_lot_id": store_lot_id,
                },
            )

        await interaction.followup.send(
            f"Purchased **{self.lot['gun_name']}** ×{qty} for **${total:,}**.",
            ephemeral=True,
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🛒 Gun Wholesale Purchase",
                color=discord.Color.dark_gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store Owner", value=f"{member.mention}", inline=False)
            embed.add_field(name="Gun", value=self.lot["gun_name"], inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Total", value=f"${total:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class GunSellModal(discord.ui.Modal, title="Sell Gun to Customer"):
    customer_input = discord.ui.TextInput(label="Customer (mention or ID)", placeholder="@buyer or 123456789")
    character_input = discord.ui.TextInput(label="Customer Character Name", placeholder="V")
    lot_row_input = discord.ui.TextInput(label="Store Lot Row #", placeholder="1")
    price_input = discord.ui.TextInput(label="Sale Price", placeholder="5000")

    def __init__(self, cog: "GunstoreHub", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return

        customer = await self.cog._resolve_member(guild, self.customer_input.value)
        if not customer:
            await interaction.followup.send("Could not find that customer.", ephemeral=True)
            return

        try:
            lot_row = int(self.lot_row_input.value)
            price = int(self.price_input.value)
        except ValueError:
            await interaction.followup.send("Row and price must be numbers.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("Price cannot be negative.", ephemeral=True)
            return

        character_name = self.character_input.value.strip()
        if not character_name:
            await interaction.followup.send("Character name required.", ephemeral=True)
            return

        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return

        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
        store = state.get("stores", {}).get(store_id)
        if not store or not store.get("lots"):
            await interaction.followup.send("Your store is empty.", ephemeral=True)
            return

        available_lots = [l for l in store["lots"] if int(l.get("qty_remaining", 0)) > 0]
        if lot_row < 1 or lot_row > len(available_lots):
            await interaction.followup.send(
                f"Invalid row {lot_row}. You have {len(available_lots)} lot(s).", ephemeral=True
            )
            return

        lot = available_lots[lot_row - 1]
        gun_name = lot["gun_name"]
        restriction = lot.get("restriction", "basic")

        if restriction == "restricted":
            await interaction.followup.send(
                f"**{gun_name}** is **restricted**. Only a Fixer or admin can authorize this sale. "
                "Use `!admin_shop` to manage restricted items.",
                ephemeral=True,
            )
            return

        if restriction == "controlled":
            approved = store.get("controlled_buyers", [])
            if customer.id not in approved:
                approve_view = InlineApproveView(
                    self.cog, self.ctx, guns_cog, store_id, customer
                )
                await interaction.followup.send(
                    f"**{gun_name}** is controlled. {customer.display_name} is not on your approved list.\n"
                    "Would you like to approve them and proceed with the sale?",
                    view=approve_view,
                    ephemeral=True,
                )
                await approve_view.wait()
                if not approve_view.approved:
                    return

        confirm_view = GunDMConfirmView(timeout=60)
        try:
            dm_msg = await customer.send(
                f"**{self.ctx.author.display_name}** wants to sell you **{gun_name}** "
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
            await self.ctx.send(
                f"{self.ctx.author.mention} — {customer.display_name} declined or didn't respond to the purchase of **{gun_name}**."
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
            balance = await self.cog.unbelievaboat.get_balance(customer.id)
            if balance is None:
                await self.ctx.send(f"Could not fetch {customer.display_name}'s balance. Sale cancelled.")
                return
            c_cash = int(balance.get("cash", 0))
            c_bank = int(balance.get("bank", 0))
            if c_cash + c_bank < price:
                await self.ctx.send(
                    f"{customer.display_name} cannot afford ${price:,}. Sale cancelled."
                )
                return
            cash_ded = min(max(c_cash, 0), price)
            bank_ded = max(0, price - cash_ded)
            ok_debit = await self.cog.unbelievaboat.update_balance(
                customer.id,
                {"cash": -cash_ded, "bank": -bank_ded},
                reason=f"Gun purchase: {gun_name} from {self.ctx.author.display_name}",
            )
            if not ok_debit:
                await self.ctx.send(f"Payment failed for {customer.display_name}. Sale cancelled.")
                return
            ok_credit = await self.cog.unbelievaboat.update_balance(
                self.ctx.author.id,
                {"cash": price},
                reason=f"Gun sale: {gun_name} to {customer.display_name}",
            )
            if ok_credit:
                seller_credited = True
            else:
                logger.error("gun sell: buyer debited but seller credit failed — creating pending transfer")
                await pt_create({
                    "seller_id": str(self.ctx.author.id),
                    "buyer_id": str(customer.id),
                    "item_id": str(uuid.uuid4()),
                    "amount": price,
                    "reason": f"Gun sell credit failed: {gun_name}",
                })
                await self.ctx.send(
                    f"⚠️ Payment from {customer.display_name} succeeded but seller payout failed. "
                    "A pending transfer has been created — an admin will resolve it."
                )

        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store = state.get("stores", {}).get(store_id)
            if not store:
                if price > 0:
                    await self.cog.unbelievaboat.update_balance(
                        customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund"
                    )
                    if seller_credited:
                        await self.cog.unbelievaboat.update_balance(
                            self.ctx.author.id, {"cash": -price}, reason="Gun sale refund"
                        )
                await self.ctx.send("Store not found. Refunded.")
                return
            target_lot = None
            for l in store.get("lots", []):
                if l.get("lot_id") == lot.get("lot_id"):
                    target_lot = l
                    break
            if not target_lot or int(target_lot.get("qty_remaining", 0)) < 1:
                if price > 0:
                    await self.cog.unbelievaboat.update_balance(
                        customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — out of stock"
                    )
                    if seller_credited:
                        await self.cog.unbelievaboat.update_balance(
                            self.ctx.author.id, {"cash": -price}, reason="Gun sale refund — out of stock"
                        )
                await self.ctx.send("Item out of stock. Refunded.")
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
            "item_type": "gun",
            "name": gun_name,
            "restriction": restriction,
            "description": "",
            "price_paid": price,
            "seller_id": str(self.ctx.author.id),
            "seller_name": self.ctx.author.display_name,
        })
        if not pi_ok:
            logger.error("gunstore sell: pi_add_item failed — attempting compensation")
            if price > 0:
                await self.cog.unbelievaboat.update_balance(
                    customer.id, {"cash": cash_ded, "bank": bank_ded}, reason="Gun sale refund — item grant failed"
                )
                if seller_credited:
                    await self.cog.unbelievaboat.update_balance(
                        self.ctx.author.id, {"cash": -price}, reason="Gun sale refund — item grant failed"
                    )
            await self.ctx.send(
                f"⚠️ Failed to add **{gun_name}** to {customer.display_name}'s inventory. "
                "Payment has been refunded. Please contact an admin."
            )
            return

        await ih_record_event(
            item_id, "player_sale",
            actor_id=str(self.ctx.author.id),
            target_id=str(customer.id),
            price=price,
            metadata={"gun_name": gun_name, "character": character_name, "restriction": restriction},
        )

        await self.ctx.send(
            f"Sold **{gun_name}** to **{character_name}** ({customer.display_name}) for **${price:,}**."
        )
        log_ch = await self.cog._log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔫 Gun Sold",
                color=discord.Color.dark_gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Store Owner", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Customer", value=f"{customer.mention} — {character_name}", inline=False)
            embed.add_field(name="Gun", value=gun_name, inline=True)
            embed.add_field(name="Price", value=f"${price:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class InlineApproveView(discord.ui.View):
    def __init__(self, cog, ctx, guns_cog, store_id, customer):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.guns_cog = guns_cog
        self.store_id = store_id
        self.customer = customer
        self.approved = False

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


class ApproveBuyerModal(discord.ui.Modal):
    buyer_input = discord.ui.TextInput(label="Buyer (mention or ID)", placeholder="@buyer or 123456789")

    def __init__(self, cog: "GunstoreHub", ctx: commands.Context, approve: bool = True):
        super().__init__(title="Approve Buyer" if approve else "Unapprove Buyer")
        self.cog = cog
        self.ctx = ctx
        self.approve = approve

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return

        user = await self.cog._resolve_member(guild, self.buyer_input.value)
        if not user:
            await interaction.followup.send("Could not find that member.", ephemeral=True)
            return

        guns_cog = self.cog._guns_cog()
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return

        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.ctx.author.id)
            store = state.get("stores", {}).get(store_id)
            if not store:
                await interaction.followup.send("No store found. Buy stock first.", ephemeral=True)
                return
            approved = store.setdefault("controlled_buyers", [])
            if self.approve:
                if user.id in approved:
                    await interaction.followup.send(
                        f"{user.display_name} is already approved.", ephemeral=True
                    )
                    return
                approved.append(user.id)
            else:
                if user.id not in approved:
                    await interaction.followup.send(
                        f"{user.display_name} is not on your list.", ephemeral=True
                    )
                    return
                approved.remove(user.id)
            await guns_cog._save_state(state)

        action = "added to" if self.approve else "removed from"
        await interaction.followup.send(
            f"{user.display_name} {action} your controlled-buyer list.", ephemeral=True
        )


class GunDMConfirmView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.accepted: Optional[bool] = None

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

    async def _resolve_member(self, guild: discord.Guild, raw: str) -> Optional[discord.Member]:
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

    @commands.command(name="gunstore")
    @is_store_owner()
    async def gunstore_hub(self, ctx: commands.Context):
        """Open the Gun Store interactive panel.

        Actions: Buy from wholesale, Sell to customer, View inventory,
        Approve/Unapprove controlled buyers.
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in the server.")
            return

        embed = discord.Embed(
            title="🔫 Gun Store",
            description=(
                "Welcome, Store Owner. Choose an action below.\n\n"
                "**Buy** — Purchase guns from wholesale\n"
                "**Sell** — Sell a gun to a customer (DM confirmation)\n"
                "**Inventory** — View your store stock\n"
                "**Approve/Unapprove** — Manage controlled-buyer list\n"
                "**Wholesale List** — Browse available wholesale stock\n"
                "**Approved Buyers** — See your approved buyer list"
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(text=f"Store Owner: {ctx.author.display_name}")

        view = GunstoreMenuView(self, ctx)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg


async def setup(bot: commands.Bot):
    await bot.add_cog(GunstoreHub(bot))
