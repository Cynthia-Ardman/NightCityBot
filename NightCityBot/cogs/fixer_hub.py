"""Unified !fixer hub — interactive panel for Fixer-level management.

Three top-level categories: Player, Store, Wholesaler.
Each opens a sub-menu with relevant actions.
"""
import logging
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
                "**Reassign Item** — Transfer item to new owner/character\n"
                "**Item History** — Audit trail for an item UUID\n"
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
                "**View Gun Store** — Inspect a store owner's gun inventory\n"
                "**View CW Store** — Inspect a Ripperdoc's cyberware stock\n"
                "**Add to Gun Store** — Add a gun lot to a store\n"
                "**Remove from Gun Store** — Remove a gun lot from a store"
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
                "**View Stock** — See current gun + CW wholesale inventory\n"
                "**Add Gun** — Add a gun lot to wholesale\n"
                "**Add CW** — Add a cyberware lot to wholesale\n"
                "**Remove Lot** — Remove a specific lot by ID\n"
                "**Restock Guns** — Full weekly gun restock\n"
                "**Restock CW** — Full weekly CW restock"
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
        await interaction.followup.send(
            "📝 **Enter:** `item_uuid, new_owner_mention_or_id, new_character_name`\n"
            "Example: `12345678-abcd-..., @Player, V`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_fixer_reassign_item(self.cog, interaction, text)

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=1)
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter the Item UUID** to look up (or type `cancel`):",
            ephemeral=True,
        )
        item_id = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if item_id is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
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

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=3)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)


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

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)


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
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter gun wholesale details** in this format:\n"
            "`gun name, quantity, unit cost, restriction`\n"
            "Example: `Militech Mk.31, 10, 5000, basic`\n"
            "Restriction is optional (defaults to `basic`). Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_wh_add_gun(self.cog, interaction, text)

    @discord.ui.button(label="Add CW", style=discord.ButtonStyle.primary, emoji="💉", row=0)
    async def add_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter CW wholesale details** in this format:\n"
            "`cyberware name, quantity, unit cost`\n"
            "Example: `Neural Link, 10, 5000`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_wh_add_cw(self.cog, interaction, text)

    @discord.ui.button(label="Remove Lot", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_lot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter lot removal details** in this format:\n"
            "`lot ID, quantity to remove`\n"
            "Leave quantity blank to remove entire lot.\n"
            "Example: `fixer-20250403-abc123, 5` or `fixer-20250403-abc123`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_wh_remove_lot(self.cog, interaction, text)

    @discord.ui.button(label="Restock Guns", style=discord.ButtonStyle.success, emoji="📥", row=1)
    async def restock_guns(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "🔫 **Full gun restock** pulls from the master sheet and applies restock settings.\n"
            "Use the **Admin Hub** (`!admin`) → Restock to trigger it.\n\n"
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

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)


async def _process_fixer_reassign_item(cog, interaction, text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await interaction.followup.send(
            "❌ Please provide: `item_uuid, new_owner_mention_or_id, new_character_name`",
            ephemeral=True,
        )
        return
    item_id = parts[0]
    raw_owner = parts[1]
    new_char_name = parts[2]
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    item = await pi_get_item(item_id)
    if item is None:
        await interaction.followup.send(f"Item `{item_id}` not found.", ephemeral=True)
        return
    new_owner = await _resolve_member(guild, raw_owner)
    if not new_owner:
        await interaction.followup.send("Could not find new owner.", ephemeral=True)
        return
    char_record = await get_character_by_name(str(new_owner.id), new_char_name)
    if char_record and not await ensure_character_active(char_record["character_id"]):
        await interaction.followup.send(
            f"❌ Character **{new_char_name}** is not active.", ephemeral=True
        )
        return
    item_name = item.get("name", "?")
    old_owner_id = item.get("owner_id", "")
    old_char = item.get("character_name", "")
    if str(new_owner.id) == old_owner_id:
        ok = await pi_update_character(item_id, new_char_name, expected_owner_id=old_owner_id)
    else:
        ok = await pi_update_owner(item_id, str(new_owner.id), new_char_name, old_owner_id)
    if not ok:
        await interaction.followup.send("Failed to reassign item.", ephemeral=True)
        return
    await ih_record_event(
        item_id, "fixer_reassign",
        actor_id=str(interaction.user.id),
        target_id=str(new_owner.id),
        metadata={
            "item_name": item_name,
            "old_owner": old_owner_id,
            "old_character": old_char,
            "new_character": new_char_name,
        },
    )
    await interaction.followup.send(
        f"✅ Reassigned **{item_name}** to {new_owner.display_name} — {new_char_name}.",
        ephemeral=True,
    )
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        embed = discord.Embed(
            title="✏️ Fixer: Item Reassigned",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
        embed.add_field(name="Old", value=f"<@{old_owner_id}> — {old_char}", inline=True)
        embed.add_field(name="New", value=f"{new_owner.mention} — {new_char_name}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _process_wh_add_gun(cog, interaction, text):
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
    restriction = parts[3].strip().lower() if len(parts) > 3 else "basic"
    if restriction not in ("basic", "controlled", "restricted"):
        restriction = "basic"
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


async def _process_wh_add_cw(cog, interaction, text):
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


async def _process_wh_remove_lot(cog, interaction, text):
    parts = [p.strip() for p in text.split(",")]
    lot_id = parts[0]
    raw_qty = parts[1].strip() if len(parts) > 1 else ""

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
        if not await ensure_character_active(self.selected_character["character_id"]):
            await interaction.response.send_message(
                f"❌ Character **{self.selected_character['name']}** is no longer active.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter item details** in this format:\n"
            "`item name, type, quantity, price`\n"
            "Example: `Militech Pistol, gun, 1, 5000`\n"
            "Type and price are optional (defaults: `misc`, no price). Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            self.stop()
            return
        await _process_fixer_add_item(
            self.cog, interaction, self.selected_player, self.selected_character, text
        )
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
    if qty < 1:
        qty = 1
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
                metadata={"item_name": name, "character": char_name, "item_type": item_type},
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
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
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
        await interaction.followup.send(
            "📝 **Enter the Item UUID** to remove (or type `cancel`):",
            ephemeral=True,
        )
        item_id = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if item_id is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        player = self.selected_player

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
        fresh = await pi_get_item(item_id)
        if fresh is None or fresh.get("owner_id") != str(player.id):
            await interaction.followup.send(
                f"Item `{item_id}` was modified before removal. Please try again.", ephemeral=True
            )
            return
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


class StoreInvPickerView(SafeView):
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
        owner = await _resolve_user_select(self.ctx, user)
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


class StoreAddPickerView(SafeView):
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
        member = await _resolve_user_select(self.ctx, user)
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
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter gun store details** in this format:\n"
            "`gun name, quantity, unit cost, restriction`\n"
            "Example: `Militech Mk.31, 5, 5000, basic`\n"
            "Restriction is optional (defaults to `basic`). Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_store_add(self.cog, interaction, self.selected_owner, text)
        self.stop()


async def _process_store_add(cog, interaction, owner, text):
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
    restriction = parts[3].strip().lower() if len(parts) > 3 else "basic"
    if restriction not in ("basic", "controlled", "restricted"):
        restriction = "basic"
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

class StoreRemovePickerView(SafeView):
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
        member = await _resolve_user_select(self.ctx, user)
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
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter lot removal details** in this format:\n"
            "`lot ID, quantity to remove`\n"
            "Leave quantity blank to remove entire lot.\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(self.cog.bot, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _process_store_remove(self.cog, interaction, self.selected_owner, text)
        self.stop()


async def _process_store_remove(cog, interaction, owner, text):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
        return
    parts = [p.strip() for p in text.split(",")]
    lot_id = parts[0]
    raw_qty = parts[1].strip() if len(parts) > 1 else ""
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
