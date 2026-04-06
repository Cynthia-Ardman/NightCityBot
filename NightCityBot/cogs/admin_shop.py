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
from NightCityBot.utils.interaction_safety import SafeView, send_ephemeral, respond_ephemeral, log_panel_failure
from NightCityBot.utils import helpers
from NightCityBot.utils.db import (
    pi_get_by_owner,
    ih_get_history,
    ih_record_event,
    cw_catalog_get_all,
    cw_catalog_upsert_many,
    gun_catalog_get_all,
    cw_shop_state_save,
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
            await respond_ephemeral(interaction, "Could not verify your role.")
            return False
        if not (any(r.id == config.FIXER_ROLE_ID for r in member.roles) or member.guild_permissions.administrator):
            await respond_ephemeral(interaction, "This panel is for Admins / Fixers only.")
            await log_panel_failure(interaction.client, "NIGHTCITYBOT_LOG_CHANNEL_ID", "Admin Panel", interaction.user, "Missing admin/fixer role")
            return False
        return True

    @discord.ui.button(label="Item History", style=discord.ButtonStyle.secondary, emoji="📜", row=0, custom_id="admin_shop:item_history")
    async def item_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = ItemHistorySourceView(cog, ctx)
        await send_ephemeral(interaction, 
            "📜 **Item History** — Where is the item?",
            view=view)

    @discord.ui.button(label="Player Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=0, custom_id="admin_shop:player_inv")
    async def player_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = PlayerInvPickerView(cog, ctx)
        await send_ephemeral(interaction, "Select a player to view their inventory:", view=view)

    @discord.ui.button(label="Seed Ripperdoc Stores", style=discord.ButtonStyle.success, emoji="🌱", row=1, custom_id="admin_shop:seed_ripperdoc")
    async def seed_ripperdoc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Admin shop system unavailable.")
            return
        await _seed_ripperdoc_stores(cog, interaction)

    @discord.ui.button(label="Seed Gun Shops", style=discord.ButtonStyle.success, emoji="🔫", row=1, custom_id="admin_shop:seed_gun_shops")
    async def seed_gun_shops(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Admin shop system unavailable.")
            return
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            await send_ephemeral(interaction, "❌ Gun shop system unavailable.")
            return
        state = await guns_cog._load_state()
        stores = state.get("stores", {})
        if not stores:
            await send_ephemeral(interaction, "❌ No gun stores are registered.")
            return
        guild = interaction.guild
        options = []
        for store_id, store_info in stores.items():
            owner_id = store_info.get("owner_id")
            if not owner_id:
                continue
            store_name = store_info.get("store_name") or f"Store {store_id}"
            owner_name = ""
            if guild:
                try:
                    m = guild.get_member(int(owner_id))
                    if m:
                        owner_name = m.display_name
                except (ValueError, TypeError):
                    pass
            has_stock = any(int(l.get("qty_remaining", 0)) > 0 for l in store_info.get("lots", []))
            status = "📦 Has stock" if has_stock else "🔲 Empty"
            options.append(discord.SelectOption(
                label=store_name[:100],
                value=store_id,
                description=f"{owner_name} — {status}"[:100] if owner_name else status,
            ))
            if len(options) >= 24:
                break
        if not options:
            await send_ephemeral(interaction, "❌ No gun stores found.")
            return
        options.insert(0, discord.SelectOption(
            label="All Empty Stores",
            value="__all_empty__",
            description="Seed only stores with no stock",
            emoji="📋",
        ))
        view = _SeedGunStorePickerView(cog, interaction.user.id, options)
        await send_ephemeral(interaction, "🔫 **Seed Gun Stores** — Pick a store to seed (replaces existing stock):", view=view)

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
                lines.append("\n**💉 Cyberware Wholesale:**")
                for i, lot in enumerate(available[:15], 1):
                    lines.append(
                        f"`{i}.` **{lot['item_name']}** — ${int(lot['unit_cost']):,} × {lot['qty_available']}"
                    )
            else:
                lines.append("**💉 Cyberware Wholesale:** Empty")
        if not lines:
            await send_ephemeral(interaction, "No wholesale systems available.")
            return
        embed = discord.Embed(
            title="🏭 Wholesale Stock Overview",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await send_ephemeral(interaction, embed=embed)

    @discord.ui.button(label="Restock Gun Wholesale", style=discord.ButtonStyle.primary, emoji="📥", row=2, custom_id="admin_shop:restock_wholesale")
    async def restock_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("AdminShop")
        catalog = await gun_catalog_get_all()
        if not catalog:
            await respond_ephemeral(interaction, "❌ Gun catalog is empty. Set a sheet and reload first.")
            return
        await respond_ephemeral(interaction, 
            f"📦 **Gun catalog has {len(catalog)} items.**\n"
            f"How many unique items to stock, and max qty per item?\n"
            f"Distribution: ~70% Basic, ~20% Controlled, ~10% Restricted\n"
            f"**Enter:** `total_items, max_qty`\n"
            f"Example: `10, 3` — stocks 10 random guns, up to 3 each\n"
            f"Type `cancel` to abort.")
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
        await send_ephemeral(interaction, 
            "⚠️ This will clear **all** gun wholesale lots. Are you sure?",
            view=confirm_view)

    @discord.ui.button(label="Restock Cyberware WH", style=discord.ButtonStyle.primary, emoji="💉", row=3, custom_id="admin_shop:restock_cw")
    async def restock_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("AdminShop")
        catalog = await cw_catalog_get_all()
        if not catalog:
            await respond_ephemeral(interaction, "❌ Cyberware catalog is empty. Set a sheet and reload first.")
            return
        await respond_ephemeral(interaction, 
            f"📦 **Cyberware catalog has {len(catalog)} items.**\n"
            f"How many unique items to stock, and max qty per item?\n"
            f"**Enter:** `total_items, max_qty`\n"
            f"Example: `5, 3` — stocks 5 random items, up to 3 each\n"
            f"Type `cancel` to abort.")
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await interaction.edit_original_response(content="⏰ Timed out or cancelled.")
            return
        await _inline_restock_cw(cog, interaction, text, catalog)

    @discord.ui.button(label="Clear Cyberware WH", style=discord.ButtonStyle.danger, emoji="🧹", row=3, custom_id="admin_shop:clear_cw_wholesale")
    async def clear_cw_wholesale(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        confirm_view = WholesaleClearConfirmView(cog, ctx, target="cw")
        await send_ephemeral(interaction, 
            "⚠️ This will clear **all** cyberware wholesale lots. Are you sure?",
            view=confirm_view)

    @discord.ui.button(label="Set Gun Sheet", style=discord.ButtonStyle.secondary, emoji="🔫", row=4, custom_id="admin_shop:set_gun_sheet")
    async def set_gun_sheet(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info("set_gun_sheet: clicked by %s", interaction.user.id)
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            await respond_ephemeral(interaction, "Gun shop system unavailable.")
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
        await respond_ephemeral(interaction, prompt)
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
        cog = interaction.client.get_cog("AdminShop")
        if cog:
            log_ch = await cog._audit_channel()
            if log_ch:
                try:
                    await log_ch.send(
                        f"🔫 **Admin: Gun Sheet URL Updated** — {interaction.user.display_name} ({interaction.user.id}) "
                        f"set gun catalog URL."
                    )
                except Exception:
                    pass

    @discord.ui.button(label="Set Cyberware Sheet", style=discord.ButtonStyle.secondary, emoji="💉", row=4, custom_id="admin_shop:set_cw_sheet")
    async def set_cw_sheet(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info("set_cw_sheet: clicked by %s", interaction.user.id)
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await respond_ephemeral(interaction, "Cyberware system unavailable.")
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
        await respond_ephemeral(interaction, prompt)
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
                db_ok = await cw_shop_state_save(cw_state)
                file_ok = await helpers.save_json_file(cw_cog.state_file, cw_state)
            if db_ok:
                await interaction.edit_original_response(content="✅ Cyberware sheet URL updated and saved.")
                cog = interaction.client.get_cog("AdminShop")
                if cog:
                    log_ch = await cog._audit_channel()
                    if log_ch:
                        try:
                            await log_ch.send(
                                f"💉 **Admin: Cyberware Sheet URL Updated** — {interaction.user.display_name} ({interaction.user.id}) "
                                f"set cyberware catalog URL."
                            )
                        except Exception:
                            pass
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
                    results.append("💉 Cyberware catalog — no sheet URL configured")
                else:
                    await download_sheet(sheet_url, cw_cog.sheet_cache_path)
                    items = parse_cyberware_sheet(cw_cog.sheet_cache_path)
                    if items:
                        await cw_catalog_upsert_many(items)
                        await cw_cog._save_catalog(items)
                    results.append(f"💉 Cyberware catalog reloaded — **{len(items)}** item(s)")
            except Exception as e:
                logger.warning("CW sheet reload failed", exc_info=True)
                results.append(f"💉 Cyberware catalog reload failed: {e}")
        else:
            results.append("💉 Cyberware system unavailable")
        await send_ephemeral(interaction, "\n".join(results))
        cog = interaction.client.get_cog("AdminShop")
        if cog:
            log_ch = await cog._audit_channel()
            if log_ch:
                try:
                    await log_ch.send(
                        f"🔄 **Admin: Sheets Reloaded** — {interaction.user.display_name} ({interaction.user.id}) "
                        f"reloaded catalogs.\n" + "\n".join(results)
                    )
                except Exception:
                    pass

    @discord.ui.button(label="Perm Overwrites", style=discord.ButtonStyle.secondary, emoji="🔐", row=4, custom_id="admin_shop:perm_overwrites")
    async def perm_overwrites(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await send_ephemeral(interaction, "Could not resolve the server.")
            return
        channels = sorted(guild.channels, key=lambda c: (c.position, c.name))
        total = 0
        per_channel = []
        for ch in channels:
            count = len(ch.overwrites)
            total += count
            if count > 0:
                kind = "📁" if isinstance(ch, discord.CategoryChannel) else "#"
                per_channel.append((count, kind, ch.name, ch.id))
        per_channel.sort(key=lambda x: -x[0])
        lines = [
            f"**Server total: {total} permission overwrites**",
            f"Channels with overwrites: {len(per_channel)} / {len(channels)}",
            "",
        ]
        top = per_channel[:30]
        for count, kind, name, cid in top:
            lines.append(f"`{count:>3}` {kind} **{name}**")
        if len(per_channel) > 30:
            rest = sum(c for c, *_ in per_channel[30:])
            lines.append(f"… and {len(per_channel) - 30} more channels ({rest} overwrites)")
        embed = discord.Embed(
            title="🔐 Permission Overwrites Audit",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Discord template limit: 500 total overwrites across all channels")
        await send_ephemeral(interaction, embed=embed)


class PlayerInvPickerView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a playeru2026", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await respond_ephemeral(interaction, "Could not resolve member.")
            return
        await interaction.response.defer(ephemeral=True)
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await send_ephemeral(interaction, f"{member.display_name} has no items.")
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
        await send_ephemeral(interaction, embed=embed)


class ItemHistorySourceView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
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
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await respond_ephemeral(interaction, "Could not resolve member.")
            return
        await interaction.response.defer(ephemeral=True)
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await send_ephemeral(interaction, f"{member.display_name} has no items.")
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
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        owner = await _resolve_user_select(self.ctx, user)
        if not owner:
            await respond_ephemeral(interaction, "Could not resolve member.")
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
            await send_ephemeral(interaction, 
                f"{owner.display_name}'s stores are empty.")
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
        super().__init__(timeout=300)
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
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        item_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await _inline_item_history(self.cog, interaction, item_id)


async def _inline_item_history(cog, interaction, item_id):
    history = await ih_get_history(item_id, limit=50)
    if not history:
        await send_ephemeral(interaction, content=f"No history for `{item_id}`.")
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
    await send_ephemeral(interaction, 
        embed=embed)


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
    catalog = [g for g in catalog if str(g.get("status", "live")).strip().lower() == "live"]
    if not catalog:
        await interaction.edit_original_response(content="No live guns in catalog.")
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
                "gun_category": gun.get("gun_category", ""),
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
        content=f"✅ Restocked **{len(stocked)}** cyberware items:\n{summary}",
    )
    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="📥 Admin: Cyberware Wholesale Restocked",
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Items", value=str(len(stocked)), inline=True)
        embed.add_field(name="Max Qty", value=str(max_qty), inline=True)
        embed.add_field(name="Details", value=summary[:1024], inline=False)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _seed_ripperdoc_stores(cog, interaction: discord.Interaction):
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await send_ephemeral(interaction, "❌ Cyberware system unavailable.")
        return

    catalog = await cw_catalog_get_all()
    if not catalog:
        await send_ephemeral(interaction, "❌ Cyberware catalog is empty. Set a sheet and reload first.")
        return

    catalog = [item for item in catalog if item.get("name")]
    if len(catalog) < 10:
        await send_ephemeral(interaction, f"❌ Cyberware catalog only has {len(catalog)} valid items — need at least 10 to seed stores.")
        return

    async with cw_cog.lock:
        state = await cw_cog._load_state()
        stores = state.get("ripperdoc_stores", {})

        if not stores:
            await send_ephemeral(interaction, "❌ No ripperdoc stores are registered.")
            return

        seeded_summary: list[str] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for store_id, store_info in stores.items():
            owner_id = store_info.get("owner_id")
            if not owner_id:
                continue
            inventory = await cw_cog._load_inventory(owner_id)
            if inventory:
                continue

            chosen = random.sample(catalog, 10)
            new_items = []
            item_names = []
            for item in chosen:
                name = item["name"]
                new_entry = {
                    "item_id": str(uuid.uuid4()),
                    "name": name,
                    "price_paid": 0,
                    "cwp": item.get("cwp", ""),
                    "slot": item.get("slot", ""),
                    "purchased_at": now_iso,
                }
                new_items.append(new_entry)
                item_names.append(name)

            if not new_items:
                continue
            await cw_cog._save_inventory(owner_id, new_items)

            store_name = store_info.get("store_name") or f"Store {owner_id}"
            items_list = ", ".join(item_names)
            seeded_summary.append(f"**{store_name}** (owner <@{owner_id}>): {len(new_items)} items\n> {items_list}")

            guild = interaction.guild
            if guild:
                try:
                    member = guild.get_member(int(owner_id))
                    if member is None:
                        member = await guild.fetch_member(int(owner_id))
                    if member:
                        item_list_dm = "\n".join(f"• {n}" for n in item_names)
                        await member.send(
                            f"🌱 **Your ripperdoc store has been stocked!**\n\n"
                            f"An admin seeded **{store_name}** with {len(new_items)} starter items:\n"
                            f"{item_list_dm}\n\n"
                            f"Head to your Ripperdoc Hub to check your inventory."
                        )
                except Exception:
                    pass

            for seeded_item in new_items:
                tx = {
                    "tx_id": str(uuid.uuid4()),
                    "tx_type": "ADMIN_SEED",
                    "ts": now_iso,
                    "ripperdoc_id": str(owner_id),
                    "admin_id": str(interaction.user.id),
                    "admin_name": interaction.user.display_name,
                    "item": seeded_item["name"],
                    "item_id": seeded_item["item_id"],
                    "price": 0,
                    "qty": 1,
                }
                await cw_cog._append_tx(tx)

    if not seeded_summary:
        await send_ephemeral(interaction, "✅ All ripperdoc stores already have inventory — nothing to seed.")
        return

    summary_text = "\n".join(seeded_summary)
    msg = f"🌱 **Seeded {len(seeded_summary)} ripperdoc store(s):**\n{summary_text}"
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await send_ephemeral(interaction, msg)

    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="🌱 Admin: Ripperdoc Stores Seeded",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Stores Seeded", value=str(len(seeded_summary)), inline=True)
        embed.add_field(name="Items Per Store", value="10", inline=True)
        embed.add_field(name="Details", value=summary_text[:1024], inline=False)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class _SeedGunStorePickerView(SafeView):
    def __init__(self, cog, admin_id: int, options: list):
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_id = admin_id
        select = discord.ui.Select(placeholder="Choose a store…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await _seed_gun_stores(self.cog, interaction, target_store_id=choice)


async def _seed_gun_stores(cog, interaction: discord.Interaction, *, target_store_id: str = "__all_empty__"):
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await send_ephemeral(interaction, "❌ Gun shop system unavailable.")
        return

    catalog = await gun_catalog_get_all()
    if not catalog:
        await send_ephemeral(interaction, "❌ Gun catalog is empty. Set a sheet and reload first.")
        return

    catalog = [
        g for g in catalog
        if g.get("gun_name") and str(g.get("status", "live")).strip().lower() == "live"
    ]
    if len(catalog) < 10:
        await send_ephemeral(interaction, f"❌ Gun catalog only has {len(catalog)} live items — need at least 10 to seed stores.")
        return

    all_empty = target_store_id == "__all_empty__"

    async with guns_cog.lock:
        state = await guns_cog._load_state()
        stores = state.get("stores", {})

        if not stores:
            await send_ephemeral(interaction, "❌ No gun stores are registered.")
            return

        seeded_summary: list[str] = []

        for store_id, store_info in stores.items():
            if not all_empty and store_id != target_store_id:
                continue
            owner_id = store_info.get("owner_id")
            if not owner_id:
                continue
            if all_empty:
                has_stock = any(int(l.get("qty_remaining", 0)) > 0 for l in store_info.get("lots", []))
                if has_stock:
                    continue

            store_info["lots"] = []

            chosen = _pick_guns_by_restriction(catalog, 10)
            new_lots = []
            gun_names = []
            all_item_ids = []
            for gun in chosen:
                qty = random.randint(1, 3)
                cost = gun.get("price", 0) or 0
                restriction = gun.get("restriction", "basic")
                lot_id = f"seed-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
                item_ids = [str(uuid.uuid4()) for _ in range(qty)]
                new_lots.append({
                    "lot_id": lot_id,
                    "gun_name": gun["gun_name"],
                    "gun_level": gun.get("gun_level", "L"),
                    "weapon_type": gun.get("weapon_type", ""),
                    "gun_category": gun.get("gun_category", ""),
                    "unit_cost": cost,
                    "qty_remaining": qty,
                    "restriction": restriction,
                    "item_ids": item_ids,
                })
                r_tag = f" [{restriction}]" if restriction != "basic" else ""
                gun_names.append(f"{gun['gun_name']}{r_tag} ×{qty}")
                all_item_ids.extend([(iid, gun["gun_name"], gun.get("gun_level"), lot_id) for iid in item_ids])

            if not new_lots:
                continue
            store_info["lots"] = new_lots

            store_name = store_info.get("store_name") or f"Store {store_id}"
            items_list = ", ".join(gun_names)
            seeded_summary.append(f"**{store_name}** (owner <@{owner_id}>): {len(new_lots)} gun types\n> {items_list}")

            guild = interaction.guild
            if guild:
                try:
                    member = guild.get_member(int(owner_id))
                    if member is None:
                        member = await guild.fetch_member(int(owner_id))
                    if member:
                        item_list_dm = "\n".join(f"• {n}" for n in gun_names)
                        action = "restocked" if not all_empty else "stocked"
                        await member.send(
                            f"🔫 **Your gun store has been {action}!**\n\n"
                            f"An admin seeded **{store_name}** with {len(new_lots)} starter guns:\n"
                            f"{item_list_dm}\n\n"
                            f"Head to your Gun Store Hub to check your inventory."
                        )
                except Exception:
                    pass

            for item_id, gun_name, gun_level, lot_id in all_item_ids:
                await ih_record_event(
                    item_id, "admin_seed",
                    actor_id=str(interaction.user.id),
                    price=0,
                    metadata={
                        "gun_name": gun_name,
                        "gun_level": gun_level,
                        "lot_id": lot_id,
                        "store_id": store_id,
                    },
                )

        await guns_cog._save_state(state)

    if not seeded_summary:
        if all_empty:
            await send_ephemeral(interaction, "✅ All gun stores already have inventory — nothing to seed.")
        else:
            await send_ephemeral(interaction, "❌ Could not find that store to seed.")
        return

    summary_text = "\n".join(seeded_summary)
    msg = f"🔫 **Seeded {len(seeded_summary)} gun store(s):**\n{summary_text}"
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await send_ephemeral(interaction, msg)

    log_ch = await cog._audit_channel()
    if log_ch:
        embed = discord.Embed(
            title="🔫 Admin: Gun Stores Seeded",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Admin", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Stores Seeded", value=str(len(seeded_summary)), inline=True)
        embed.add_field(name="Items Per Store", value="10 types", inline=True)
        mode = "specific store" if not all_empty else "all empty stores"
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="Details", value=summary_text[:1024], inline=False)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class WholesaleClearConfirmView(SafeView):
    def __init__(self, cog: "AdminShopCog", ctx: commands.Context, target: str = "guns"):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
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
            label = "Cyberware"
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
                "**Item History** — Browse a player/store item's audit trail\n"
                "**Player Inventory** — Browse a player's items\n"
                "**Wholesale Stock** — View gun + cyberware wholesale inventory\n"
                "**Restock Wholesale** — Add guns to wholesale\n"
                "**Clear Gun WH** — Remove all gun wholesale lots\n"
                "**Restock Cyberware** — Stock random cyberware from catalog\n"
                "**Clear Cyberware WH** — Remove all cyberware wholesale lots\n"
                "**Set Gun/Cyberware Sheet** — Set Google Sheet URL for catalogs\n"
                "**Reload Sheets** — Re-download and refresh both catalogs"
            ),
            color=discord.Color.orange(),
        )

    @staticmethod
    def _guide_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📘 Admin Panel — How It Works",
            description=(
                "This panel gives admins full control over the shop systems, wholesale markets, and item tracking. "
                "All responses are private and **auto-delete after 5 minutes**."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="📜 Item History",
            value="Look up the full audit trail for any item — see every purchase, transfer, and sale it's been through.",
            inline=False,
        )
        embed.add_field(
            name="📦 Player Inventory",
            value="Browse any player's current inventory by selecting their name.",
            inline=False,
        )
        embed.add_field(
            name="🌱 Seed Ripperdoc Stores / 🔫 Seed Gun Shops",
            value="Fill empty stores with 10 random starter items from the catalog. Stores that already have stock are skipped. Owners receive a DM notification.",
            inline=False,
        )
        embed.add_field(
            name="🏭 Wholesale Stock",
            value="View all current gun and cyberware lots available in wholesale.",
            inline=False,
        )
        embed.add_field(
            name="📥 Restock Gun / 💉 Restock Cyberware Wholesale",
            value="Randomly populate the wholesale market with new lots pulled from the master catalogs (Google Sheets).",
            inline=False,
        )
        embed.add_field(
            name="🗑️ Clear Gun / 🧹 Clear Cyberware Wholesale",
            value="Wipe all current wholesale lots. Useful before a fresh restock.",
            inline=False,
        )
        embed.add_field(
            name="🔫 Set Gun Sheet / 💉 Set Cyberware Sheet",
            value="Update the Google Sheets URL that the bot reads gun or cyberware catalogs from.",
            inline=False,
        )
        embed.add_field(
            name="🔄 Reload Sheets",
            value="Re-download and refresh both the gun and cyberware catalogs from Google Sheets.",
            inline=False,
        )
        return embed

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
        await channel.send(embed=self._guide_embed(), view=view)
        await ctx.send("✅ Admin Shop panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminShopCog(bot))
