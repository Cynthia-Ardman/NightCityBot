"""Unified !admin_shop panel — admin operations for the shop system.

Provides a single interactive panel for Fixers/admins to manage inventory,
look up item history, add/remove items, and view audit trails.
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView, SafeModal
from NightCityBot.utils.db import (
    pi_add_item,
    pi_get_item,
    pi_get_by_owner,
    pi_delete_item,
    pi_update_owner,
    pi_update_character,
    ih_record_event,
    ih_get_history,
    wh_lots_get_all,
    wh_lots_replace_all,
    cw_catalog_get_all,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active, get_character_by_name
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.inline_helpers import collect_text_input, QtySelectView

logger = logging.getLogger(__name__)


async def _resolve_user_select(ctx, user) -> Optional[discord.Member]:
    if isinstance(user, discord.Member):
        return user
    guild = ctx.guild
    if guild and user:
        member = guild.get_member(user.id)
        if member:
            return member
        try:
            return await guild.fetch_member(user.id)
        except Exception:
            pass
    return None


class AdminShopMenuView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
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

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = AdminAddItemPickerView(self.cog, self.ctx)
        await interaction.followup.send("**Step 1** — Select the player to add an item to:", view=view, ephemeral=True)

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AdminRemoveItemModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Reassign Item", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def reassign_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AdminReassignModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=1)
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ItemHistoryModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Player Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=1)
    async def player_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerInvPickerView(self.cog, self.ctx)
        await interaction.followup.send("Select a player to view their inventory:", view=view, ephemeral=True)

    @discord.ui.button(label="Wholesale Stock", style=discord.ButtonStyle.secondary, emoji="🏭", row=2)
    async def wholesale_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        lines = []
        if guns_cog:
            state = await guns_cog._load_state()
            gun_lots = state.get("wholesale_lots", [])
            available = [l for l in gun_lots if int(l.get("qty_available", 0)) > 0]
            if available:
                lines.append("**🔫 Gun Wholesale:**")
                for i, lot in enumerate(available[:15], 1):
                    r = lot.get("restriction", "basic")
                    r_tag = f" [{r}]" if r != "basic" else ""
                    lines.append(
                        f"`{i}.` **{lot['gun_name']}**{r_tag} — ${int(lot['unit_cost']):,} × {lot['qty_available']}"
                    )
            else:
                lines.append("**🔫 Gun Wholesale:** Empty")
        if cw_cog:
            state = await cw_cog._load_state()
            cw_lots = state.get("cw_wholesale_lots", [])
            available = [l for l in cw_lots if int(l.get("qty_available", 0)) > 0]
            if available:
                lines.append("\n**💉 CW Wholesale:**")
                for i, lot in enumerate(available[:15], 1):
                    lines.append(
                        f"`{i}.` **{lot['item_name']}** — ${int(lot['unit_cost']):,} × {lot['qty_available']}"
                    )
            else:
                lines.append("**💉 CW Wholesale:** Empty")
        if not lines:
            await interaction.followup.send("No wholesale systems available.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🏭 Wholesale Stock Overview",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Restock Wholesale", style=discord.ButtonStyle.primary, emoji="📥", row=2)
    async def restock_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WholesaleRestockModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Clear Gun WH", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def clear_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        confirm_view = WholesaleClearConfirmView(self.cog, self.ctx, target="guns")
        await interaction.followup.send(
            "⚠️ This will clear **all** gun wholesale lots. Are you sure?",
            view=confirm_view,
            ephemeral=True,
        )

    @discord.ui.button(label="Restock CW", style=discord.ButtonStyle.primary, emoji="💉", row=3)
    async def restock_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CWWholesaleRestockModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Clear CW WH", style=discord.ButtonStyle.danger, emoji="🧹", row=3)
    async def clear_cw_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        confirm_view = WholesaleClearConfirmView(self.cog, self.ctx, target="cw")
        await interaction.followup.send(
            "⚠️ This will clear **all** cyberware wholesale lots. Are you sure?",
            view=confirm_view,
            ephemeral=True,
        )


class AdminAddItemPickerView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.selected_player: Optional[discord.Member] = None
        self.selected_character: Optional[dict] = None
        self._character_select: Optional[discord.ui.Select] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        self.selected_player = member
        self.selected_character = None
        characters = await get_active_characters(str(member.id))
        if not characters:
            await interaction.response.send_message(
                f"❌ {member.display_name} has no active characters. "
                "They must create a character before receiving items.",
                ephemeral=True,
            )
            self.selected_player = None
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
            f"Player: **{member.display_name}** ✓ — Now select their character.",
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

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=2)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_player is None:
            await interaction.response.send_message("Please select a player first.", ephemeral=True)
            return
        if self.selected_character is None:
            await interaction.response.send_message("Please select a character.", ephemeral=True)
            return
        if not await ensure_character_active(self.selected_character["character_id"]):
            await interaction.response.send_message(
                f"❌ Character **{self.selected_character['name']}** is no longer active.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(AdminAddItemDetailsModal(self.cog, self.ctx, self.selected_player, self.selected_character))
        self.stop()


class AdminAddItemDetailsModal(SafeModal, title="Add Item — Details"):
    name_input = discord.ui.TextInput(label="Item Name")
    item_type_input = discord.ui.TextInput(label="Type (gun/cyberware/gear/misc)", default="misc")
    qty_price_input = discord.ui.TextInput(
        label="Qty,Price (e.g. 1,5000 or just 1)",
        default="1",
        required=False,
    )

    def __init__(self, cog: "AdminShopCog", ctx: commands.Context, player: discord.Member, character: dict):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.player = player
        self.character = character

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        char_name = self.character.get("name", "")
        character_id = self.character.get("character_id")
        if not char_name:
            await interaction.followup.send("Character selection required.", ephemeral=True)
            return
        if character_id and not await ensure_character_active(character_id):
            await interaction.followup.send(
                f"❌ Character **{char_name}** is no longer active.", ephemeral=True
            )
            return

        name = self.name_input.value.strip()
        if not name:
            await interaction.followup.send("❌ Item name is required.", ephemeral=True)
            return
        item_type = self.item_type_input.value.strip().lower() or "misc"

        qty = 1
        price = None
        qp = self.qty_price_input.value.strip()
        if qp:
            parts = [p.strip() for p in qp.split(",")]
            try:
                qty = int(parts[0])
            except (ValueError, IndexError):
                qty = 1
            if len(parts) > 1:
                try:
                    price = int(parts[1])
                except ValueError:
                    pass
        if qty < 1:
            qty = 1

        added = 0
        now = datetime.now(timezone.utc).isoformat()
        for _ in range(qty):
            item_id = str(uuid.uuid4())
            ok = await pi_add_item({
                "item_id": item_id,
                "owner_id": str(self.player.id),
                "character_name": char_name,
                "character_id": character_id,
                "item_type": item_type,
                "name": name,
                "restriction": "basic",
                "description": "",
                "price_paid": price,
                "seller_id": str(interaction.user.id),
                "seller_name": interaction.user.display_name,
                "acquired_at": now,
            })
            if ok:
                added += 1
                await ih_record_event(
                    item_id, "admin_add",
                    actor_id=str(interaction.user.id),
                    target_id=str(self.player.id),
                    price=price,
                    metadata={"item_name": name, "character_name": char_name}
                )

        await interaction.followup.send(
            f"✅ Added **{name}** (x{added}) to **{char_name}**.",
            ephemeral=True
        )

        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="➕ Admin: Item Added",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
            embed.add_field(name="Player", value=self.player.mention, inline=True)
            embed.add_field(name="Character", value=char_name, inline=True)
            embed.add_field(name="Item", value=f"{name} ({item_type})", inline=True)
            embed.add_field(name="Qty", value=str(added), inline=True)
            if price is not None:
                embed.add_field(name="Price Paid", value=f"${price:,}", inline=True)
            embed.set_footer(text=f"Item Type: {item_type}")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class AdminRemoveItemModal(SafeModal, title="Remove Item"):
    player_input = discord.ui.TextInput(label="Player (mention or ID)")
    item_id_input = discord.ui.TextInput(label="Item UUID")


    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return

        player = await self.cog._resolve_member(guild, self.player_input.value)
        if not player:
            await interaction.followup.send("Could not find that player.", ephemeral=True)
            return

        item_id = self.item_id_input.value.strip()
        item = await pi_get_item(item_id)
        if item is None:
            await interaction.followup.send(f"Item `{item_id}` not found.", ephemeral=True)
            return
        if item.get("owner_id") != str(player.id):
            await interaction.followup.send(
                f"Item does not belong to {player.display_name}.", ephemeral=True
            )
            return

        item_name = item.get("name", "?")
        ok = await pi_delete_item(item_id)
        if not ok:
            await interaction.followup.send("Failed to remove item.", ephemeral=True)
            return

        await ih_record_event(
            item_id, "admin_remove",
            actor_id=str(self.ctx.author.id),
            target_id=str(player.id),
            metadata={"item_name": item_name},
        )

        await interaction.followup.send(
            f"Removed **{item_name}** (`{item_id}`) from {player.display_name}.", ephemeral=True
        )
        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Admin: Item Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Player", value=f"{player.mention}", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class PlayerInvPickerView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a playeru2026", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await interaction.followup.send(f"{member.display_name} has no items.", ephemeral=True)
            return
        lines = []
        for i, item in enumerate(items[:30], 1):
            itype = item.get("item_type", "misc")
            name = item.get("name", "?")
            char = item.get("character_name", "u2014")
            iid = item.get("item_id", "?")[:8]
            lines.append(f"`{i}.` **{name}** [{itype}] u2014 {char} (`{iid}...`)")
        embed = discord.Embed(
            title=f"ud83dudce6 {member.display_name}'s Inventory",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(items)} item(s) total")
        await interaction.followup.send(embed=embed, ephemeral=True)


class AdminReassignModal(SafeModal, title="Reassign Item"):
    item_id_input = discord.ui.TextInput(label="Item UUID")
    player_input = discord.ui.TextInput(label="New Owner (mention or ID)")
    character_input = discord.ui.TextInput(label="New Character Name")

    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return

        item_id = self.item_id_input.value.strip()
        item = await pi_get_item(item_id)
        if item is None:
            await interaction.followup.send(f"Item `{item_id}` not found.", ephemeral=True)
            return

        new_owner = await self.cog._resolve_member(guild, self.player_input.value)
        if not new_owner:
            await interaction.followup.send("Could not find new owner.", ephemeral=True)
            return

        new_char_name = self.character_input.value.strip()
        char_record = await get_character_by_name(str(new_owner.id), new_char_name)
        if char_record and not await ensure_character_active(char_record["character_id"]):
            await interaction.followup.send(
                f"❌ Character **{new_char_name}** is not active.", ephemeral=True
            )
            return
        new_char = new_char_name
        item_name = item.get("name", "?")
        old_owner_id = item.get("owner_id", "")
        old_char = item.get("character_name", "")

        if str(new_owner.id) == old_owner_id:
            ok = await pi_update_character(item_id, new_char, expected_owner_id=old_owner_id)
        else:
            ok = await pi_update_owner(item_id, str(new_owner.id), new_char, old_owner_id)

        if not ok:
            await interaction.followup.send("Failed to reassign item.", ephemeral=True)
            return

        await ih_record_event(
            item_id, "admin_reassign",
            actor_id=str(self.ctx.author.id),
            target_id=str(new_owner.id),
            metadata={
                "item_name": item_name,
                "old_owner": old_owner_id,
                "old_character": old_char,
                "new_character": new_char,
            },
        )

        await interaction.followup.send(
            f"Reassigned **{item_name}** to {new_owner.display_name} — {new_char}.",
            ephemeral=True,
        )
        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="✏️ Admin: Item Reassigned",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
            embed.add_field(name="Old", value=f"<@{old_owner_id}> — {old_char}", inline=True)
            embed.add_field(name="New", value=f"{new_owner.mention} — {new_char}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class ItemHistoryModal(SafeModal, title="Item History Lookup"):
    item_id_input = discord.ui.TextInput(label="Item UUID")

    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        item_id = self.item_id_input.value.strip()
        history = await ih_get_history(item_id, limit=50)
        if not history:
            await interaction.followup.send(f"No history for `{item_id}`.", ephemeral=True)
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
            title=f"📜 Item History — `{item_id[:12]}...`",
            description="\n".join(lines[:25]),
            color=discord.Color.greyple(),
        )
        embed.set_footer(text=f"{len(history)} event(s)")
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class WholesaleRestockModal(SafeModal, title="Restock Gun Wholesale"):
    gun_name_input = discord.ui.TextInput(label="Gun Name")
    qty_input = discord.ui.TextInput(label="Quantity", default="10")
    cost_input = discord.ui.TextInput(label="Unit Cost", placeholder="5000")
    restriction_input = discord.ui.TextInput(
        label="Restriction (basic/controlled/restricted)",
        default="basic",
        required=False,
    )

    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return

        try:
            qty = int(self.qty_input.value)
            cost = int(self.cost_input.value)
        except ValueError:
            await interaction.followup.send("Quantity and cost must be numbers.", ephemeral=True)
            return
        if qty < 1 or cost < 0:
            await interaction.followup.send("Invalid quantity or cost.", ephemeral=True)
            return

        gun_name = self.gun_name_input.value.strip()
        restriction = (self.restriction_input.value.strip().lower() or "basic")

        async with guns_cog.lock:
            state = await guns_cog._load_state()
            lots = state.setdefault("wholesale_lots", [])
            lot_id = f"admin-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            lots.append({
                "lot_id": lot_id,
                "gun_name": gun_name,
                "gun_level": "L",
                "weapon_type": "",
                "unit_cost": cost,
                "qty_available": qty,
                "restriction": restriction,
            })
            await guns_cog._save_state(state)

        await interaction.followup.send(
            f"Restocked **{gun_name}** ×{qty} at ${cost:,} [{restriction}].", ephemeral=True
        )
        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="📥 Admin: Wholesale Restocked",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Gun", value=gun_name, inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
            embed.add_field(name="Restriction", value=restriction, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class CWWholesaleRestockModal(SafeModal, title="Restock CW Wholesale"):
    item_name_input = discord.ui.TextInput(label="Cyberware Name")
    qty_input = discord.ui.TextInput(label="Quantity", default="10")
    cost_input = discord.ui.TextInput(label="Unit Cost", placeholder="5000")

    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return

        try:
            qty = int(self.qty_input.value)
            cost = int(self.cost_input.value)
        except ValueError:
            await interaction.followup.send("Quantity and cost must be numbers.", ephemeral=True)
            return
        if qty < 1 or cost < 0:
            await interaction.followup.send("Invalid quantity or cost.", ephemeral=True)
            return

        item_name = self.item_name_input.value.strip()

        async with cw_cog.lock:
            state = await cw_cog._load_state()
            lots = state.setdefault("cw_wholesale_lots", [])
            lot_id = f"admin-cw-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            lots.append({
                "lot_id": lot_id,
                "item_name": item_name,
                "unit_cost": cost,
                "qty_available": qty,
            })
            await cw_cog._save_state(state)

        await interaction.followup.send(
            f"Restocked CW **{item_name}** ×{qty} at ${cost:,}.", ephemeral=True
        )
        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="📥 Admin: CW Wholesale Restocked",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=f"{self.ctx.author.mention}", inline=False)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WholesaleClearConfirmView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context, target: str = "guns"):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.target = target

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.target == "cw":
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await interaction.response.edit_message(content="Cyberware system unavailable.", view=None)
                self.stop()
                return
            async with cw_cog.lock:
                state = await cw_cog._load_state()
                state["cw_wholesale_lots"] = []
                await cw_cog._save_state(state)
            label = "CW"
        else:
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await interaction.response.edit_message(content="Gun shop system unavailable.", view=None)
                self.stop()
                return
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                state["wholesale_lots"] = []
                await guns_cog._save_state(state)
            label = "Gun"

        await interaction.response.edit_message(content=f"✅ All {label} wholesale lots cleared.", view=None)
        log_ch = await self.cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title=f"🗑️ Admin: {label} Wholesale Cleared",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=f"{self.ctx.author.mention}", inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class AdminShopCog(commands.Cog, name="AdminShop"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _audit_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
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

    @commands.command(name="admin", aliases=["admin_shop"])
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    @commands.max_concurrency(1, per=commands.BucketType.user)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def admin_shop(self, ctx: commands.Context):
        """Open the Admin Shop management panel.

        Actions: Add/Remove items, Reassign, Item history lookup, Player inventory lookup.
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in the server.")
            return

        embed = discord.Embed(
            title="🔧 Admin Shop Panel",
            description=(
                "Choose an admin action below.\n\n"
                "**Add Item** — Add items to a player's inventory\n"
                "**Remove Item** — Remove an item by UUID\n"
                "**Reassign** — Transfer/reassign an item\n"
                "**Item History** — Look up audit trail by UUID\n"
                "**Player Inventory** — Browse a player's items\n"
                "**Wholesale Stock** — View gun + CW wholesale inventory\n"
                "**Restock Wholesale** — Add guns to wholesale\n"
                "**Clear Gun WH** — Remove all gun wholesale lots\n"
                "**Restock CW** — Add cyberware to wholesale\n"
                "**Clear CW WH** — Remove all CW wholesale lots"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Admin: {ctx.author.display_name}")

        view = AdminShopMenuView(self, ctx)
        msg = await ctx.send(embed=embed, view=view, delete_after=120)
        view.message = msg
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminShopCog(bot))
