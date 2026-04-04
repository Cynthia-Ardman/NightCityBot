"""Unified !fixer hub — interactive panel for Fixer-level management.

Three top-level categories: Player, Store, Wholesaler.
Each opens a sub-menu with relevant actions.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView
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
from NightCityBot.utils.characters import get_active_characters, ensure_character_active, get_character_by_name
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.inline_helpers import collect_text_input
from NightCityBot.utils.panel_context import PanelContext

logger = logging.getLogger(__name__)


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


class FixerTopView(SafeView):
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
        if not (any(r.id == config.FIXER_ROLE_ID for r in member.roles) or member.guild_permissions.administrator):
            await interaction.response.send_message("This panel is for Fixers only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Player", style=discord.ButtonStyle.primary, emoji="👤", row=0, custom_id="fixer:player_menu")
    async def player_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("FixerHub")
        ctx = PanelContext(interaction)
        view = PlayerSubView(cog, ctx)
        embed = discord.Embed(
            title="👤 Fixer Panel — Player",
            description=(
                "**View Inventory** — Browse a player's items\n"
                "**Add Item** — Give an item to a player\n"
                "**Remove Item** — Delete an item by UUID\n"
                "**Reassign Item** — Transfer an item to a new owner/character\n"
                "**Start LOA** — Put a player on Leave of Absence\n"
                "**End LOA** — Take a player off LOA"
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Store", style=discord.ButtonStyle.primary, emoji="🏪", row=0, custom_id="fixer:store_menu")
    async def store_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("FixerHub")
        ctx = PanelContext(interaction)
        view = StoreSubView(cog, ctx)
        embed = discord.Embed(
            title="🏪 Fixer Panel — Store",
            description=(
                "**View Gun Store** — Select a store to view, add, or remove items\n"
                "**View Ripperdoc Store** — Select a Ripperdoc to view, add, or remove stock"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Wholesaler", style=discord.ButtonStyle.primary, emoji="🏭", row=0, custom_id="fixer:wholesaler_menu")
    async def wholesaler_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("FixerHub")
        ctx = PanelContext(interaction)
        view = WholesalerSubView(cog, ctx)
        embed = discord.Embed(
            title="🏭 Fixer Panel — Wholesaler",
            description=(
                "**View Stock** — See current gun + cyberware wholesale inventory\n"
                "**Add Gun** — Add a gun lot to wholesale\n"
                "**Add Cyberware** — Add a cyberware lot to wholesale\n"
                "**Remove Gun** — Remove a gun lot from wholesale\n"
                "**Remove Cyberware** — Remove a cyberware lot from wholesale"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PlayerSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

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
        await interaction.response.defer(ephemeral=True)
        view = PlayerRemoveItemView(self.cog, self.ctx)
        await interaction.followup.send(
            "**Remove Item** — Select the player, then enter the item UUID:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Reassign Item", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def reassign_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ReassignSourcePickerView(self.cog, self.ctx)
        await interaction.followup.send(
            "✏️ **Reassign Item — Step 1** — Select the player who currently owns the item:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Start LOA", style=discord.ButtonStyle.success, emoji="🏖️", row=1)
    async def start_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="start")
        await interaction.followup.send("Select a player to put on LOA:", view=view, ephemeral=True)

    @discord.ui.button(label="End LOA", style=discord.ButtonStyle.danger, emoji="🔚", row=1)
    async def end_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="end")
        await interaction.followup.send("Select a player to take off LOA:", view=view, ephemeral=True)


GUN_STORE_OWNER_ROLE_ID = config.GUN_STORE_OWNER_ROLE_ID
RIPPERDOC_ROLE_ID = config.RIPPERDOC_ROLE_ID


class StoreSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="View Gun Store", style=discord.ButtonStyle.secondary, emoji="🔫", row=0)
    async def view_gun_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        state = await guns_cog._load_state() if guns_cog else {}
        stores = state.get("stores", {})
        guild_prefix = f"{guild.id}:"
        options = []
        for store_id, store_data in stores.items():
            if not store_id.startswith(guild_prefix):
                continue
            owner_id_str = str(store_data.get("owner_id", ""))
            if not owner_id_str:
                continue
            m = guild.get_member(int(owner_id_str))
            if not m:
                try:
                    m = await guild.fetch_member(int(owner_id_str))
                except Exception:
                    continue
            store_name = store_data.get("store_name")
            label = store_name or f"{m.display_name}'s Gun Store"
            options.append(discord.SelectOption(
                label=label[:100], value=str(m.id),
                description=m.display_name[:100] if store_name else None,
            ))
            if len(options) >= 25:
                break
        if not options:
            await interaction.followup.send("No gun stores found.", ephemeral=True)
            return
        view = StoreOwnerPickerView(self.cog, self.ctx, options, store_type="gun")
        await interaction.followup.send(
            "🔫 **Gun Store** — Select a store:", view=view, ephemeral=True
        )

    @discord.ui.button(label="View Ripperdoc Store", style=discord.ButtonStyle.secondary, emoji="💉", row=0)
    async def view_ripperdoc_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        cw_cog = interaction.client.get_cog("CyberwareShop")
        rd_stores = {}
        if cw_cog:
            state = await cw_cog._load_state()
            rd_stores = state.get("ripperdoc_stores", {})
        role = guild.get_role(RIPPERDOC_ROLE_ID)
        if not role or not role.members:
            await interaction.followup.send("No Ripperdocs found.", ephemeral=True)
            return
        options = []
        guild_prefix = f"rd:{guild.id}:"
        for m in role.members[:25]:
            store_name = None
            for sid, s in rd_stores.items():
                if sid.startswith(guild_prefix) and s.get("owner_id") == m.id and s.get("store_name"):
                    store_name = s["store_name"]
                    break
            if not store_name:
                continue
            options.append(discord.SelectOption(label=store_name[:100], value=str(m.id),
                                               description=m.display_name[:100]))
        if not options:
            await interaction.followup.send("No Ripperdoc stores found. Ripperdocs must set up a store first.", ephemeral=True)
            return
        view = StoreOwnerPickerView(self.cog, self.ctx, options, store_type="cw")
        await interaction.followup.send(
            "💉 **Ripperdoc Store** — Select a Ripperdoc:", view=view, ephemeral=True
        )


class WholesalerSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

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
                lines.append("\n**💉 Cyberware Wholesale:**")
                for i, lot in enumerate(available[:15], 1):
                    lines.append(
                        f"`{i}.` **{lot['item_name']}** — ${int(lot['unit_cost']):,} × {lot['qty_available']}"
                    )
            else:
                lines.append("**💉 Cyberware Wholesale:** Empty")
        if not lines:
            await interaction.followup.send("No wholesale systems available.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🏭 Wholesale Stock Overview",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Add Gun", style=discord.ButtonStyle.primary, emoji="🔫", row=1)
    async def add_gun(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.followup.send(
            "📝 **Enter gun wholesale details** in this format:\n"
            "`gun name, quantity, unit cost, restriction`\n"
            "Example: `Militech Mk.31, 10, 5000, basic`\n"
            "• **Basic** — freely available, no restrictions\n"
            "• **Controlled** — requires a license or special authorization to purchase\n"
            "• **Restricted** — illegal or military-grade, requires admin approval for sale\n"
            "Type `cancel` to abort.",
            ephemeral=True,
            wait=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await msg.edit(content="⏰ Timed out or cancelled.")
            return
        await _process_wh_add_gun(self.cog, interaction, text, msg)

    @discord.ui.button(label="Add Cyberware", style=discord.ButtonStyle.primary, emoji="💉", row=1)
    async def add_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.followup.send(
            "📝 **Enter cyberware wholesale details** in this format:\n"
            "`cyberware name, quantity, unit cost`\n"
            "Example: `Neural Link, 10, 5000`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
            wait=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await msg.edit(content="⏰ Timed out or cancelled.")
            return
        await _process_wh_add_cw(self.cog, interaction, text, msg)

    @discord.ui.button(label="Remove Gun", style=discord.ButtonStyle.danger, emoji="🔫", row=2)
    async def remove_gun(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("Gun shop system not loaded.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        lots = state.get("wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await interaction.followup.send("🔫 Gun wholesale is empty — nothing to remove.", ephemeral=True)
            return
        options = []
        for lot in available[:25]:
            lid = lot.get("lot_id", "?")
            name = lot.get("gun_name", "?")
            r = lot.get("restriction", "basic")
            r_tag = f" [{r}]" if r != "basic" else ""
            label = f"{name}{r_tag}"[:100]
            desc = f"×{lot['qty_available']} — ${int(lot.get('unit_cost', 0)):,}"[:100]
            options.append(discord.SelectOption(label=label, value=lid, description=desc))
        view = WHRemoveGunPickerView(self.cog, self.ctx, options)
        await interaction.followup.send(
            "🔫 **Remove Gun Lot** — Select the lot to remove:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Remove Cyberware", style=discord.ButtonStyle.danger, emoji="💉", row=2)
    async def remove_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware shop system not loaded.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        lots = state.get("cw_wholesale_lots", [])
        available = [l for l in lots if int(l.get("qty_available", 0)) > 0]
        if not available:
            await interaction.followup.send("💉 Cyberware wholesale is empty — nothing to remove.", ephemeral=True)
            return
        options = []
        for lot in available[:25]:
            lid = lot.get("lot_id", "?")
            name = lot.get("item_name", "?")
            label = f"{name}"[:100]
            desc = f"×{lot['qty_available']} — ${int(lot.get('unit_cost', 0)):,}"[:100]
            options.append(discord.SelectOption(label=label, value=lid, description=desc))
        view = WHRemoveCWPickerView(self.cog, self.ctx, options)
        await interaction.followup.send(
            "💉 **Remove Cyberware Lot** — Select the lot to remove:",
            view=view,
            ephemeral=True,
        )


class ReassignSourcePickerView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the item's current owner…", row=0)
    async def source_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a player.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in server.", ephemeral=True)
            return
        member = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if member is None:
            try:
                member = await guild.fetch_member(raw_user.id)
            except Exception:
                await interaction.response.send_message("Could not find that member.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await interaction.followup.send(
                f"{member.display_name} has no items to reassign.", ephemeral=True
            )
            return
        options = []
        for item in items[:25]:
            iid = item.get("item_id", "")
            name = item.get("name", "?")
            char = item.get("character_name", "—")
            label = f"{name}"[:100]
            desc = f"Character: {char}"[:100]
            options.append(discord.SelectOption(label=label, value=iid, description=desc))
        view = ReassignItemPickerView(self.cog, self.ctx, member, items[:25])
        await interaction.followup.send(
            f"✏️ **Step 2** — Select the item from **{member.display_name}**:",
            view=view,
            ephemeral=True,
        )
        self.stop()


class ReassignItemPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, items: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.source_owner = source_owner
        self.items_map = {item.get("item_id", ""): item for item in items}
        options = []
        for item in items[:25]:
            iid = item.get("item_id", "")
            name = item.get("name", "?")
            char = item.get("character_name", "—")
            label = f"{name}"[:100]
            desc = f"Character: {char}"[:100]
            options.append(discord.SelectOption(label=label, value=iid, description=desc))
        select = discord.ui.Select(placeholder="Choose an item…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        item_id = interaction.data["values"][0]
        item = self.items_map.get(item_id)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        view = ReassignDestPickerView(self.cog, self.ctx, self.source_owner, item)
        await interaction.response.send_message(
            f"✏️ **Step 3** — Select the new owner for **{item.get('name', '?')}**:",
            view=view,
            ephemeral=True,
        )
        self.stop()


class ReassignDestPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, item: dict):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.source_owner = source_owner
        self.item = item

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the new owner…", row=0)
    async def dest_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await interaction.response.send_message("Please select a player.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in server.", ephemeral=True)
            return
        member = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if member is None:
            try:
                member = await guild.fetch_member(raw_user.id)
            except Exception:
                await interaction.response.send_message("Could not find that member.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        chars = await get_active_characters(str(member.id))
        if not chars:
            await interaction.followup.send(
                f"{member.display_name} has no active characters.", ephemeral=True
            )
            return
        view = ReassignCharPickerView(
            self.cog, self.ctx, self.source_owner, self.item, member, chars[:25]
        )
        await interaction.followup.send(
            f"✏️ **Step 4** — Select the character on **{member.display_name}** to receive the item:",
            view=view,
            ephemeral=True,
        )
        self.stop()


class ReassignCharPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, item: dict,
                 dest_owner: discord.Member, chars: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.source_owner = source_owner
        self.item = item
        self.dest_owner = dest_owner
        options = []
        for c in chars[:25]:
            cname = c.get("name", "?")
            options.append(discord.SelectOption(label=cname[:100], value=cname))
        select = discord.ui.Select(placeholder="Choose a character…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        new_char_name = interaction.data["values"][0]
        item_id = self.item.get("item_id", "")
        item_name = self.item.get("name", "?")
        old_owner_id = self.item.get("owner_id", "")
        old_char = self.item.get("character_name", "")

        await interaction.response.defer(ephemeral=True)

        char_record = await get_character_by_name(str(self.dest_owner.id), new_char_name)
        if char_record and not await ensure_character_active(char_record["character_id"]):
            await interaction.followup.send(
                f"❌ Character **{new_char_name}** is not active.", ephemeral=True
            )
            return

        new_char_id = char_record["character_id"] if char_record else None
        if str(self.dest_owner.id) == old_owner_id:
            ok = await pi_update_character(item_id, new_char_name, expected_owner_id=old_owner_id, new_character_id=new_char_id)
        else:
            ok = await pi_update_owner(item_id, str(self.dest_owner.id), new_char_name, old_owner_id, new_character_id=new_char_id)
        if not ok:
            await interaction.followup.send("Failed to reassign item.", ephemeral=True)
            return

        await ih_record_event(
            item_id, "fixer_reassign",
            actor_id=str(interaction.user.id),
            target_id=str(self.dest_owner.id),
            metadata={
                "item_name": item_name,
                "old_owner": old_owner_id,
                "old_character": old_char,
                "new_character": new_char_name,
            },
        )
        await interaction.followup.send(
            f"✅ Reassigned **{item_name}** from {self.source_owner.display_name} "
            f"to {self.dest_owner.display_name} — {new_char_name}.",
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
            embed.add_field(name="New", value=f"{self.dest_owner.mention} — {new_char_name}", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


async def _process_wh_add_gun(cog, interaction, text, msg=None):
    async def _reply(content):
        if msg:
            await msg.edit(content=content)
        else:
            await interaction.followup.send(content, ephemeral=True)

    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await _reply("Gun shop system unavailable.")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await _reply("❌ Need at least: `gun name, quantity, unit cost`")
        return
    gun_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await _reply("Quantity and cost must be numbers.")
        return
    if qty < 1 or cost < 0:
        await _reply("Invalid quantity or cost.")
        return
    restriction = parts[3].strip().lower() if len(parts) > 3 else ""
    while restriction not in ("basic", "controlled", "restricted"):
        await _reply(
            f"Got: **{gun_name}** ×{qty} at ${cost:,} — now enter the restriction level:\n"
            "`basic`, `controlled`, or `restricted`\n"
            "Type `cancel` to abort."
        )
        r_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if r_text is None:
            await _reply("⏰ Timed out or cancelled.")
            return
        restriction = r_text.strip().lower()
        if restriction not in ("basic", "controlled", "restricted"):
            await _reply(
                "❌ Invalid restriction. Must be `basic`, `controlled`, or `restricted`. Try again."
            )
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
    await _reply(f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] to wholesale.")
    log_ch = await _audit_channel(cog.bot)
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


async def _process_wh_add_cw(cog, interaction, text, msg=None):
    async def _reply(content):
        if msg:
            await msg.edit(content=content)
        else:
            await interaction.followup.send(content, ephemeral=True)

    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await _reply("Cyberware system unavailable.")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await _reply("❌ Need at least: `cyberware name, quantity, unit cost`")
        return
    item_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await _reply("Quantity and cost must be numbers.")
        return
    if qty < 1 or cost < 0:
        await _reply("Invalid quantity or cost.")
        return
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
    await _reply(f"Added CW **{item_name}** ×{qty} at ${cost:,} to wholesale.")
    log_ch = await _audit_channel(cog.bot)
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


async def _process_wh_remove_lot(cog, interaction, lot_id, remove_qty=None):
    lot_id = lot_id.strip()

    guns_cog = cog.bot.cogs.get("GunsShopCog")
    cw_cog = cog.bot.cogs.get("CyberwareShop")

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
        await interaction.followup.send(content=f"Lot `{lot_id}` not found in either wholesale.", ephemeral=True)
        return

    current_qty = int(lot.get("qty_available", 0))
    if current_qty > 1 and remove_qty is None:
        item_name = lot.get("gun_name") or lot.get("item_name") or "?"
        if current_qty <= 24:
            options = []
            for i in range(1, current_qty + 1):
                lbl = f"All ({i})" if i == current_qty else str(i)
                options.append(discord.SelectOption(label=lbl, value=f"{lot_id}:{i}"))
            view = WHRemoveQtyPickerView(cog, interaction.user.id, options)
            await interaction.followup.send(
                f"**{item_name}** has **{current_qty}** in stock. How many to remove?",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"**{item_name}** has **{current_qty}** in stock.\n"
                f"Enter the quantity to remove (1–{current_qty}), or `all` to remove everything.\n"
                "Type `cancel` to abort.",
                ephemeral=True,
            )
            text = await collect_text_input(
                interaction.client, interaction.channel_id, interaction.user.id
            )
            if text is None:
                await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                return
            text = text.strip().lower()
            if text == "all":
                remove_qty = current_qty
            else:
                try:
                    remove_qty = int(text)
                except ValueError:
                    await interaction.followup.send("❌ Invalid number.", ephemeral=True)
                    return
                if remove_qty < 1 or remove_qty > current_qty:
                    await interaction.followup.send(
                        f"❌ Must be between 1 and {current_qty}.", ephemeral=True
                    )
                    return
            await _process_wh_remove_lot(cog, interaction, lot_id, remove_qty=remove_qty)
        return

    if remove_qty is None:
        remove_qty = current_qty

    if found_in == "gun":
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            lots = state.get("wholesale_lots", [])
            lot = next((l for l in lots if l.get("lot_id") == lot_id), None)
            if not lot:
                await interaction.followup.send(content="Lot disappeared.", ephemeral=True)
                return
            item_name = lot.get("gun_name", "?")
            available = int(lot.get("qty_available", 0))
            removed = min(remove_qty, available)
            if removed >= available:
                lots.remove(lot)
            else:
                lot["qty_available"] = available - removed
            await guns_cog._save_state(state)
    else:
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            lots = state.get("cw_wholesale_lots", [])
            lot = next((l for l in lots if l.get("lot_id") == lot_id), None)
            if not lot:
                await interaction.followup.send(content="Lot disappeared.", ephemeral=True)
                return
            item_name = lot.get("item_name", "?")
            available = int(lot.get("qty_available", 0))
            removed = min(remove_qty, available)
            if removed >= available:
                lots.remove(lot)
            else:
                lot["qty_available"] = available - removed
            await cw_cog._save_state(state)

    label = "Gun" if found_in == "gun" else "CW"
    await interaction.followup.send(
        content=f"Removed **{item_name}** ×{removed} from {label} wholesale.",
        ephemeral=True,
    )
    log_ch = await _audit_channel(cog.bot)
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


class PlayerInvPickerView(SafeView):
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


class PlayerAddItemPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
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
        await interaction.response.edit_message(
            content=f"Player: **{member.display_name}** ✓ — Now select their character.",
            view=self,
        )

    async def _on_character_select(self, interaction: discord.Interaction):
        char_id = interaction.data["values"][0]
        for ch in self._characters:
            if ch["character_id"] == char_id:
                self.selected_character = ch
                break
        if self.selected_character:
            await interaction.response.edit_message(
                content=f"Player: **{self.selected_player.display_name}** ✓ | "
                        f"Character: **{self.selected_character['name']}** ✓ — Click Continue.",
                view=self,
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
        await interaction.response.defer(ephemeral=True)
        if not await ensure_character_active(self.selected_character["character_id"]):
            await interaction.followup.send(
                f"❌ Character **{self.selected_character['name']}** is no longer active.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "📝 **Enter item details** in this format:\n"
            "`item name, type, quantity, cost, restriction`\n\n"
            "**For guns:** `name, gun, qty, cost, restriction, power_level, type`\n"
            "Example: `Militech Pistol, gun, 1, 5000, basic, high, power`\n"
            "power_level: low/medium/high — type: power/smart/tech\n\n"
            "**For cyberware:** `name, cyberware, qty, cost, restriction, cwp, slot`\n"
            "Example: `Kerenzikov, cyberware, 1, 3000, basic, 14, Neural`\n\n"
            "**For other items:** `name, type, qty, cost`\n"
            "Available types: **gun**, **cyberware**, **gear**, **misc** (default)\n"
            "Type and price are optional (defaults: `misc`, no price). Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            self.stop()
            return
        await _process_fixer_add_item(
            self.cog, interaction, self.selected_player, self.selected_character, text
        )
        self.stop()


VALID_GUN_POWER_LEVELS = {"low", "medium", "high"}
VALID_GUN_TYPES = {"power", "smart", "tech"}
VALID_CW_SLOTS = {
    "skeleton & torso musculature",
    "arms & arm attachments",
    "miscellaneous",
    "integumentary system",
    "neural",
    "universal muscular (arms/legs/tail)",
    "hands & feet",
    "ocular system",
    "legs & mobility",
    "auditory system",
    "circulatory & immune systems",
}


class _ConfirmItemView(SafeView):
    def __init__(self, target_user_id: int):
        super().__init__(timeout=300)
        self.target_user_id = target_user_id
        self.result: Optional[bool] = None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        self.result = True
        self.stop()
        await interaction.response.edit_message(content="✅ **Approved** — processing…", view=None)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        self.result = False
        self.stop()
        await interaction.response.edit_message(content="❌ **Denied** — transaction cancelled.", view=None)

    async def on_timeout(self):
        self.result = None
        self.stop()


async def _process_fixer_add_item(cog, interaction, player, character, text):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    char_name = character.get("name", "")
    character_id = character.get("character_id")
    if not char_name:
        await interaction.followup.send("Character selection required.", ephemeral=True)
        return
    if character_id and not await ensure_character_active(character_id):
        await interaction.followup.send(
            f"❌ Character **{char_name}** is no longer active.", ephemeral=True
        )
        return
    parts = [p.strip() for p in text.split(",")]
    name = parts[0] if parts else ""
    if not name:
        await interaction.followup.send("❌ Item name is required.", ephemeral=True)
        return
    item_type = parts[1].lower() if len(parts) > 1 and parts[1] else "misc"
    qty = 1
    if len(parts) > 2:
        try:
            qty = int(parts[2])
        except ValueError:
            qty = 1
    price = None
    if len(parts) > 3:
        try:
            price = int(parts[3])
        except ValueError:
            pass
    restriction = "basic"
    if len(parts) > 4 and parts[4]:
        r = parts[4].strip().lower()
        if r in ("basic", "controlled", "restricted"):
            restriction = r
    if qty < 1:
        qty = 1

    power_level = None
    weapon_subtype = None
    cwp_val = None
    slot_val = None

    if item_type == "gun":
        if len(parts) < 7:
            await interaction.followup.send(
                "❌ Gun items require: `name, gun, quantity, cost, restriction, power_level, type`\n"
                "power_level: low/medium/high — type: power/smart/tech",
                ephemeral=True,
            )
            return
        pl_raw = parts[5].strip().lower()
        type_raw = parts[6].strip().lower()
        if pl_raw not in VALID_GUN_POWER_LEVELS:
            await interaction.followup.send(
                f"❌ Invalid power_level `{pl_raw}`. Must be one of: low, medium, high.",
                ephemeral=True,
            )
            return
        if type_raw not in VALID_GUN_TYPES:
            await interaction.followup.send(
                f"❌ Invalid gun type `{type_raw}`. Must be one of: power, smart, tech.",
                ephemeral=True,
            )
            return
        power_level = pl_raw
        weapon_subtype = type_raw

    elif item_type == "cyberware":
        if len(parts) < 7:
            await interaction.followup.send(
                "❌ Cyberware items require: `name, cyberware, quantity, cost, restriction, cwp, slot`\n"
                "cwp: integer — slot: one of the valid body locations",
                ephemeral=True,
            )
            return
        try:
            cwp_val = str(int(parts[5].strip()))
        except ValueError:
            await interaction.followup.send(
                f"❌ Invalid CWP `{parts[5].strip()}`. Must be an integer.",
                ephemeral=True,
            )
            return
        slot_raw = parts[6].strip().lower()
        if slot_raw not in VALID_CW_SLOTS:
            await interaction.followup.send(
                f"❌ Invalid slot `{parts[6].strip()}`. Valid slots:\n"
                + "\n".join(f"• {s.title()}" for s in sorted(VALID_CW_SLOTS)),
                ephemeral=True,
            )
            return
        slot_val = parts[6].strip()

    total_cost = (price or 0) * qty
    cash_deducted = 0
    bank_deducted = 0
    if price is not None and price > 0:
        confirm_view = _ConfirmItemView(target_user_id=player.id)
        confirm_msg = await interaction.followup.send(
            f"💰 {player.mention}, a Fixer wants to add **{name}** ×{qty} to your inventory "
            f"for **${total_cost:,}** total. Do you approve?",
            view=confirm_view,
            allowed_mentions=discord.AllowedMentions(users=[player]),
        )
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await confirm_msg.edit(content="⏰ Confirmation timed out — transaction cancelled.", view=None)
            except Exception:
                pass
            await interaction.followup.send("⏰ Player did not respond in time. Item not added.", ephemeral=True)
            return
        if confirm_view.result is False:
            await interaction.followup.send("❌ Player denied the transaction. Item not added.", ephemeral=True)
            return
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await interaction.followup.send("❌ Economy system unavailable.", ephemeral=True)
            return
        balance = await ub.get_balance(player.id)
        if balance is None:
            await interaction.followup.send("❌ Could not fetch player balance.", ephemeral=True)
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await interaction.followup.send(
                f"❌ Player cannot afford ${total_cost:,} (has ${cash + bank:,}).", ephemeral=True
            )
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            player.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer add-item: {name} x{qty}"
        )
        if not ok_deduct:
            await interaction.followup.send("❌ Failed to deduct funds. Item not added.", ephemeral=True)
            return

    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(qty):
        item_id = str(uuid.uuid4())
        ok = await pi_add_item({
            "item_id": item_id,
            "owner_id": str(player.id),
            "character_name": char_name,
            "character_id": character_id,
            "item_type": item_type,
            "name": name,
            "restriction": restriction,
            "description": "",
            "price_paid": price,
            "seller_id": str(interaction.user.id),
            "seller_name": interaction.user.display_name,
            "acquired_at": now,
            "power_level": power_level,
            "weapon_subtype": weapon_subtype,
            "cwp": cwp_val,
            "slot": slot_val,
        })
        if ok:
            added += 1
            meta = {"item_name": name, "character": char_name, "item_type": item_type}
            if power_level:
                meta["power_level"] = power_level
            if weapon_subtype:
                meta["weapon_subtype"] = weapon_subtype
            if cwp_val:
                meta["cwp"] = cwp_val
            if slot_val:
                meta["slot"] = slot_val
            await ih_record_event(
                item_id, "admin_add",
                actor_id=str(interaction.user.id),
                target_id=str(player.id),
                price=price,
                metadata=meta,
            )
    if added < qty and price is not None and price > 0:
        failed_qty = qty - added
        refund_amount = price * failed_qty
        ub = getattr(cog.bot, "unbelievaboat", None)
        refunded = False
        if ub and refund_amount > 0:
            refund_cash = min(cash_deducted, refund_amount)
            refund_bank = min(bank_deducted, refund_amount - refund_cash)
            refunded = await ub.update_balance(
                player.id, {"cash": refund_cash, "bank": refund_bank},
                reason=f"Fixer add-item partial refund: {failed_qty}x {name} failed to save"
            )
        logger.error(
            "CRITICAL: Deducted %d from user %s but only added %d/%d items '%s'. "
            "Refund attempted: %s (amount: %d).",
            total_cost, player.id, added, qty, name, refunded, refund_amount,
        )

    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        embed = discord.Embed(
            title="🔧 Fixer: Item Added",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Player", value=f"{player.mention} — {char_name}", inline=False)
        embed.add_field(name="Item", value=name, inline=True)
        embed.add_field(name="Qty", value=str(added), inline=True)
        embed.add_field(name="Type", value=item_type, inline=True)
        if price is not None and price > 0:
            embed.add_field(name="Cost", value=f"${price:,}", inline=True)
        if power_level:
            embed.add_field(name="Power Level", value=power_level.title(), inline=True)
        if weapon_subtype:
            embed.add_field(name="Gun Type", value=weapon_subtype.title(), inline=True)
        if cwp_val:
            embed.add_field(name="CWP", value=cwp_val, inline=True)
        if slot_val:
            embed.add_field(name="Slot", value=slot_val.title(), inline=True)
        if added < qty and price is not None and price > 0:
            embed.add_field(
                name="⚠️ Partial Failure",
                value=f"Only {added}/{qty} items added after payment. Manual review needed.",
                inline=False,
            )
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    if added < qty and price is not None and price > 0:
        await interaction.followup.send(
            f"⚠️ Added **{name}** ×{added}/{qty} to {player.display_name}'s inventory ({char_name}). "
            f"Payment was deducted but not all items were saved. Contact an admin for reconciliation.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"Added **{name}** ×{added} to {player.display_name}'s inventory ({char_name}).",
            ephemeral=True,
        )


class PlayerRemoveItemView(SafeView):
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

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
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
        await interaction.response.defer(ephemeral=True)
        player = self.selected_player
        items = await pi_get_by_owner(str(player.id))
        if not items:
            await interaction.followup.send(
                f"{player.display_name} has no items.", ephemeral=True
            )
            return
        grouped: dict[str, list[dict]] = {}
        for item in items:
            name = item.get("name", "?")
            if name not in grouped:
                grouped[name] = []
            grouped[name].append(item)
        options = []
        for name, group in sorted(grouped.items()):
            count = len(group)
            itype = group[0].get("item_type", "misc")
            label = f"{name} ×{count}" if count > 1 else name
            if len(label) > 100:
                label = label[:97] + "..."
            desc = f"Type: {itype}"
            options.append(discord.SelectOption(
                label=label, value=name, description=desc,
            ))
        if len(options) > 25:
            options = options[:25]
        step2 = RemoveItemPickerView(
            self.cog, self.ctx, player, grouped,
        )
        step2.item_dropdown.options = options
        await interaction.followup.send(
            f"**{player.display_name}**'s inventory — select the item to remove:",
            view=step2, ephemeral=True,
        )
        self.stop()


class RemoveItemPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context,
                 player: discord.Member, grouped: dict[str, list[dict]]):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.player = player
        self.grouped = grouped
        self.selected_name: Optional[str] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="Select item to remove…", row=0)
    async def item_dropdown(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_name = select.values[0]
        group = self.grouped.get(self.selected_name, [])
        count = len(group)
        if count > 1:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                f"**{self.selected_name}** — this player owns **{count}**. "
                f"How many to remove? (1-{count}, or type `cancel`):",
                ephemeral=True,
            )
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                return
            try:
                qty = int(text.strip())
            except ValueError:
                await interaction.followup.send("Invalid number.", ephemeral=True)
                return
            if qty < 1 or qty > count:
                await interaction.followup.send(
                    f"Quantity must be between 1 and {count}.", ephemeral=True
                )
                return
        else:
            qty = 1
            await interaction.response.defer(ephemeral=True)
        await self._do_remove(interaction, qty)

    async def _do_remove(self, interaction: discord.Interaction, qty: int):
        group = self.grouped.get(self.selected_name, [])
        to_remove = group[:qty]
        removed = 0
        for item in to_remove:
            item_id = item.get("item_id") or item.get("id", "")
            fresh = await pi_get_item(item_id)
            if fresh is None or fresh.get("owner_id") != str(self.player.id):
                continue
            ok = await pi_delete_item(item_id, expected_owner_id=str(self.player.id))
            if ok:
                removed += 1
                await ih_record_event(
                    item_id, "admin_remove",
                    actor_id=str(interaction.user.id),
                    target_id=str(self.player.id),
                    metadata={"item_name": self.selected_name},
                )
        count_str = f"×{removed}" if removed > 1 else ""
        await interaction.followup.send(
            f"Removed **{self.selected_name}** {count_str} from {self.player.display_name}."
            if removed > 0
            else f"Failed to remove **{self.selected_name}**.",
            ephemeral=True,
        )
        log_ch = await _audit_channel(self.cog.bot)
        if log_ch and removed > 0:
            embed = discord.Embed(
                title="🗑️ Fixer: Item Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
            embed.add_field(name="Player", value=f"{self.player.mention}", inline=False)
            embed.add_field(
                name="Item",
                value=f"**{self.selected_name}** {count_str}",
                inline=False,
            )
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


class LOAPickerView(SafeView):
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
        member = await _resolve_user_select(self.ctx, user)
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
            try:
                await member.add_roles(loa_role, reason=f"LOA start by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(f"❌ Could not assign LOA role: {e}", ephemeral=True)
                return
            await interaction.followup.send(
                f"✅ {member.display_name} is now on LOA.", ephemeral=True
            )
        else:
            if not has_role:
                await interaction.followup.send(
                    f"{member.display_name} is not currently on LOA.", ephemeral=True
                )
                return
            try:
                await member.remove_roles(loa_role, reason=f"LOA end by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(f"❌ Could not remove LOA role: {e}", ephemeral=True)
                return
            await interaction.followup.send(
                f"✅ {member.display_name}'s LOA has ended.", ephemeral=True
            )
        self.stop()


class WHRemoveGunPickerView(SafeView):
    def __init__(self, cog, ctx, options: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        select = discord.ui.Select(
            placeholder="Choose a gun lot to remove…",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        lot_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await _process_wh_remove_lot(self.cog, interaction, lot_id)


class WHRemoveCWPickerView(SafeView):
    def __init__(self, cog, ctx, options: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        select = discord.ui.Select(
            placeholder="Choose a CW lot to remove…",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        lot_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await _process_wh_remove_lot(self.cog, interaction, lot_id)


class WHRemoveQtyPickerView(SafeView):
    def __init__(self, cog, user_id: int, options: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        select = discord.ui.Select(
            placeholder="How many to remove?",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        lot_id, qty_str = value.rsplit(":", 1)
        remove_qty = int(qty_str)
        await interaction.response.defer(ephemeral=True)
        await _process_wh_remove_lot(self.cog, interaction, lot_id, remove_qty=remove_qty)


class FixerItemHistorySourceView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Player Item", style=discord.ButtonStyle.primary, emoji="👤", row=0)
    async def player_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        view = FixerItemHistoryPlayerPickerView(self.cog, self.ctx)
        await interaction.response.edit_message(
            content="📜 **Item History** — Select the player:",
            view=view,
        )

    @discord.ui.button(label="Store Item", style=discord.ButtonStyle.primary, emoji="🏪", row=0)
    async def store_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        view = FixerItemHistoryStorePickerView(self.cog, self.ctx)
        await interaction.response.edit_message(
            content="📜 **Item History** — Select the store owner:",
            view=view,
        )


class FixerItemHistoryPlayerPickerView(SafeView):
    def __init__(self, cog, ctx):
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
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await interaction.followup.send(f"{member.display_name} has no items.", ephemeral=True)
            return
        options = []
        for item in items[:25]:
            name = item.get("name", "?")
            itype = item.get("item_type", "misc")
            char = item.get("character_name", "—")
            iid = item.get("item_id", "?")
            label = f"{name} [{itype}]"[:100]
            desc = f"{char} — {iid[:8]}…"[:100]
            options.append(discord.SelectOption(label=label, value=iid, description=desc))
        self.stop()
        view = FixerItemHistoryItemPickerView(self.cog, self.ctx, options, member.display_name)
        await interaction.edit_original_response(
            content=f"📜 **{member.display_name}** — Select an item to view history:",
            view=view,
        )


class FixerItemHistoryStorePickerView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        owner = await _resolve_user_select(self.ctx, user)
        if not owner:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        options = []
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        if guns_cog:
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, owner.id)
            lots = state.get("stores", {}).get(store_id, {}).get("lots", [])
            for lot in lots:
                for iid in lot.get("item_ids", []):
                    name = lot.get("gun_name", "?")
                    label = f"🔫 {name}"[:100]
                    desc = f"${int(lot.get('unit_cost', 0)):,} — {iid[:8]}…"[:100]
                    options.append(discord.SelectOption(label=label, value=iid, description=desc))
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        if cw_cog:
            inventory = await cw_cog._load_inventory(owner.id)
            seen = set()
            for item in (inventory or []):
                iid = item.get("item_id", "")
                if iid and iid not in seen:
                    seen.add(iid)
                    name = item.get("name", "?")
                    label = f"💉 {name}"[:100]
                    desc = f"${int(item.get('price_paid', 0) or 0):,}"[:100]
                    options.append(discord.SelectOption(label=label, value=iid, description=desc))
        if not options:
            await interaction.followup.send(
                f"{owner.display_name}'s stores are empty.", ephemeral=True
            )
            return
        options = options[:25]
        self.stop()
        view = FixerItemHistoryItemPickerView(self.cog, self.ctx, options, owner.display_name)
        await interaction.edit_original_response(
            content=f"📜 **{owner.display_name}'s Store** — Select an item to view history:",
            view=view,
        )


class FixerItemHistoryItemPickerView(SafeView):
    def __init__(self, cog, ctx, options: list, owner_name: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.owner_name = owner_name
        select = discord.ui.Select(
            placeholder="Choose an item…",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        item_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        history = await ih_get_history(item_id, limit=50)
        if not history:
            await interaction.followup.send(f"No history for this item.", ephemeral=True)
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
        await interaction.followup.send(embed=embed, ephemeral=True)


class StoreOwnerPickerView(SafeView):
    def __init__(self, cog, ctx, options: list, store_type: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.store_type = store_type
        select = discord.ui.Select(
            placeholder="Choose a store owner…",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        owner_id = int(interaction.data["values"][0])
        guild = self.ctx.guild
        owner = guild.get_member(owner_id) if guild else None
        if not owner:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        if self.store_type == "gun":
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
                return
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, owner.id)
            store_data = state.get("stores", {}).get(store_id, {})
            lots = [
                l for l in store_data.get("lots", [])
                if l.get("qty_remaining", 0) > 0
            ]
            store_title = store_data.get("store_name") or f"{owner.display_name}'s Gun Store"
            if lots:
                lines = []
                for i, l in enumerate(lots[:25], 1):
                    r = l.get("restriction", "basic")
                    r_tag = f" [{r}]" if r != "basic" else ""
                    lines.append(
                        f"`{i}.` **{l['gun_name']}**{r_tag} — ${int(l['unit_cost']):,} × {l['qty_remaining']}"
                    )
                embed = discord.Embed(
                    title=f"🔫 {store_title}",
                    description="\n".join(lines),
                    color=discord.Color.dark_green(),
                )
                embed.set_footer(text=f"{len(lots)} lot(s)")
            else:
                embed = discord.Embed(
                    title=f"🔫 {store_title}",
                    description="This store is currently empty.",
                    color=discord.Color.dark_green(),
                )
            employees = store_data.get("employees", [])
            if employees:
                emp_mentions = [f"<@{uid}>" for uid in employees]
                embed.add_field(
                    name=f"👥 Employees ({len(employees)})",
                    value=", ".join(emp_mentions),
                    inline=False,
                )
        else:
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
                return
            state = await cw_cog._load_state()
            rd_stores = state.get("ripperdoc_stores", {})
            store_name = None
            guild_prefix = f"rd:{guild.id}:"
            for sid, s in rd_stores.items():
                if sid.startswith(guild_prefix) and s.get("owner_id") == owner.id and s.get("store_name"):
                    store_name = s["store_name"]
                    break
            if store_name:
                store_title = f"{store_name} (Owner: {owner.display_name})"
            else:
                store_title = f"{owner.display_name}'s Ripperdoc Stock"
            inventory = await cw_cog._load_inventory(owner.id)
            if inventory:
                groups = cw_cog._grouped_inventory(inventory)
                lines = []
                for i, g in enumerate(groups[:25], 1):
                    count_str = f" × {g['count']}" if g['count'] > 1 else ""
                    price_str = f"${g['price_paid']:,}" if g.get('price_paid') else "—"
                    lines.append(f"`{i}.` **{g['name']}**{count_str} — {price_str}")
                embed = discord.Embed(
                    title=f"💉 {store_title}",
                    description="\n".join(lines),
                    color=discord.Color.purple(),
                )
                embed.set_footer(text=f"{len(inventory)} item(s) in {len(groups)} slot(s)")
            else:
                embed = discord.Embed(
                    title=f"💉 {store_title}",
                    description="This store is currently empty.",
                    color=discord.Color.purple(),
                )

        action_view = StoreActionView(self.cog, self.ctx, owner, self.store_type)
        await interaction.followup.send(embed=embed, view=action_view, ephemeral=True)


class StoreActionView(SafeView):
    def __init__(self, cog, ctx, owner: discord.Member, store_type: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.owner = owner
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if self.store_type == "gun":
            await interaction.followup.send(
                f"📝 **Add to {self.owner.display_name}'s Gun Store**\n"
                "Enter: `gun name, quantity, unit cost, restriction`\n"
                "Example: `Militech Mk.31, 5, 5000, basic`\n"
                "• **Basic** — freely available, no restrictions\n"
                "• **Controlled** — requires a license or special authorization to purchase\n"
                "• **Restricted** — illegal or military-grade, requires admin approval for sale\n"
                "Type `cancel` to abort.",
                ephemeral=True,
            )
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                return
            await _process_store_add_gun(self.cog, interaction, self.owner, text)
        else:
            await interaction.followup.send(
                f"📝 **Add to {self.owner.display_name}'s Ripperdoc Store**\n"
                "Enter: `cyberware name, quantity, unit cost`\n"
                "Example: `Kiroshi Optics, 3, 8000`\n"
                "Type `cancel` to abort.",
                ephemeral=True,
            )
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                return
            await _process_store_add_cw(self.cog, interaction, self.owner, text)

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if self.store_type == "gun":
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
                return
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.owner.id)
            lots = [
                l for l in state.get("stores", {}).get(store_id, {}).get("lots", [])
                if l.get("qty_remaining", 0) > 0
            ]
            if not lots:
                await interaction.followup.send(
                    f"{self.owner.display_name}'s gun store is empty — nothing to remove.",
                    ephemeral=True,
                )
                return
            options = []
            for lot in lots[:25]:
                lid = lot.get("lot_id", "?")
                name = lot.get("gun_name", "?")
                r = lot.get("restriction", "basic")
                r_tag = f" [{r}]" if r != "basic" else ""
                label = f"{name}{r_tag}"[:100]
                desc = f"×{lot['qty_remaining']} — ${int(lot.get('unit_cost', 0)):,}"[:100]
                options.append(discord.SelectOption(label=label, value=lid, description=desc))
            view = StoreRemoveLotPickerView(
                self.cog, self.ctx, self.owner, options, store_type="gun"
            )
            await interaction.followup.send(
                f"🗑️ **Remove from {self.owner.display_name}'s Gun Store** — Select the lot:",
                view=view,
                ephemeral=True,
            )
        else:
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
                return
            inventory = await cw_cog._load_inventory(self.owner.id)
            if not inventory:
                await interaction.followup.send(
                    f"{self.owner.display_name}'s Ripperdoc stock is empty — nothing to remove.",
                    ephemeral=True,
                )
                return
            seen = set()
            options = []
            for item in inventory:
                iid = item.get("item_id", "")
                if iid and iid not in seen:
                    seen.add(iid)
                    name = item.get("name", "?")
                    label = f"{name}"[:100]
                    desc = f"${int(item.get('price_paid', 0) or 0):,}"[:100]
                    options.append(discord.SelectOption(label=label, value=iid, description=desc))
            options = options[:25]
            view = StoreRemoveLotPickerView(
                self.cog, self.ctx, self.owner, options, store_type="cw"
            )
            await interaction.followup.send(
                f"🗑️ **Remove from {self.owner.display_name}'s Ripperdoc Store** — Select the item:",
                view=view,
                ephemeral=True,
            )


class StoreRemoveLotPickerView(SafeView):
    def __init__(self, cog, ctx, owner: discord.Member, options: list, store_type: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.owner = owner
        self.store_type = store_type
        select = discord.ui.Select(
            placeholder="Choose an item to remove…",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        item_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        self.stop()
        if self.store_type == "gun":
            await _process_store_remove_gun(self.cog, interaction, self.owner, item_id)
        else:
            await _process_store_remove_cw(self.cog, interaction, self.owner, item_id)


async def _process_store_add_gun(cog, interaction, owner, text):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await interaction.followup.send("❌ Need at least: `gun name, quantity, unit cost`", ephemeral=True)
        return
    gun_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await interaction.followup.send("Quantity and cost must be numbers.", ephemeral=True)
        return
    if qty < 1 or cost < 0:
        await interaction.followup.send("Invalid quantity or cost.", ephemeral=True)
        return
    restriction = parts[3].strip().lower() if len(parts) > 3 else ""
    while restriction not in ("basic", "controlled", "restricted"):
        await interaction.followup.send(
            f"Got: **{gun_name}** ×{qty} at ${cost:,} — now enter the restriction level:\n"
            "`basic`, `controlled`, or `restricted`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        r_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if r_text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        restriction = r_text.strip().lower()
        if restriction not in ("basic", "controlled", "restricted"):
            await interaction.followup.send(
                "❌ Invalid restriction. Must be `basic`, `controlled`, or `restricted`. Try again.",
                ephemeral=True,
            )

    total_cost = cost * qty
    if cost > 0:
        confirm_view = _ConfirmItemView(target_user_id=owner.id)
        confirm_msg = await interaction.followup.send(
            f"💰 {owner.mention}, a Fixer wants to add **{gun_name}** ×{qty} at **${total_cost:,}** total to your store. Approve?",
            view=confirm_view,
            allowed_mentions=discord.AllowedMentions(users=[owner]),
        )
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await confirm_msg.edit(content="⏰ Confirmation timed out — cancelled.", view=None)
            except Exception:
                pass
            await interaction.followup.send("⏰ Store owner did not respond. Item not added.", ephemeral=True)
            return
        if confirm_view.result is False:
            await interaction.followup.send("❌ Store owner denied. Item not added.", ephemeral=True)
            return
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await interaction.followup.send("❌ Economy system unavailable.", ephemeral=True)
            return
        balance = await ub.get_balance(owner.id)
        if balance is None:
            await interaction.followup.send("❌ Could not fetch store owner's balance.", ephemeral=True)
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await interaction.followup.send(
                f"❌ Store owner cannot afford ${total_cost:,} (has ${cash + bank:,}).", ephemeral=True
            )
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            owner.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer store-add gun: {gun_name} x{qty}"
        )
        if not ok_deduct:
            await interaction.followup.send("❌ Failed to deduct funds. Item not added.", ephemeral=True)
            return

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
        saved = await guns_cog._save_state(state)
    if not saved and cost > 0:
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub:
            await ub.update_balance(
                owner.id, {"cash": cash_deducted, "bank": bank_deducted},
                reason=f"Fixer store-add gun refund: save failed for {gun_name} x{qty}"
            )
        await interaction.followup.send(
            f"❌ Failed to save store inventory. Funds have been refunded.", ephemeral=True
        )
        return
    await interaction.followup.send(
        f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] to {owner.display_name}'s store.",
        ephemeral=True,
    )


async def _process_store_add_cw(cog, interaction, owner, text):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await interaction.followup.send("❌ Need at least: `cyberware name, quantity, unit cost`", ephemeral=True)
        return
    item_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await interaction.followup.send("Quantity and cost must be numbers.", ephemeral=True)
        return
    if qty < 1 or cost < 0:
        await interaction.followup.send("Invalid quantity or cost.", ephemeral=True)
        return

    total_cost = cost * qty
    cash_deducted = 0
    bank_deducted = 0
    if cost > 0:
        confirm_view = _ConfirmItemView(target_user_id=owner.id)
        confirm_msg = await interaction.followup.send(
            f"💰 {owner.mention}, a Fixer wants to add **{item_name}** ×{qty} at **${total_cost:,}** total to your Ripperdoc store. Approve?",
            view=confirm_view,
            allowed_mentions=discord.AllowedMentions(users=[owner]),
        )
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await confirm_msg.edit(content="⏰ Confirmation timed out — cancelled.", view=None)
            except Exception:
                pass
            await interaction.followup.send("⏰ Store owner did not respond. Item not added.", ephemeral=True)
            return
        if confirm_view.result is False:
            await interaction.followup.send("❌ Store owner denied. Item not added.", ephemeral=True)
            return
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await interaction.followup.send("❌ Economy system unavailable.", ephemeral=True)
            return
        balance = await ub.get_balance(owner.id)
        if balance is None:
            await interaction.followup.send("❌ Could not fetch store owner's balance.", ephemeral=True)
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await interaction.followup.send(
                f"❌ Store owner cannot afford ${total_cost:,} (has ${cash + bank:,}).", ephemeral=True
            )
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            owner.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer store-add cyberware: {item_name} x{qty}"
        )
        if not ok_deduct:
            await interaction.followup.send("❌ Failed to deduct funds. Item not added.", ephemeral=True)
            return

    async with cw_cog._locks.acquire(str(owner.id)):
        inventory = await cw_cog._load_inventory(owner.id)
        for _ in range(qty):
            inventory.append({
                "item_id": str(uuid.uuid4()),
                "name": item_name,
                "price_paid": cost,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            })
        saved = await cw_cog._save_inventory(owner.id, inventory)
    if not saved and cost > 0:
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub:
            await ub.update_balance(
                owner.id, {"cash": cash_deducted, "bank": bank_deducted},
                reason=f"Fixer store-add CW refund: save failed for {item_name} x{qty}"
            )
        await interaction.followup.send(
            f"❌ Failed to save clinic inventory. Funds have been refunded.", ephemeral=True
        )
        return
    await interaction.followup.send(
        f"Added **{item_name}** ×{qty} at ${cost:,} to {owner.display_name}'s Ripperdoc store.",
        ephemeral=True,
    )


async def _process_store_remove_gun(cog, interaction, owner, lot_id):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
        return
    async with guns_cog.lock:
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(guild.id, owner.id)
        store = state.get("stores", {}).get(store_id)
        if not store:
            await interaction.followup.send("Store not found.", ephemeral=True)
            return
        lot = next((l for l in store.get("lots", []) if l.get("lot_id") == lot_id), None)
        if not lot:
            await interaction.followup.send(f"Lot not found in store.", ephemeral=True)
            return
        gun_name = lot.get("gun_name", "?")
        removed = int(lot.get("qty_remaining", 0))
        store["lots"].remove(lot)
        await guns_cog._save_state(state)
    await interaction.followup.send(
        f"Removed **{gun_name}** ×{removed} from {owner.display_name}'s store.", ephemeral=True
    )


async def _process_store_remove_cw(cog, interaction, owner, item_id):
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
        return
    async with cw_cog._locks.acquire(str(owner.id)):
        inventory = await cw_cog._load_inventory(owner.id)
        item = next((i for i in inventory if i.get("item_id") == item_id), None)
        if not item:
            await interaction.followup.send("Item not found in store.", ephemeral=True)
            return
        item_name = item.get("name", "?")
        inventory.remove(item)
        await cw_cog._save_inventory(owner.id, inventory)
    await interaction.followup.send(
        f"Removed **{item_name}** from {owner.display_name}'s Ripperdoc store.", ephemeral=True
    )

class FixerHubCog(commands.Cog, name="FixerHub"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_view = FixerTopView()
        bot.add_view(self._panel_view)

    @staticmethod
    def _panel_embed() -> discord.Embed:
        return discord.Embed(
            title="🛠️ Fixer Panel",
            description=(
                "Choose a category below.\n\n"
                "**Player** — Inventory, items, LOA, history\n"
                "**Store** — Gun store and Ripperdoc stock management\n"
                "**Wholesaler** — Wholesale inventory and restocking"
            ),
            color=discord.Color.dark_gold(),
        )

    @commands.hybrid_command(name="fixer")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fixer(self, ctx: commands.Context):
        """Post (or refresh) the persistent Fixer panel in the designated channel."""
        channel = self.bot.get_channel(config.FIXER_HUB_CHANNEL_ID)
        if channel is None:
            await ctx.send("❌ Fixer hub channel not found.", ephemeral=True)
            return
        view = FixerTopView()
        await channel.send(embed=self._panel_embed(), view=view)
        await ctx.send("✅ Fixer panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FixerHubCog(bot))
