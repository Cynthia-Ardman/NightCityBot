"""Player hub — interactive !player panel for viewing inventory, trading, and giving items."""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.player_inventory import (
    query_player_inventory as pi_get_by_owner,
    get_player_item as pi_get_item,
    delete_player_item as pi_delete_item,
    transfer_player_item as pi_update_owner,
    insert_player_item as pi_add_item,
)
from NightCityBot.utils.db import ih_record_event, pt_create

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 15


def _resolve_id(raw: str) -> Optional[int]:
    raw = raw.strip()
    m = re.match(r"<@!?(\d+)>", raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


async def _resolve_member(guild: discord.Guild, raw: str) -> Optional[discord.Member]:
    uid = _resolve_id(raw)
    if uid is None:
        return None
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except Exception:
            return None
    return member


async def _log_channel(bot: commands.Bot, attr: str) -> Optional[discord.TextChannel]:
    ch_id = getattr(config, attr, 0)
    if not ch_id:
        return None
    ch = bot.get_channel(int(ch_id))
    if ch is None:
        try:
            ch = await bot.fetch_channel(int(ch_id))
        except Exception:
            return None
    return ch


async def _route_log_channel(bot: commands.Bot, item_type: str) -> Optional[discord.TextChannel]:
    if item_type == "gun":
        return await _log_channel(bot, "GUN_LOG_CHANNEL_ID")
    if item_type == "cyberware":
        return await _log_channel(bot, "CYBERWARE_LOG_CHANNEL_ID")
    return await _log_channel(bot, "GEAR_MISC_LOG_CHANNEL_ID")


class PlayerHubCog(commands.Cog, name="PlayerHub"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _inv_system_enabled(self) -> bool:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("player_inventory"):
            return False
        return True

    @commands.command(name="player")
    async def player_cmd(self, ctx: commands.Context):
        """Open the player hub — view inventory, trade items, give items."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        embed = discord.Embed(
            title="🎒 Player Hub",
            description="Manage your inventory, trade with other players, or give items.",
            color=discord.Color.blue(),
        )
        view = PlayerHubView(self, ctx)
        msg = await ctx.send(embed=embed, view=view, delete_after=120)
        view.message = msg
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @commands.command(name="helpplayer")
    async def helpplayer(self, ctx: commands.Context):
        """Quick reference for the player hub."""
        embed = discord.Embed(
            title="📘 Player Hub Help",
            description=(
                "`!player` — open the interactive player panel.\n\n"
                "From the panel you can:\n"
                "• **View Inventory** — see all your items grouped by character\n"
                "• **Trade Item** — sell an item to another player (with payment)\n"
                "• **Give Item** — transfer an item for free\n"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Use !helpme for general help")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayerHubCog(bot))


class PlayerHubView(discord.ui.View):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.primary, emoji="📦", row=0)
    async def view_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        display_lines, all_groups = inv_cog._build_display(items)
        item_lines = [(rn, ln) for rn, ln in display_lines if rn is not None]
        total_groups = len(item_lines)
        total_pages = max(1, (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
        page_rows = {rn for rn, _ in item_lines[:GROUPS_PER_PAGE]}
        page_lines: list[str] = []
        pending_header = None
        for rn, ln in display_lines:
            if rn is None:
                pending_header = ln
            else:
                if rn in page_rows:
                    if pending_header is not None:
                        page_lines.append(pending_header)
                        pending_header = None
                    page_lines.append(ln)
        embed = discord.Embed(
            title=f"📦 {interaction.user.display_name}'s Inventory (1/{total_pages})",
            description="\n".join(page_lines) if page_lines else "No items.",
            color=discord.Color.blue(),
        )
        hint = f"Use `!my_inventory 2` to see page 2." if total_pages > 1 else ""
        embed.set_footer(
            text=f"{len(items)} total item(s) | Row numbers are used for Trade and Give."
            + (f" | {hint}" if hint else "")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Trade Item", style=discord.ButtonStyle.success, emoji="💱", row=0)
    async def trade_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        view = TradeSetupView(self.cog, self.ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the buyer and the item to trade:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Give Item", style=discord.ButtonStyle.secondary, emoji="🎁", row=0)
    async def give_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        view = GiveSetupView(self.cog, self.ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the recipient and the item to give:",
            view=view,
            ephemeral=True,
        )


class TradeConfirmView(discord.ui.View):
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


class TradeSetupView(discord.ui.View):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_buyer: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None

        options = []
        for i, g in enumerate(all_groups[:25]):
            item = g["items"][0]
            item_type = item.get("item_type", "misc")
            char = item.get("character_name", "")
            count = g.get("count", 1)
            count_str = f" ×{count}" if count > 1 else ""
            char_str = f" ({char})" if char else ""
            label = f"{g['name']}{count_str} [{item_type}]{char_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))

        item_select = discord.ui.Select(
            placeholder="Choose an item to trade…",
            options=options,
            row=1,
        )
        item_select.callback = self._on_item_select
        self.add_item(item_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the buyer…", row=0)
    async def buyer_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await interaction.response.send_message("Please select a server member.", ephemeral=True)
            return
        if isinstance(user, discord.Member):
            self.selected_buyer = user
        else:
            guild = self.ctx.guild
            if guild:
                member = guild.get_member(user.id)
                if member:
                    self.selected_buyer = member
                else:
                    await interaction.response.send_message(
                        "That user doesn't appear to be in this server.", ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message("Could not resolve server member.", ephemeral=True)
                return
        await interaction.response.send_message(
            f"Buyer: **{self.selected_buyer.display_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        await interaction.response.send_message(
            f"Item: **{g['name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=2)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_buyer is None:
            await interaction.response.send_message("Please select a buyer first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select an item first.", ephemeral=True)
            return
        if self.selected_buyer.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot trade items to yourself.", ephemeral=True
            )
            return
        group = self.all_groups[self.selected_group_idx]
        selected_item = group["items"][0]
        restriction = selected_item.get("restriction", "basic")
        if restriction in ("controlled", "restricted"):
            await interaction.response.send_message(
                f"❌ **{group['name']}** is **{restriction}** — "
                "trading controlled/restricted guns is not allowed. "
                "Contact a Fixer for assistance.",
                ephemeral=True,
            )
            return
        modal = TradeDetailsModal(self.cog, self.selected_buyer, group)
        await interaction.response.send_modal(modal)
        self.stop()


class TradeDetailsModal(discord.ui.Modal, title="Trade — Finalize Details"):
    price_input = discord.ui.TextInput(
        label="Price ($)",
        placeholder="e.g. 5000 (0 for free)",
    )
    buyer_char_input = discord.ui.TextInput(
        label="Buyer's Character Name",
        placeholder="Character receiving the item",
    )

    def __init__(self, cog: PlayerHubCog, buyer: discord.Member, group: dict):
        super().__init__()
        self.cog = cog
        self.buyer = buyer
        self.group = group

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return

        buyer = self.buyer
        try:
            price = int(self.price_input.value.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Price must be a number.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("❌ Price cannot be negative.", ephemeral=True)
            return
        if buyer.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You cannot trade items to yourself.",
                ephemeral=True,
            )
            return

        buyer_character = self.buyer_char_input.value.strip().strip('"').strip("'")
        if not buyer_character:
            await interaction.followup.send("❌ Buyer character name is required.", ephemeral=True)
            return

        selected_item = self.group["items"][0]
        item_name = selected_item["name"]
        item_id = selected_item["item_id"]
        item_type = selected_item.get("item_type", "misc")
        restriction = selected_item.get("restriction", "basic")

        live_item = await pi_get_item(item_id)
        if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ **{item_name}** is no longer in your inventory. "
                "Please check View Inventory and try again.",
                ephemeral=True,
            )
            return

        if restriction in ("controlled", "restricted"):
            await interaction.followup.send(
                f"❌ **{item_name}** is **{restriction}** — "
                "player-to-player trading of controlled/restricted items is not allowed. "
                "Contact a Fixer for assistance.",
                ephemeral=True,
            )
            return

        inv_cog = self.cog.bot.cogs.get("PlayerInventory")

        if buyer.id != interaction.user.id:
            price_str = f"**${price:,}**" if price > 0 else "**free**"
            confirm_view = TradeConfirmView(timeout=60)
            try:
                dm_msg = await buyer.send(
                    f"**{interaction.user.display_name}** wants to trade you **{item_name}** "
                    f"for {price_str} (character: **{buyer_character}**).\n"
                    "Do you accept?",
                    view=confirm_view,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Cannot DM {buyer.display_name}. They may have DMs disabled.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                f"📩 Confirmation sent to {buyer.display_name} via DM. Waiting…",
                ephemeral=True,
            )
            await confirm_view.wait()

            if not confirm_view.accepted:
                try:
                    await dm_msg.edit(content="Trade declined or timed out.", view=None)
                except Exception:
                    pass
                await interaction.followup.send(
                    f"❌ {buyer.display_name} declined or didn't respond to the trade.",
                    ephemeral=True,
                )
                return
            try:
                await dm_msg.edit(view=None)
            except Exception:
                pass

        b_cash_deduct = 0
        b_bank_deduct = 0

        if price > 0 and buyer.id != interaction.user.id:
            ub = getattr(inv_cog, "unbelievaboat", None) if inv_cog else None
            if not ub:
                await interaction.followup.send("❌ Economy system unavailable.", ephemeral=True)
                return
            buyer_balance = await ub.get_balance(buyer.id)
            if buyer_balance is None:
                await interaction.followup.send("❌ Could not fetch buyer's balance.", ephemeral=True)
                return

            b_cash = int(buyer_balance.get("cash", 0))
            b_bank = int(buyer_balance.get("bank", 0))
            if b_cash + b_bank < price:
                await interaction.followup.send(
                    f"❌ {buyer.display_name} cannot afford **${price:,}** "
                    f"(they have **${b_cash + b_bank:,}**).",
                    ephemeral=True,
                )
                return

            b_cash_deduct = min(max(b_cash, 0), price)
            b_bank_deduct = max(0, price - b_cash_deduct)

            ok_buyer = await ub.update_balance(
                buyer.id,
                {"cash": -b_cash_deduct, "bank": -b_bank_deduct},
                reason=f"Trade purchase: {item_name} from {interaction.user.display_name}",
            )
            if not ok_buyer:
                await interaction.followup.send("❌ Failed to deduct from buyer's balance. Aborting.", ephemeral=True)
                return

            ok_seller = await ub.update_balance(
                interaction.user.id,
                {"cash": price},
                reason=f"Trade sale: {item_name} to {buyer.display_name}",
            )
            if not ok_seller:
                logger.error(
                    "player_hub trade: buyer debited but seller credit failed — seller=%s buyer=%s item=%s",
                    interaction.user.id, buyer.id, item_id,
                )
                pt_id = str(uuid.uuid4())
                await pt_create({
                    "transfer_id": pt_id,
                    "seller_id": str(interaction.user.id),
                    "buyer_id": str(buyer.id),
                    "item_id": item_id,
                    "amount": price,
                })
                alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
                if alert_ch:
                    await alert_ch.send(
                        f"🚨 **PENDING TRADE** — seller credit failed!\n"
                        f"Transfer ID: `{pt_id}`\n"
                        f"Seller: {interaction.user.mention} | Buyer: {buyer.mention}\n"
                        f"Item: **{item_name}** | Amount: **${price:,}**\n"
                        "Buyer has been debited. Please resolve manually.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                await interaction.followup.send(
                    "⚠️ Buyer was charged but seller payout failed. "
                    "This has been flagged for admin review. "
                    "Item ownership has NOT been transferred yet.",
                    ephemeral=True,
                )
                return

        ok_transfer = await pi_update_owner(
            item_id, str(buyer.id), buyer_character, str(interaction.user.id)
        )
        if not ok_transfer:
            if price > 0 and buyer.id != interaction.user.id:
                ub = getattr(inv_cog, "unbelievaboat", None) if inv_cog else None
                pt_id = str(uuid.uuid4())
                await pt_create({
                    "transfer_id": pt_id,
                    "seller_id": str(interaction.user.id),
                    "buyer_id": str(buyer.id),
                    "item_id": item_id,
                    "amount": price,
                })
                logger.error(
                    "player_hub trade: ownership write failed after payment — pt=%s seller=%s buyer=%s item=%s",
                    pt_id, interaction.user.id, buyer.id, item_id,
                )
                alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
                if alert_ch:
                    await alert_ch.send(
                        f"🚨 **PENDING TRADE — ownership write failed**\n"
                        f"Transfer ID: `{pt_id}`\n"
                        f"Seller: {interaction.user.mention} | Buyer: {buyer.mention}\n"
                        f"Item: **{item_name}** | Amount: **${price:,}**\n"
                        "Buyer debited; seller credited. Ownership NOT transferred. Resolve manually.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                if ub:
                    await ub.update_balance(
                        buyer.id,
                        {"cash": b_cash_deduct, "bank": b_bank_deduct},
                        reason=f"Trade refund (DB failure): {item_name}",
                    )
                    await ub.update_balance(
                        interaction.user.id,
                        {"cash": -price},
                        reason=f"Trade refund (DB failure): {item_name}",
                    )
                await interaction.followup.send(
                    f"⚠️ Ownership write failed (Transfer ID `{pt_id}`). "
                    "Refunds have been attempted and this has been flagged for admin review.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ Failed to transfer item ownership. Please try again.",
                    ephemeral=True,
                )
            return

        log_ch = await _route_log_channel(self.cog.bot, item_type)
        if log_ch:
            seller_char = selected_item.get("character_name") or "—"
            embed = discord.Embed(
                title="💱 Item Traded",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Seller",
                value=f"{interaction.user.mention} ({interaction.user.display_name}) — {seller_char}",
                inline=False,
            )
            embed.add_field(
                name="Buyer",
                value=f"{buyer.mention} ({buyer.display_name}) — {buyer_character}",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Type", value=f"{item_type}/{restriction}", inline=True)
            embed.add_field(name="Price", value=f"${price:,}" if price else "Free", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ih_record_event(
            item_id, "traded",
            actor_id=str(interaction.user.id),
            target_id=str(buyer.id),
            price=price,
            metadata={
                "item_name": item_name,
                "item_type": item_type,
                "buyer_character": buyer_character,
                "seller_character": selected_item.get("character_name", ""),
            },
        )

        price_str = f"for **${price:,}**" if price else "for free"
        await interaction.followup.send(
            f"✅ Traded **{item_name}** to **{buyer_character}** ({buyer.display_name}) {price_str}.",
            ephemeral=True,
        )


class GiveSetupView(discord.ui.View):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_recipient: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None

        options = []
        for i, g in enumerate(all_groups[:25]):
            item = g["items"][0]
            item_type = item.get("item_type", "misc")
            char = item.get("character_name", "")
            count = g.get("count", 1)
            count_str = f" ×{count}" if count > 1 else ""
            char_str = f" ({char})" if char else ""
            label = f"{g['name']}{count_str} [{item_type}]{char_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))

        item_select = discord.ui.Select(
            placeholder="Choose an item to give…",
            options=options,
            row=1,
        )
        item_select.callback = self._on_item_select
        self.add_item(item_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the recipient…", row=0)
    async def recipient_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await interaction.response.send_message("Please select a server member.", ephemeral=True)
            return
        if isinstance(user, discord.Member):
            self.selected_recipient = user
        else:
            guild = self.ctx.guild
            if guild:
                member = guild.get_member(user.id)
                if member:
                    self.selected_recipient = member
                else:
                    await interaction.response.send_message(
                        "That user doesn't appear to be in this server.", ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message("Could not resolve server member.", ephemeral=True)
                return
        await interaction.response.send_message(
            f"Recipient: **{self.selected_recipient.display_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        await interaction.response.send_message(
            f"Item: **{g['name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=2)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_recipient is None:
            await interaction.response.send_message("Please select a recipient first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select an item first.", ephemeral=True)
            return
        group = self.all_groups[self.selected_group_idx]
        modal = GiveDetailsModal(self.cog, self.selected_recipient, group)
        await interaction.response.send_modal(modal)
        self.stop()


class GiveDetailsModal(discord.ui.Modal, title="Give — Finalize Details"):
    sender_char_input = discord.ui.TextInput(
        label="Your Character Name",
        placeholder="Character giving the item",
    )
    receiver_char_input = discord.ui.TextInput(
        label="Recipient's Character Name",
        placeholder="Leave blank if giving to a Ripperdoc",
        required=False,
    )

    def __init__(self, cog: PlayerHubCog, recipient: discord.Member, group: dict):
        super().__init__()
        self.cog = cog
        self.recipient = recipient
        self.group = group

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return

        target = self.recipient
        sender_char = self.sender_char_input.value.strip().strip('"').strip("'")
        if not sender_char:
            await interaction.followup.send("❌ Your character name is required.", ephemeral=True)
            return

        selected_item = self.group["items"][0]
        item_name = selected_item["name"]
        item_id = selected_item["item_id"]
        item_type = selected_item.get("item_type", "misc")
        item_char = selected_item.get("character_name", "")

        if item_char and item_char.lower() != sender_char.lower():
            await interaction.followup.send(
                f"❌ **{item_name}** belongs to character **{item_char}**, "
                f"not **{sender_char}**. Check your character name.",
                ephemeral=True,
            )
            return

        target_roles = getattr(target, "roles", [])
        is_ripperdoc_target = any(
            getattr(r, "id", None) == getattr(config, "RIPPERDOC_ROLE_ID", None)
            for r in target_roles
        )

        if item_type == "cyberware" and is_ripperdoc_target:
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if cw_cog is None:
                await interaction.followup.send("❌ CyberwareShop cog unavailable. Contact an admin.", ephemeral=True)
                return

            ok_del = await pi_delete_item(item_id)
            if not ok_del:
                await interaction.followup.send("❌ Failed to remove item from your inventory.", ephemeral=True)
                return

            rd_inventory = await cw_cog._load_inventory(target.id)
            rd_inventory.append({
                "item_id": item_id,
                "name": item_name,
                "price_paid": selected_item.get("price_paid"),
                "purchased_at": (
                    selected_item.get("acquired_at")
                    or selected_item.get("created_at")
                    or datetime.now(timezone.utc).isoformat()
                ),
            })
            ok_save = await cw_cog._save_inventory(target.id, rd_inventory)
            if not ok_save:
                logger.error(
                    "player_hub give: _save_inventory failed for ripperdoc=%s item=%s — attempting restore",
                    target.id, item_id,
                )
                await pi_add_item({
                    "item_id": item_id,
                    "owner_id": str(interaction.user.id),
                    "character_name": selected_item.get("character_name", ""),
                    "item_type": item_type,
                    "name": item_name,
                    "restriction": selected_item.get("restriction", "basic"),
                    "description": selected_item.get("description", ""),
                    "price_paid": selected_item.get("price_paid"),
                    "seller_id": selected_item.get("seller_id"),
                    "seller_name": selected_item.get("seller_name", ""),
                    "acquired_at": selected_item.get("acquired_at"),
                })
                await interaction.followup.send(
                    "❌ Failed to add item to ripperdoc stock. Your item has been restored.",
                    ephemeral=True,
                )
                return

            log_ch = await _log_channel(self.cog.bot, "CYBERWARE_LOG_CHANNEL_ID")
            if log_ch:
                embed = discord.Embed(
                    title="💉 Cyberware Returned to Ripperdoc Stock",
                    color=discord.Color.teal(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="From", value=f"{interaction.user.mention} ({interaction.user.display_name}) — {sender_char}", inline=False)
                embed.add_field(name="Ripperdoc", value=f"{target.mention} ({target.display_name})", inline=False)
                embed.add_field(name="Item", value=item_name, inline=True)
                embed.set_footer(text="NightCityBot Audit Log")
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

            await ih_record_event(
                item_id, "given",
                actor_id=str(interaction.user.id),
                target_id=str(target.id),
                metadata={
                    "item_name": item_name,
                    "item_type": "cyberware",
                    "sender_character": sender_char,
                    "routed_to": "ripperdoc_stock",
                },
            )
            await interaction.followup.send(
                f"✅ **{item_name}** transferred from **{sender_char}** to "
                f"{target.display_name}'s ripperdoc stock.",
                ephemeral=True,
            )
            return

        receiver_char = (self.receiver_char_input.value or "").strip().strip('"').strip("'")
        if not receiver_char:
            await interaction.followup.send(
                "❌ Recipient's character name is required for player-to-player gives.",
                ephemeral=True,
            )
            return

        ok = await pi_update_owner(item_id, str(target.id), receiver_char, str(interaction.user.id))
        if not ok:
            await interaction.followup.send("❌ Failed to transfer item. Please try again.", ephemeral=True)
            return

        log_ch = await _route_log_channel(self.cog.bot, item_type)
        if log_ch:
            embed = discord.Embed(
                title="🎁 Item Given",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="From",
                value=f"{interaction.user.mention} ({interaction.user.display_name}) — {sender_char}",
                inline=False,
            )
            embed.add_field(
                name="To",
                value=f"{target.mention} ({target.display_name}) — {receiver_char}",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Type", value=item_type, inline=True)
            price_paid = selected_item.get("price_paid")
            if price_paid is not None:
                embed.add_field(name="Price Paid", value=f"${price_paid:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ih_record_event(
            item_id, "given",
            actor_id=str(interaction.user.id),
            target_id=str(target.id),
            metadata={
                "item_name": item_name,
                "item_type": item_type,
                "sender_character": sender_char,
                "receiver_character": receiver_char,
            },
        )
        await interaction.followup.send(
            f"✅ Transferred **{item_name}** from **{sender_char}** to "
            f"**{receiver_char}** ({target.display_name}).",
            ephemeral=True,
        )
