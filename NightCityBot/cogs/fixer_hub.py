"""Unified !fixer hub — interactive panel for Fixer-level management.

Three top-level categories: Player, Store, Wholesaler.
Each opens a sub-menu with relevant actions.
"""
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
    pi_get_item,
    pi_get_by_owner,
    pi_delete_item,
    pi_update_owner,
    pi_update_character,
    ih_record_event,
    ih_get_history,
)
from NightCityBot.utils.permissions import is_fixer

logger = logging.getLogger(__name__)


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


async def _audit_channel(bot: commands.Bot) -> Optional[discord.TextChannel]:
    ch_id = getattr(config, "NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
    if not ch_id:
        return None
    ch = bot.get_channel(int(ch_id))
    if ch is None:
        try:
            ch = await bot.fetch_channel(int(ch_id))
        except Exception:
            pass
    return ch


def _resolve_user_select(ctx, user) -> Optional[discord.Member]:
    if isinstance(user, discord.Member):
        return user
    guild = ctx.guild
    if guild and user:
        return guild.get_member(user.id)
    return None


class FixerTopView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
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

    @discord.ui.button(label="Player", style=discord.ButtonStyle.primary, emoji="👤", row=0)
    async def player_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PlayerSubView(self.cog, self.ctx, parent=self)
        embed = discord.Embed(
            title="👤 Fixer Panel — Player",
            description=(
                "**View Inventory** — Browse a player's items\n"
                "**Add Item** — Give an item to a player\n"
                "**Remove Item** — Delete an item by UUID\n"
                "**Reassign Item** — Transfer item to new owner/character\n"
                "**Item History** — Audit trail for an item UUID\n"
                "**Start LOA** — Put a player on Leave of Absence\n"
                "**End LOA** — Take a player off LOA"
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Store", style=discord.ButtonStyle.primary, emoji="🏪", row=0)
    async def store_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StoreSubView(self.cog, self.ctx, parent=self)
        embed = discord.Embed(
            title="🏪 Fixer Panel — Store",
            description=(
                "**View Gun Store** — Inspect a store owner's gun inventory\n"
                "**View CW Store** — Inspect a Ripperdoc's cyberware stock\n"
                "**Add to Gun Store** — Add a gun lot to a store\n"
                "**Remove from Gun Store** — Remove a gun lot from a store"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Wholesaler", style=discord.ButtonStyle.primary, emoji="🏭", row=0)
    async def wholesaler_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = WholesalerSubView(self.cog, self.ctx, parent=self)
        embed = discord.Embed(
            title="🏭 Fixer Panel — Wholesaler",
            description=(
                "**View Stock** — See current gun + CW wholesale inventory\n"
                "**Add Gun** — Add a gun lot to wholesale\n"
                "**Add CW** — Add a cyberware lot to wholesale\n"
                "**Remove Lot** — Remove a specific lot by ID\n"
                "**Restock Guns** — Full weekly gun restock\n"
                "**Restock CW** — Full weekly CW restock"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class PlayerSubView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context, parent: FixerTopView):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.parent = parent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=0)
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerInvPickerView(self.cog, self.ctx)
        await interaction.followup.send("Select a player to view their inventory:", view=view, ephemeral=True)

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerAddItemPickerView(self.cog, self.ctx)
        await interaction.followup.send("**Step 1** — Select the player to add an item to:", view=view, ephemeral=True)

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlayerRemoveItemModal(self.cog))

    @discord.ui.button(label="Reassign Item", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def reassign_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlayerReassignModal(self.cog))

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=1)
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ItemHistoryModal(self.cog))

    @discord.ui.button(label="Start LOA", style=discord.ButtonStyle.success, emoji="🏖️", row=2)
    async def start_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="start")
        await interaction.followup.send("Select a player to put on LOA:", view=view, ephemeral=True)

    @discord.ui.button(label="End LOA", style=discord.ButtonStyle.danger, emoji="🔚", row=2)
    async def end_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="end")
        await interaction.followup.send("Select a player to take off LOA:", view=view, ephemeral=True)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Fixer Panel",
            description=(
                "Choose a category below.\n\n"
                "**Player** — Inventory, items, LOA, history\n"
                "**Store** — Gun store and Ripperdoc stock management\n"
                "**Wholesaler** — Wholesale inventory and restocking"
            ),
            color=discord.Color.dark_gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self.parent)


