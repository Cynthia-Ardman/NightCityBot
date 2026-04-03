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
from NightCityBot.utils import helpers
from NightCityBot.utils.db import (
    pi_get_by_owner,
    ih_get_history,
    cw_catalog_get_all,
    cw_catalog_upsert_many,
    gun_catalog_get_all,
    db_save,
)
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

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=0, custom_id="admin_shop:item_history")
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = ItemHistorySourceView(cog, ctx)
        await interaction.followup.send(
            "📜 **Item History** — Where is the item?",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Player Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=0, custom_id="admin_shop:player_inv")
    async def player_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = PlayerInvPickerView(cog, ctx)
        await interaction.followup.send("Select a player to view their inventory:", view=view, ephemeral=True)

    @discord.ui.button(label="Wholesale Stock", style=discord.ButtonStyle.secondary, emoji="🏭", row=1, custom_id="admin_shop:wholesale_stock")
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

    @discord.ui.button(label="Restock Gun Wholesale", style=discord.ButtonStyle.primary, emoji="📥", row=2, custom_id="admin_shop:restock_wholesale")
    async def restock_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("AdminShop")
        catalog = await gun_catalog_get_all()
        if not catalog:
            await interaction.response.send_message("❌ Gun catalog is empty. Set a sheet and reload first.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"📦 **Gun catalog has {len(catalog)} items.**\n"
            f"How many unique items to stock, and max qty per item?\n"
            f"Distribution: ~70% Basic, ~20% Controlled, ~10% Restricted\n"
            f"**Enter:** `total_items, max_qty`\n"
            f"Example: `10, 3` — stocks 10 random guns, up to 3 each\n"
            f"Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.edit_original_response(content="⏰ Timed out or cancelled.")
            return
        await _inline_restock_wholesale(cog, interaction, text, catalog)

    @discord.ui.button(label="Clear Gun Wholesale", style=discord.ButtonStyle.danger, emoji="🗑️", row=2, custom_id="admin_shop:clear_wholesale")
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

    @discord.ui.button(label="Restock CW Wholesale", style=discord.ButtonStyle.primary, emoji="💉", row=3, custom_id="admin_shop:restock_cw")
    async def restock_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("AdminShop")
        catalog = await cw_catalog_get_all()
        if not catalog:
            await interaction.response.send_message("❌ CW catalog is empty. Set a sheet and reload first.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"📦 **CW catalog has {len(catalog)} items.**\n"
            f"How many unique items to stock, and max qty per item?\n"
            f"**Enter:** `total_items, max_qty`\n"
            f"Example: `5, 3` — stocks 5 random items, up to 3 each\n"
            f"Type `cancel` to abort.",
            ephemeral=True,
        )
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.edit_original_response(content="⏰ Timed out or cancelled.")
            return
        await _inline_restock_cw(cog, interaction, text, catalog)

    @discord.ui.button(label="Clear CW Wholesale", style=discord.ButtonStyle.danger, emoji="🧹", row=3, custom_id="admin_shop:clear_cw_wholesale")
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
        logger.info("set_gun_sheet: clicked by %s", interaction.user.id)
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            await interaction.response.send_message("Gun shop system unavailable.", ephemeral=True)
            return
        try:
            state = await guns_cog._load_state()
        except Exception:
            logger.exception("set_gun_sheet: failed to load gun state")
            state = {}
        current = str(state.get("settings", {}).get("master_sheet_url", "")).strip()
        prompt = "📝 **Paste the Google Sheets URL** for the gun catalog"
        if current:
            prompt += f"\nCurrent: `{current[:80]}{'…' if len(current) > 80 else ''}`"
        prompt += "\nType `cancel` to abort."
        await interaction.response.send_message(prompt, ephemeral=True)
        logger.info("set_gun_sheet: prompt shown, waiting for text input in channel %s", interaction.channel_id)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.edit_original_response(content="⏰ Timed out or cancelled.")
            return
        url = text.strip().strip("<>")
        if not url.startswith(("http://", "https://")):
            await interaction.edit_original_response(content="❌ Invalid URL.")
            return
        normalized = guns_cog._normalize_sheet_source_url(url)
        async with guns_cog.lock:
            latest = await guns_cog._load_state()
            latest.setdefault("settings", {})["master_sheet_url"] = normalized
            await guns_cog._save_state(latest)
        await interaction.edit_original_response(content="✅ Gun sheet URL updated.")

    @discord.ui.button(label="Set CW Sheet", style=discord.ButtonStyle.secondary, emoji="💉", row=4, custom_id="admin_shop:set_cw_sheet")
    async def set_cw_sheet(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info("set_cw_sheet: clicked by %s", interaction.user.id)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.response.send_message("Cyberware system unavailable.", ephemeral=True)
            return
        try:
            state = await cw_cog._load_state()
        except Exception:
            logger.exception("set_cw_sheet: failed to load CW state")
            state = {}
        current = str(state.get("sheet_url", "")).strip()
        prompt = "📝 **Paste the Google Sheets URL** for the cyberware catalog"
        if current:
            prompt += f"\nCurrent: `{current[:80]}{'…' if len(current) > 80 else ''}`"
        prompt += "\nType `cancel` to abort."
        await interaction.response.send_message(prompt, ephemeral=True)
        logger.info("set_cw_sheet: prompt shown, waiting for text input in channel %s", interaction.channel_id)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.edit_original_response(content="⏰ Timed out or cancelled.")
            return
        url = text.strip().strip("<>")
        if not url.startswith(("http://", "https://")):
            await interaction.edit_original_response(content="❌ Invalid URL.")
            return
        try:
            async with cw_cog.lock:
                cw_state = await cw_cog._load_state()
                cw_state["sheet_url"] = url
                db_ok = await db_save("cw_shop_state", cw_state)
                file_ok = await helpers.save_json_file(cw_cog.state_file, cw_state)
            if db_ok:
                await interaction.edit_original_response(content="✅ Cyberware sheet URL updated and saved.")
            elif file_ok:
                await interaction.edit_original_response(content="⚠️ URL saved to file only — database write failed. It may not survive a full rebuild.")
            else:
                await interaction.edit_original_response(content="❌ Failed to save sheet URL. Please try again.")
        except Exception:
            logger.exception("set_cw_sheet: failed to save CW state")
            await interaction.edit_original_response(content="❌ Error saving sheet URL. Check logs.")

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


class ItemHistorySourceView(SafeView):
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
        view = ItemHistoryPlayerPickerView(self.cog, self.ctx)
        await interaction.response.edit_message(
            content="📜 **Item History** — Select the player:",
            view=view,
        )

    @discord.ui.button(label="Store Item", style=discord.ButtonStyle.primary, emoji="🏪", row=0)
    async def store_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        view = ItemHistoryStorePickerView(self.cog, self.ctx)
        await interaction.response.edit_message(
            content="📜 **Item History** — Select the store owner:",
            view=view,
        )


class ItemHistoryPlayerPickerView(SafeView):
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
        view = ItemHistoryItemPickerView(self.cog, self.ctx, options, member.display_name)
        await interaction.edit_original_response(
            content=f"📜 **{member.display_name}** — Select an item to view history:",
            view=view,
        )


class ItemHistoryStorePickerView(SafeView):
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
        guns_cog = interaction.client.get_cog("GunsShopCog")
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
        cw_cog = interaction.client.get_cog("CyberwareShop")
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
        view = ItemHistoryItemPickerView(self.cog, self.ctx, options, owner.display_name)
        await interaction.edit_original_response(
            content=f"📜 **{owner.display_name}'s Store** — Select an item to view history:",
            view=view,
        )


class ItemHistoryItemPickerView(SafeView):
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
        await _inline_item_history(self.cog, interaction, item_id)


async def _inline_item_history(cog, interaction, item_id):
    history = await ih_get_history(item_id, limit=50)
    if not history:
        await interaction.followup.send(content=f"No history for `{item_id}`.", ephemeral=True)
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
    )


def _pick_guns_by_restriction(catalog, total_items):
    basic = [g for g in catalog if g.get("restriction", "basic").lower() == "basic"]
    controlled = [g for g in catalog if g.get("restriction", "basic").lower() == "controlled"]
    restricted = [g for g in catalog if g.get("restriction", "basic").lower() == "restricted"]

    n_basic = max(1, round(total_items * 0.70)) if basic else 0
    n_controlled = max(1, round(total_items * 0.20)) if controlled else 0
    n_restricted = max(1, round(total_items * 0.10)) if restricted else 0

    n_basic = min(n_basic, len(basic))
    n_controlled = min(n_controlled, len(controlled))
    n_restricted = min(n_restricted, len(restricted))

    total_picked = n_basic + n_controlled + n_restricted
    if total_picked > total_items:
        overshoot = total_picked - total_items
        for _ in range(overshoot):
            if n_basic > 1:
                n_basic -= 1
            elif n_controlled > 1:
                n_controlled -= 1
            elif n_restricted > 1:
                n_restricted -= 1

    chosen = []
    if n_basic:
        chosen.extend(random.sample(basic, n_basic))
    if n_controlled:
        chosen.extend(random.sample(controlled, n_controlled))
    if n_restricted:
        chosen.extend(random.sample(restricted, n_restricted))
    return chosen


async def _inline_restock_wholesale(cog, interaction, text, catalog):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2:
        await interaction.edit_original_response(
            content="❌ Please provide: `total_items, max_qty`",
        )
        return
    try:
        total_items = int(parts[0])
        max_qty = int(parts[1])
    except ValueError:
        await interaction.edit_original_response(content="Both values must be numbers.")
        return
    if total_items < 1:
        await interaction.edit_original_response(content="Total items must be at least 1.")
        return
    if max_qty < 1:
        await interaction.edit_original_response(content="Max qty must be at least 1.")
        return
    total_items = min(total_items, len(catalog))
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await interaction.edit_original_response(content="Gun shop system unavailable.")
        return
    chosen = _pick_guns_by_restriction(catalog, total_items)
    async with guns_cog.lock:
        state = await guns_cog._load_state()
        lots = state.setdefault("wholesale_lots", [])
        stocked = []
        for gun in chosen:
            qty = random.randint(1, max_qty)
            cost = gun.get("price", 0) or 0
            restriction = gun.get("restriction", "basic")
            lot_id = f"admin-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            lots.append({
                "lot_id": lot_id,
                "gun_name": gun["gun_name"],
                "gun_level": gun.get("gun_level", "L"),
                "weapon_type": gun.get("weapon_type", ""),
                "unit_cost": cost,
                "qty_available": qty,
                "restriction": restriction,
            })
            r_tag = f" [{restriction}]" if restriction != "basic" else ""
            stocked.append(f"**{gun['gun_name']}**{r_tag} ×{qty} at ${cost:,}")
        await guns_cog._save_state(state)
    summary = "\n".join(stocked)
    await interaction.edit_original_response(
        content=f"✅ Restocked **{len(stocked)}** guns:\n{summary}",
    )
    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="📥 Admin: Gun Wholesale Restocked",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Items", value=str(len(stocked)), inline=True)
        embed.add_field(name="Max Qty", value=str(max_qty), inline=True)
        embed.add_field(name="Details", value=summary[:1024], inline=False)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _inline_restock_cw(cog, interaction, text, catalog):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2:
        await interaction.edit_original_response(
            content="❌ Please provide: `total_items, max_qty`",
        )
        return
    try:
        total_items = int(parts[0])
        max_qty = int(parts[1])
    except ValueError:
        await interaction.edit_original_response(content="Both values must be numbers.")
        return
    if total_items < 1:
        await interaction.edit_original_response(content="Total items must be at least 1.")
        return
    if max_qty < 1:
        await interaction.edit_original_response(content="Max qty must be at least 1.")
        return
    total_items = min(total_items, len(catalog))
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await interaction.edit_original_response(content="Cyberware system unavailable.")
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
    await interaction.edit_original_response(
        content=f"✅ Restocked **{len(stocked)}** CW items:\n{summary}",
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
                "**Item History** — Browse a player/store item's audit trail\n"
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
