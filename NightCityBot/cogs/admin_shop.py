"""Unified !admin panel — admin operations for the shop system.

Provides a single interactive panel for Fixers/admins to manage inventory,
look up item history, add/remove items, and view audit trails.
"""
import logging
import re
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView
from NightCityBot.utils.db import (
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
    cw_catalog_upsert_many,
    gun_catalog_upsert_many,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active, get_character_by_name
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.inline_helpers import collect_text_input
from NightCityBot.utils.panel_context import PanelContext
from NightCityBot.services.cyberware_shop_data import download_sheet, parse_cyberware_sheet

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
            await interaction.response.send_message("This panel is for Admins / Fixers only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reassign Item", style=discord.ButtonStyle.secondary, emoji="✏️", row=0, custom_id="admin_shop:reassign_item")
    async def reassign_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        await interaction.followup.send(
            "📝 **Enter:** `item_uuid, new_owner_mention_or_id, new_character_name`\n"
            "Example: `12345678-abcd-..., @Player, V`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _inline_reassign_item(cog, interaction, text)

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=1, custom_id="admin_shop:item_history")
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        await interaction.followup.send(
            "📝 **Enter the Item UUID** to look up (or type `cancel`):",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _inline_item_history(cog, interaction, text.strip())

    @discord.ui.button(label="Player Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=1, custom_id="admin_shop:player_inv")
    async def player_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = PlayerInvPickerView(cog, ctx)
        await interaction.followup.send("Select a player to view their inventory:", view=view, ephemeral=True)

    @discord.ui.button(label="Wholesale Stock", style=discord.ButtonStyle.secondary, emoji="🏭", row=2, custom_id="admin_shop:wholesale_stock")
    async def wholesale_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = interaction.client.get_cog("GunsShopCog")
        cw_cog = interaction.client.get_cog("CyberwareShop")
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

    @discord.ui.button(label="Restock Wholesale", style=discord.ButtonStyle.primary, emoji="📥", row=2, custom_id="admin_shop:restock_wholesale")
    async def restock_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        await interaction.followup.send(
            "📝 **Enter:** `gun_name, quantity, unit_cost, restriction`\n"
            "Restriction is optional (defaults to `basic`).\n"
            "Example: `Militech M-76e, 10, 5000, controlled`\n"
            "Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _inline_restock_wholesale(cog, interaction, text)

    @discord.ui.button(label="Clear Gun WH", style=discord.ButtonStyle.danger, emoji="🗑️", row=2, custom_id="admin_shop:clear_wholesale")
    async def clear_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        confirm_view = WholesaleClearConfirmView(cog, ctx, target="guns")
        await interaction.followup.send(
            "⚠️ This will clear **all** gun wholesale lots. Are you sure?",
            view=confirm_view,
            ephemeral=True,
        )

    @discord.ui.button(label="Restock CW", style=discord.ButtonStyle.primary, emoji="💉", row=3, custom_id="admin_shop:restock_cw")
    async def restock_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        catalog = await cw_catalog_get_all()
        if not catalog:
            await interaction.followup.send("❌ CW catalog is empty. Set a sheet and reload first.", ephemeral=True)
            return
        await interaction.followup.send(
            f"📦 **CW catalog has {len(catalog)} items.**\n"
            f"How many unique items to stock, and max qty per item?\n"
            f"**Enter:** `total_items, max_qty`\n"
            f"Example: `5, 3` — stocks 5 random items, up to 3 each\n"
            f"Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        await _inline_restock_cw(cog, interaction, text, catalog)

    @discord.ui.button(label="Clear CW WH", style=discord.ButtonStyle.danger, emoji="🧹", row=3, custom_id="admin_shop:clear_cw_wholesale")
    async def clear_cw_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        confirm_view = WholesaleClearConfirmView(cog, ctx, target="cw")
        await interaction.followup.send(
            "⚠️ This will clear **all** cyberware wholesale lots. Are you sure?",
            view=confirm_view,
            ephemeral=True,
        )

    @discord.ui.button(label="Set Gun Sheet", style=discord.ButtonStyle.secondary, emoji="🔫", row=4, custom_id="admin_shop:set_gun_sheet")
    async def set_gun_sheet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
            return
        state = await guns_cog._load_state()
        current = str(state.get("settings", {}).get("master_sheet_url", "")).strip()
        prompt = "📝 **Paste the Google Sheets URL** for the gun catalog"
        if current:
            prompt += f"\nCurrent: `{current[:80]}{'…' if len(current) > 80 else ''}`"
        prompt += "\nType `cancel` to abort."
        await interaction.followup.send(prompt, ephemeral=True)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        url = text.strip().strip("<>")
        if not url.startswith(("http://", "https://")):
            await interaction.followup.send("❌ Invalid URL.", ephemeral=True)
            return
        normalized = guns_cog._normalize_sheet_source_url(url)
        async with guns_cog.lock:
            latest = await guns_cog._load_state()
            latest.setdefault("settings", {})["master_sheet_url"] = normalized
            await guns_cog._save_state(latest)
        await interaction.followup.send(f"✅ Gun sheet URL updated.", ephemeral=True)

    @discord.ui.button(label="Set CW Sheet", style=discord.ButtonStyle.secondary, emoji="💉", row=4, custom_id="admin_shop:set_cw_sheet")
    async def set_cw_sheet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
            return
        state = await cw_cog._load_state()
        current = str(state.get("sheet_url", "")).strip()
        prompt = "📝 **Paste the Google Sheets URL** for the cyberware catalog"
        if current:
            prompt += f"\nCurrent: `{current[:80]}{'…' if len(current) > 80 else ''}`"
        prompt += "\nType `cancel` to abort."
        await interaction.followup.send(prompt, ephemeral=True)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            return
        url = text.strip().strip("<>")
        if not url.startswith(("http://", "https://")):
            await interaction.followup.send("❌ Invalid URL.", ephemeral=True)
            return
        async with cw_cog.lock:
            cw_state = await cw_cog._load_state()
            cw_state["sheet_url"] = url
            await cw_cog._save_state(cw_state)
        await interaction.followup.send(f"✅ Cyberware sheet URL updated.", ephemeral=True)

    @discord.ui.button(label="Reload Sheets", style=discord.ButtonStyle.success, emoji="🔄", row=4, custom_id="admin_shop:reload_sheets")
    async def reload_sheets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        results = []
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if guns_cog:
            try:
                guns = await guns_cog._load_master_guns()
                results.append(f"🔫 Gun catalog reloaded — **{len(guns)}** item(s)")
            except Exception as e:
                logger.warning("Gun sheet reload failed", exc_info=True)
                results.append(f"🔫 Gun catalog reload failed: {e}")
        else:
            results.append("🔫 Gun shop system unavailable")
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog:
            try:
                cw_state = await cw_cog._load_state()
                sheet_url = str(cw_state.get("sheet_url", "")).strip()
                if not sheet_url:
                    results.append("💉 CW catalog — no sheet URL configured")
                else:
                    await download_sheet(sheet_url, cw_cog.sheet_cache_path)
                    items = parse_cyberware_sheet(cw_cog.sheet_cache_path)
                    if items:
                        await cw_catalog_upsert_many(items)
                        await cw_cog._save_catalog(items)
                    results.append(f"💉 CW catalog reloaded — **{len(items)}** item(s)")
            except Exception as e:
                logger.warning("CW sheet reload failed", exc_info=True)
                results.append(f"💉 CW catalog reload failed: {e}")
        else:
            results.append("💉 Cyberware system unavailable")
        await interaction.followup.send("\n".join(results), ephemeral=True)


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


async def _inline_reassign_item(cog, interaction, text):
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
    new_owner = await cog._resolve_member(guild, raw_owner)
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
        item_id, "admin_reassign",
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
        f"Reassigned **{item_name}** to {new_owner.display_name} — {new_char_name}.",
        ephemeral=True,
    )
    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="✏️ Admin: Item Reassigned",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Item", value=f"**{item_name}** (`{item_id}`)", inline=False)
        embed.add_field(name="Old", value=f"<@{old_owner_id}> — {old_char}", inline=True)
        embed.add_field(name="New", value=f"{new_owner.mention} — {new_char_name}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _inline_item_history(cog, interaction, item_id):
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


async def _inline_restock_wholesale(cog, interaction, text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await interaction.followup.send(
            "❌ Please provide at least: `gun_name, quantity, unit_cost`",
            ephemeral=True,
        )
        return
    gun_name = parts[0]
    if not gun_name:
        await interaction.followup.send("❌ Gun name is required.", ephemeral=True)
        return
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await interaction.followup.send("Quantity and cost must be numbers.", ephemeral=True)
        return
    if qty < 1 or cost < 0:
        await interaction.followup.send("Invalid quantity or cost.", ephemeral=True)
        return
    restriction = parts[3].strip().lower() if len(parts) > 3 and parts[3].strip() else "basic"
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await interaction.followup.send("Gun shop system unavailable.", ephemeral=True)
        return
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
    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="📥 Admin: Wholesale Restocked",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Gun", value=gun_name, inline=True)
        embed.add_field(name="Qty", value=str(qty), inline=True)
        embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
        embed.add_field(name="Restriction", value=restriction, inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _inline_restock_cw(cog, interaction, text, catalog):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2:
        await interaction.followup.send(
            "❌ Please provide: `total_items, max_qty`",
            ephemeral=True,
        )
        return
    try:
        total_items = int(parts[0])
        max_qty = int(parts[1])
    except ValueError:
        await interaction.followup.send("Both values must be numbers.", ephemeral=True)
        return
    if total_items < 1:
        await interaction.followup.send("Total items must be at least 1.", ephemeral=True)
        return
    if max_qty < 1:
        await interaction.followup.send("Max qty must be at least 1.", ephemeral=True)
        return
    total_items = min(total_items, len(catalog))
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.followup.send("Cyberware system unavailable.", ephemeral=True)
        return
    chosen = random.sample(catalog, total_items)
    async with cw_cog.lock:
        state = await cw_cog._load_state()
        lots = state.setdefault("cw_wholesale_lots", [])
        stocked = []
        for item in chosen:
            qty = random.randint(1, max_qty)
            cost = item.get("price", 0) or 0
            lot_id = f"admin-cw-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            lots.append({
                "lot_id": lot_id,
                "item_name": item["name"],
                "unit_cost": cost,
                "qty_available": qty,
            })
            stocked.append(f"**{item['name']}** ×{qty} at ${cost:,}")
        await cw_cog._save_state(state)
    summary = "\n".join(stocked)
    await interaction.followup.send(
        f"✅ Restocked **{len(stocked)}** CW items:\n{summary}", ephemeral=True
    )
    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="📥 Admin: CW Wholesale Restocked",
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Items", value=str(len(stocked)), inline=True)
        embed.add_field(name="Max Qty", value=str(max_qty), inline=True)
        embed.add_field(name="Details", value=summary[:1024], inline=False)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WholesaleClearConfirmView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context, target: str = "guns"):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        self.cancel_clear.disabled = True
        await interaction.response.edit_message(view=self)
        if self.target == "cw":
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await interaction.edit_original_response(content="Cyberware system unavailable.", view=None)
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
                await interaction.edit_original_response(content="Gun shop system unavailable.", view=None)
                self.stop()
                return
            async with guns_cog.lock:
                state = await guns_cog._load_state()
                state["wholesale_lots"] = []
                await guns_cog._save_state(state)
            label = "Gun"

        await interaction.edit_original_response(content=f"✅ All {label} wholesale lots cleared.", view=None)
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
        self._panel_view = AdminShopMenuView()
        bot.add_view(self._panel_view)

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

    @staticmethod
    def _panel_embed() -> discord.Embed:
        return discord.Embed(
            title="🔧 Admin Shop Panel",
            description=(
                "Choose an admin action below.\n\n"
                "**Reassign** — Transfer/reassign an item\n"
                "**Item History** — Look up audit trail by UUID\n"
                "**Player Inventory** — Browse a player's items\n"
                "**Wholesale Stock** — View gun + CW wholesale inventory\n"
                "**Restock Wholesale** — Add guns to wholesale\n"
                "**Clear Gun WH** — Remove all gun wholesale lots\n"
                "**Restock CW** — Stock random CW from catalog\n"
                "**Clear CW WH** — Remove all CW wholesale lots\n"
                "**Set Gun/CW Sheet** — Set Google Sheet URL for catalogs\n"
                "**Reload Sheets** — Re-download and refresh both catalogs"
            ),
            color=discord.Color.orange(),
        )

    @commands.hybrid_command(name="admin", aliases=["admin_shop"])
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def admin_shop(self, ctx: commands.Context):
        """Post (or refresh) the persistent Admin Shop panel in the designated channel."""
        channel = self.bot.get_channel(config.ADMIN_HUB_CHANNEL_ID)
        if channel is None:
            await ctx.send("❌ Admin hub channel not found.", ephemeral=True)
            return
        view = AdminShopMenuView()
        await channel.send(embed=self._panel_embed(), view=view)
        await ctx.send("✅ Admin Shop panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminShopCog(bot))