class StoreSubView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context, parent: FixerTopView):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.parent = parent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="View Gun Store", style=discord.ButtonStyle.secondary, emoji="🔫", row=0)
    async def view_gun_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = StoreInvPickerView(self.cog, self.ctx, store_type="gun")
        await interaction.followup.send("Select a store owner to view their gun store:", view=view, ephemeral=True)

    @discord.ui.button(label="View CW Store", style=discord.ButtonStyle.secondary, emoji="💉", row=0)
    async def view_cw_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = StoreInvPickerView(self.cog, self.ctx, store_type="cw")
        await interaction.followup.send("Select a Ripperdoc to view their CW stock:", view=view, ephemeral=True)

    @discord.ui.button(label="Add to Gun Store", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add_to_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = StoreAddPickerView(self.cog, self.ctx)
        await interaction.followup.send("**Step 1** — Select the store owner:", view=view, ephemeral=True)

    @discord.ui.button(label="Remove from Gun Store", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_from_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = StoreRemovePickerView(self.cog, self.ctx)
        await interaction.followup.send("**Step 1** — Select the store owner:", view=view, ephemeral=True)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Fixer Panel",
            description=(
                "Choose a category below.\n\n"
                "**Player** — Inventory, items, LOA, history\n"
                "**Store** — Gun store and Ripperdoc stock management\n"
                "**Wholesaler** — Wholesale inventory and restocking"
            ),
            color=discord.Color.dark_gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self.parent)


class WholesalerSubView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context, parent: FixerTopView):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.parent = parent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="View Stock", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    @discord.ui.button(label="Add Gun", style=discord.ButtonStyle.primary, emoji="🔫", row=0)
    async def add_gun(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WHAddGunModal(self.cog))

    @discord.ui.button(label="Add CW", style=discord.ButtonStyle.primary, emoji="💉", row=0)
    async def add_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WHAddCWModal(self.cog))

    @discord.ui.button(label="Remove Lot", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_lot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WHRemoveLotModal(self.cog))

    @discord.ui.button(label="Restock Guns", style=discord.ButtonStyle.success, emoji="📥", row=1)
    async def restock_guns(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "🔫 **Full gun restock** pulls from the master sheet and applies restock settings.\n"
            "Run `!guns_wh_restock` in chat to trigger it.\n\n"
            "To add individual lots manually, use the **Add Gun** button above.",
            ephemeral=True,
        )

    @discord.ui.button(label="Restock CW", style=discord.ButtonStyle.success, emoji="💊", row=1)
    async def restock_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "💉 **Full CW restock** pulls from the cyberware catalogue and applies restock settings.\n"
            "Run `!cw_wh_restock` in chat to trigger it.\n\n"
            "To add individual lots manually, use the **Add CW** button above.",
            ephemeral=True,
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Fixer Panel",
            description=(
                "Choose a category below.\n\n"
                "**Player** — Inventory, items, LOA, history\n"
                "**Store** — Gun store and Ripperdoc stock management\n"
                "**Wholesaler** — Wholesale inventory and restocking"
            ),
            color=discord.Color.dark_gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self.parent)


class PlayerInvPickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = _resolve_user_select(self.ctx, user)
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
            char = item.get("character_name", "—")
            iid = item.get("item_id", "?")[:8]
            lines.append(f"`{i}.` **{name}** [{itype}] — {char} (`{iid}...`)")
        embed = discord.Embed(
            title=f"📦 {member.display_name}'s Inventory",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(items)} item(s) total")
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlayerAddItemPickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.selected_player: Optional[discord.Member] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        self.selected_player = member
        await interaction.response.send_message(f"Player: **{member.display_name}** ✓", ephemeral=True)

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_player is None:
            await interaction.response.send_message("Please select a player first.", ephemeral=True)
            return
        await interaction.response.send_modal(PlayerAddItemDetailsModal(self.cog, self.selected_player))
        self.stop()


class PlayerAddItemDetailsModal(discord.ui.Modal, title="Add Item — Details"):
    name_input = discord.ui.TextInput(label="Item Name")
    character_input = discord.ui.TextInput(label="Character Name")
    item_type_input = discord.ui.TextInput(label="Type (gun/cyberware/gear/misc)", default="misc")
    qty_price_input = discord.ui.TextInput(
        label="Qty,Price (e.g. 1,5000 or just 1)",
        default="1",
        required=False,
    )

    def __init__(self, cog: "FixerHubCog", player: discord.Member):
        super().__init__()
        self.cog = cog
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        player = self.player
        name = self.name_input.value.strip()
        character = self.character_input.value.strip()
        item_type = self.item_type_input.value.strip().lower() or "misc"
        qty = 1
        price = None
        raw_qp = self.qty_price_input.value.strip()
        if raw_qp:
            parts = raw_qp.split(",")
            try:
                qty = int(parts[0].strip())
            except ValueError:
                qty = 1
            if len(parts) > 1:
                try:
                    price = int(parts[1].strip())
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
                "owner_id": str(player.id),
                "character_name": character,
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
                    target_id=str(player.id),
                    price=price,
                    metadata={"item_name": name, "character": character, "item_type": item_type},
                )
        await interaction.followup.send(
            f"Added **{name}** ×{added} to {player.display_name}'s inventory ({character}).",
            ephemeral=True,
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="🔧 Fixer: Item Added",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Player", value=f"{player.mention} — {character}", inline=False)
            embed.add_field(name="Item", value=name, inline=True)
            embed.add_field(name="Qty", value=str(added), inline=True)
            embed.add_field(name="Type", value=item_type, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class PlayerRemoveItemModal(discord.ui.Modal, title="Remove Item"):
    player_input = discord.ui.TextInput(label="Player (@mention or ID)")
    item_id_input = discord.ui.TextInput(label="Item UUID")

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        player = await _resolve_member(guild, self.player_input.value)
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
            actor_id=str(interaction.user.id),
            target_id=str(player.id),
            metadata={"item_name": item_name},
        )
        await interaction.followup.send(
            f"Removed **{item_name}** (`{item_id}`) from {player.display_name}.", ephemeral=True
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Fixer: Item Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Player", value=f"{player.mention}", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class PlayerReassignModal(discord.ui.Modal, title="Reassign Item"):
    item_id_input = discord.ui.TextInput(label="Item UUID")
    player_input = discord.ui.TextInput(label="New Owner (@mention or ID)")
    character_input = discord.ui.TextInput(label="New Character Name")

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        item_id = self.item_id_input.value.strip()
        item = await pi_get_item(item_id)
        if item is None:
            await interaction.followup.send(f"Item `{item_id}` not found.", ephemeral=True)
            return
        new_owner = await _resolve_member(guild, self.player_input.value)
        if not new_owner:
            await interaction.followup.send("Could not find new owner.", ephemeral=True)
            return
        new_char = self.character_input.value.strip()
        item_name = item.get("name", "?")
        old_owner_id = item.get("owner_id", "")
        old_char = item.get("character_name", "")
        if str(new_owner.id) == old_owner_id:
            ok = await pi_update_character(item_id, new_char)
        else:
            ok = await pi_update_owner(item_id, str(new_owner.id), new_char, old_owner_id)
        if not ok:
            await interaction.followup.send("Failed to reassign item.", ephemeral=True)
            return
        await ih_record_event(
            item_id, "admin_reassign",
            actor_id=str(interaction.user.id),
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
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="✏️ Fixer: Item Reassigned",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
            embed.add_field(name="Old", value=f"<@{old_owner_id}> — {old_char}", inline=True)
            embed.add_field(name="New", value=f"{new_owner.mention} — {new_char}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class ItemHistoryModal(discord.ui.Modal, title="Item History Lookup"):
    item_id_input = discord.ui.TextInput(label="Item UUID")

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

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


class LOAPickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context, action: str = "start"):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        loa_cog = self.cog.bot.get_cog("LOA")
        if not loa_cog:
            await interaction.followup.send("LOA system unavailable.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        loa_role = loa_cog.get_loa_role(guild)
        if loa_role is None:
            await interaction.followup.send("⚠️ LOA role is not configured.", ephemeral=True)
            return
        has_role = any(r.id == loa_role.id for r in member.roles)
        if self.action == "start":
            if has_role:
                await interaction.followup.send(
                    f"{member.display_name} is already on LOA.", ephemeral=True
                )
                return
            await member.add_roles(loa_role, reason=f"LOA start by {interaction.user}")
            await interaction.followup.send(
                f"✅ {member.display_name} is now on LOA.", ephemeral=True
            )
        else:
            if not has_role:
                await interaction.followup.send(
                    f"{member.display_name} is not currently on LOA.", ephemeral=True
                )
                return
            await member.remove_roles(loa_role, reason=f"LOA end by {interaction.user}")
            await interaction.followup.send(
                f"✅ {member.display_name}'s LOA has ended.", ephemeral=True
            )
        self.stop()


class StoreInvPickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context, store_type: str = "gun"):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        owner = _resolve_user_select(self.ctx, user)
        if not owner:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild

        if self.store_type == "gun":
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
                return
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, owner.id)
            lots = [
                l for l in state.get("stores", {}).get(store_id, {}).get("lots", [])
                if l.get("qty_remaining", 0) > 0
            ]
            if not lots:
                await interaction.followup.send(
                    f"{owner.display_name}'s gun store is empty.", ephemeral=True
                )
                return
            lines = []
            for i, l in enumerate(lots[:25], 1):
                r = l.get("restriction", "basic")
                r_tag = f" [{r}]" if r != "basic" else ""
                lines.append(
                    f"`{i}.` **{l['gun_name']}**{r_tag} — ${int(l['unit_cost']):,} × {l['qty_remaining']}"
                )
            embed = discord.Embed(
                title=f"🔫 {owner.display_name}'s Gun Store",
                description="\n".join(lines),
                color=discord.Color.dark_green(),
            )
            embed.set_footer(text=f"{len(lots)} lot(s)")
        else:
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
                return
            inventory = await cw_cog._load_inventory(owner.id)
            if not inventory:
                await interaction.followup.send(
                    f"{owner.display_name}'s CW stock is empty.", ephemeral=True
                )
                return
            groups = cw_cog._grouped_inventory(inventory)
            lines = []
            for i, g in enumerate(groups[:25], 1):
                count_str = f" × {g['count']}" if g['count'] > 1 else ""
                price_str = f"${g['price_paid']:,}" if g.get('price_paid') else "—"
                lines.append(f"`{i}.` **{g['name']}**{count_str} — {price_str}")
            embed = discord.Embed(
                title=f"💉 {owner.display_name}'s CW Stock",
                description="\n".join(lines),
                color=discord.Color.purple(),
            )
            embed.set_footer(text=f"{len(inventory)} item(s) in {len(groups)} slot(s)")

        await interaction.followup.send(embed=embed, ephemeral=True)


class StoreAddPickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.selected_owner: Optional[discord.Member] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        self.selected_owner = member
        await interaction.response.send_message(f"Store Owner: **{member.display_name}** ✓", ephemeral=True)

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_owner is None:
            await interaction.response.send_message("Please select a store owner first.", ephemeral=True)
            return
        await interaction.response.send_modal(StoreAddDetailsModal(self.cog, self.selected_owner))
        self.stop()


class StoreAddDetailsModal(discord.ui.Modal, title="Add to Gun Store — Details"):
    gun_name_input = discord.ui.TextInput(label="Gun Name")
    qty_input = discord.ui.TextInput(label="Quantity", default="1")
    cost_input = discord.ui.TextInput(label="Unit Cost", placeholder="5000")
    restriction_input = discord.ui.TextInput(
        label="Restriction (basic/controlled/restricted)",
        default="basic",
        required=False,
    )

    def __init__(self, cog: "FixerHubCog", owner: discord.Member):
        super().__init__()
        self.cog = cog
        self.owner = owner

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        owner = self.owner
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
        restriction = self.restriction_input.value.strip().lower() or "basic"
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, owner.id)
            store = state.setdefault("stores", {}).setdefault(store_id, {"lots": []})
            lot_id = f"fixer-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            store["lots"].append({
                "lot_id": lot_id,
                "gun_name": gun_name,
                "gun_level": "L",
                "weapon_type": "",
                "unit_cost": cost,
                "qty_remaining": qty,
                "restriction": restriction,
            })
            await guns_cog._save_state(state)
        await interaction.followup.send(
            f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] to {owner.display_name}'s store.",
            ephemeral=True,
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="➕ Fixer: Store Stock Added",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Store Owner", value=f"{owner.mention}", inline=False)
            embed.add_field(name="Gun", value=gun_name, inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class StoreRemovePickerView(discord.ui.View):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.selected_owner: Optional[discord.Member] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        self.selected_owner = member
        await interaction.response.send_message(f"Store Owner: **{member.display_name}** ✓", ephemeral=True)

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_owner is None:
            await interaction.response.send_message("Please select a store owner first.", ephemeral=True)
            return
        await interaction.response.send_modal(StoreRemoveDetailsModal(self.cog, self.selected_owner))
        self.stop()


class StoreRemoveDetailsModal(discord.ui.Modal, title="Remove from Gun Store"):
    lot_id_input = discord.ui.TextInput(label="Lot ID")
    qty_input = discord.ui.TextInput(label="Qty to remove (blank = all)", required=False)

    def __init__(self, cog: "FixerHubCog", owner: discord.Member):
        super().__init__()
        self.cog = cog
        self.owner = owner

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        owner = self.owner
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        lot_id = self.lot_id_input.value.strip()
        raw_qty = self.qty_input.value.strip()
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, owner.id)
            store = state.get("stores", {}).get(store_id)
            if not store:
                await interaction.followup.send("Store not found.", ephemeral=True)
                return
            lot = next((l for l in store.get("lots", []) if l.get("lot_id") == lot_id), None)
            if not lot:
                await interaction.followup.send(f"Lot `{lot_id}` not found in store.", ephemeral=True)
                return
            gun_name = lot.get("gun_name", "?")
            current_qty = int(lot.get("qty_remaining", 0))
            if raw_qty:
                try:
                    remove_qty = int(raw_qty)
                except ValueError:
                    await interaction.followup.send("Qty must be a number.", ephemeral=True)
                    return
                if remove_qty <= 0:
                    await interaction.followup.send("Qty must be positive.", ephemeral=True)
                    return
                if remove_qty >= current_qty:
                    store["lots"].remove(lot)
                    removed = current_qty
                else:
                    lot["qty_remaining"] = current_qty - remove_qty
                    removed = remove_qty
            else:
                store["lots"].remove(lot)
                removed = current_qty
            await guns_cog._save_state(state)
        await interaction.followup.send(
            f"Removed **{gun_name}** ×{removed} from {owner.display_name}'s store.", ephemeral=True
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Fixer: Store Stock Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Store Owner", value=f"{owner.mention}", inline=False)
            embed.add_field(name="Gun", value=f"**{gun_name}** (`{lot_id}`)", inline=False)
            embed.add_field(name="Qty Removed", value=str(removed), inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WHAddGunModal(discord.ui.Modal, title="Add Gun to Wholesale"):
    gun_name_input = discord.ui.TextInput(label="Gun Name")
    qty_input = discord.ui.TextInput(label="Quantity", default="10")
    cost_input = discord.ui.TextInput(label="Unit Cost", placeholder="5000")
    restriction_input = discord.ui.TextInput(
        label="Restriction (basic/controlled/restricted)",
        default="basic",
        required=False,
    )

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

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
        restriction = self.restriction_input.value.strip().lower() or "basic"
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            lots = state.setdefault("wholesale_lots", [])
            lot_id = f"fixer-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
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
            f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] to wholesale.", ephemeral=True
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="📥 Fixer: Gun Wholesale Restocked",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Gun", value=gun_name, inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WHAddCWModal(discord.ui.Modal, title="Add CW to Wholesale"):
    item_name_input = discord.ui.TextInput(label="Cyberware Name")
    qty_input = discord.ui.TextInput(label="Quantity", default="10")
    cost_input = discord.ui.TextInput(label="Unit Cost", placeholder="5000")

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

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
            lot_id = f"fixer-cw-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            lots.append({
                "lot_id": lot_id,
                "item_name": item_name,
                "unit_cost": cost,
                "qty_available": qty,
            })
            await cw_cog._save_state(state)
        await interaction.followup.send(
            f"Added CW **{item_name}** ×{qty} at ${cost:,} to wholesale.", ephemeral=True
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title="📥 Fixer: CW Wholesale Restocked",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Qty", value=str(qty), inline=True)
            embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WHRemoveLotModal(discord.ui.Modal, title="Remove Wholesale Lot"):
    lot_id_input = discord.ui.TextInput(label="Lot ID")
    qty_input = discord.ui.TextInput(label="Qty to remove (blank = all)", required=False)

    def __init__(self, cog: "FixerHubCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lot_id = self.lot_id_input.value.strip()
        raw_qty = self.qty_input.value.strip()

        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")

        found_in = None
        if guns_cog:
            state = await guns_cog._load_state()
            lot = next((l for l in state.get("wholesale_lots", []) if l.get("lot_id") == lot_id), None)
            if lot:
                found_in = "gun"
        if not found_in and cw_cog:
            state = await cw_cog._load_state()
            lot = next((l for l in state.get("cw_wholesale_lots", []) if l.get("lot_id") == lot_id), None)
            if lot:
                found_in = "cw"

        if not found_in:
            await interaction.followup.send(f"Lot `{lot_id}` not found in either wholesale.", ephemeral=True)
            return

        if found_in == "gun":
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                lots = state.get("wholesale_lots", [])
                lot = next((l for l in lots if l.get("lot_id") == lot_id), None)
                if not lot:
                    await interaction.followup.send("Lot disappeared.", ephemeral=True)
                    return
                item_name = lot.get("gun_name", "?")
                current_qty = int(lot.get("qty_available", 0))
                if raw_qty:
                    try:
                        remove_qty = int(raw_qty)
                    except ValueError:
                        await interaction.followup.send("Qty must be a number.", ephemeral=True)
                        return
                    if remove_qty <= 0:
                        await interaction.followup.send("Qty must be positive.", ephemeral=True)
                        return
                    if remove_qty >= current_qty:
                        lots.remove(lot)
                        removed = current_qty
                    else:
                        lot["qty_available"] = current_qty - remove_qty
                        removed = remove_qty
                else:
                    lots.remove(lot)
                    removed = current_qty
                await guns_cog._save_state(state)
        else:
            async with cw_cog.lock:
                state = await cw_cog._load_state()
                lots = state.get("cw_wholesale_lots", [])
                lot = next((l for l in lots if l.get("lot_id") == lot_id), None)
                if not lot:
                    await interaction.followup.send("Lot disappeared.", ephemeral=True)
                    return
                item_name = lot.get("item_name", "?")
                current_qty = int(lot.get("qty_available", 0))
                if raw_qty:
                    try:
                        remove_qty = int(raw_qty)
                    except ValueError:
                        await interaction.followup.send("Qty must be a number.", ephemeral=True)
                        return
                    if remove_qty <= 0:
                        await interaction.followup.send("Qty must be positive.", ephemeral=True)
                        return
                    if remove_qty >= current_qty:
                        lots.remove(lot)
                        removed = current_qty
                    else:
                        lot["qty_available"] = current_qty - remove_qty
                        removed = remove_qty
                else:
                    lots.remove(lot)
                    removed = current_qty
                await cw_cog._save_state(state)

        label = "Gun" if found_in == "gun" else "CW"
        await interaction.followup.send(
            f"Removed **{item_name}** ×{removed} from {label} wholesale.", ephemeral=True
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch:
            embed = discord.Embed(
                title=f"🗑️ Fixer: {label} Wholesale Lot Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}** (`{lot_id}`)", inline=False)
            embed.add_field(name="Qty Removed", value=str(removed), inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class FixerHubCog(commands.Cog, name="FixerHub"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="fixer")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def fixer(self, ctx: commands.Context):
        """Open the Fixer management panel."""
        if not ctx.guild:
            await ctx.send("This command can only be used in the server.")
            return

        embed = discord.Embed(
            title="🛠️ Fixer Panel",
            description=(
                "Choose a category below.\n\n"
                "**Player** — Inventory, items, LOA, history\n"
                "**Store** — Gun store and Ripperdoc stock management\n"
                "**Wholesaler** — Wholesale inventory and restocking"
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(text=f"Fixer: {ctx.author.display_name}")

        view = FixerTopView(self, ctx)
        msg = await ctx.send(embed=embed, view=view, delete_after=120)
        view.message = msg
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FixerHubCog(bot))
