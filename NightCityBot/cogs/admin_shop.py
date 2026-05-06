"""Unified !admin panel — admin operations for the shop system.

Provides a single interactive panel for Fixers/admins to manage inventory,
look up item history, add/remove items, and view audit trails.
"""
import logging
import re
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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
    balance_history_get,
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

    @discord.ui.button(label="Manage Stores", style=discord.ButtonStyle.success, emoji="🏪", row=1, custom_id="admin_shop:manage_stores")
    async def manage_stores(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = _ManageStoresTypeView(interaction.user.id)
        await send_ephemeral(interaction, "🏪 **Manage Stores** — Which type?", view=view)

    @discord.ui.button(label="Set Gun Sheet", style=discord.ButtonStyle.secondary, emoji="🔫", row=1, custom_id="admin_shop:set_gun_sheet")
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

    @discord.ui.button(label="Set Cyberware Sheet", style=discord.ButtonStyle.secondary, emoji="💉", row=1, custom_id="admin_shop:set_cw_sheet")
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

    @discord.ui.button(label="Reload Sheets", style=discord.ButtonStyle.success, emoji="🔄", row=2, custom_id="admin_shop:reload_sheets")
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
            try:
                async with guns_cog.lock:
                    state = await guns_cog._load_state()
                    raw_lots = state.get("wholesale_lots", []) or []
                    kept = [
                        l for l in raw_lots
                        if isinstance(l, dict)
                        and str(l.get("lot_id", "")).startswith("fixer-")
                    ]
                    removed = len(raw_lots) - len(kept)
                    if removed > 0:
                        state["wholesale_lots"] = kept
                        await guns_cog._save_state(state)
                        results.append(f"🔫 Cleared **{removed}** non-custom gun lot(s) from wholesale")
            except Exception as e:
                logger.warning("Gun wholesale legacy purge failed", exc_info=True)
                results.append(f"🔫 Gun wholesale legacy purge failed: {e}")
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
            try:
                async with cw_cog.lock:
                    state = await cw_cog._load_state()
                    raw_lots = state.get("cw_wholesale_lots", []) or []
                    kept = [
                        l for l in raw_lots
                        if isinstance(l, dict)
                        and str(l.get("lot_id", "")).startswith("fixer-cw-")
                    ]
                    removed = len(raw_lots) - len(kept)
                    if removed > 0:
                        state["cw_wholesale_lots"] = kept
                        await cw_cog._save_state(state)
                        results.append(f"💉 Cleared **{removed}** non-custom cyberware lot(s) from wholesale")
            except Exception as e:
                logger.warning("CW wholesale legacy purge failed", exc_info=True)
                results.append(f"💉 Cyberware wholesale legacy purge failed: {e}")
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


    @discord.ui.button(label="Balance History", style=discord.ButtonStyle.secondary, row=3, custom_id="admin_shop:balance_history")
    async def balance_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        ctx = PanelContext(interaction)
        view = BalanceHistoryPickerView(cog, ctx)
        await send_ephemeral(
            interaction,
            "**Balance History** — Pick a player to see the last 30 days of balance changes:",
            view=view,
        )

    @discord.ui.button(label="Perm Overwrites", style=discord.ButtonStyle.secondary, emoji="🔐", row=3, custom_id="admin_shop:perm_overwrites")
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


async def _seed_ripperdoc_stores(cog, interaction: discord.Interaction, *, target_store_id: str = "__all_empty__"):
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

    all_empty = target_store_id == "__all_empty__"

    async with cw_cog.lock:
        state = await cw_cog._load_state()
        stores = state.get("ripperdoc_stores", {})

        if not stores:
            await send_ephemeral(interaction, "❌ No ripperdoc stores are registered.")
            return

        if not all_empty and target_store_id not in stores:
            await send_ephemeral(interaction, "❌ Store not found.")
            return

        seeded_summary: list[str] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        stores_to_seed = stores.items() if all_empty else [(target_store_id, stores[target_store_id])]

        for store_id, store_info in stores_to_seed:
            owner_id = store_info.get("owner_id")
            if not owner_id:
                continue
            inventory = await cw_cog._load_inventory(owner_id)
            if all_empty and inventory:
                continue
            if not all_empty and inventory:
                await cw_cog._save_inventory(owner_id, [])

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


class _ManageStoresTypeView(SafeView):
    def __init__(self, admin_id: int):
        super().__init__(timeout=300)
        self.admin_id = admin_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Gun Stores", style=discord.ButtonStyle.primary, emoji="🔫", row=0)
    async def gun_stores(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = _StoreManagementMenuView(self.admin_id, store_type="gun")
        await send_ephemeral(interaction, "🔫 **Gun Store Management**", view=view)

    @discord.ui.button(label="Ripperdoc Stores", style=discord.ButtonStyle.primary, emoji="💉", row=0)
    async def ripperdoc_stores(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = _StoreManagementMenuView(self.admin_id, store_type="ripperdoc")
        await send_ephemeral(interaction, "💉 **Ripperdoc Store Management**", view=view)


class _StoreManagementMenuView(SafeView):
    def __init__(self, admin_id: int, store_type: str):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Create Store", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = _AdminCreateStoreOwnerSelect(self.admin_id, self.store_type)
        emoji = "🔫" if self.store_type == "gun" else "💉"
        label = "Gun" if self.store_type == "gun" else "Ripperdoc"
        await send_ephemeral(interaction, f"{emoji} **Create {label} Store** — Select the owner:", view=view)

    @discord.ui.button(label="Seed Stores", style=discord.ButtonStyle.primary, emoji="🌱", row=0)
    async def seed_stores(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("AdminShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Admin shop system unavailable.")
            return
        if self.store_type == "gun":
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
            view = _SeedGunStorePickerView(cog, self.admin_id, options)
            await send_ephemeral(interaction, "🔫 **Seed Gun Stores** — Pick a store to seed (replaces existing stock):", view=view)
        else:
            cw_cog = interaction.client.get_cog("CyberwareShop")
            if not cw_cog:
                await send_ephemeral(interaction, "❌ Cyberware system unavailable.")
                return
            state = await cw_cog._load_state()
            stores = state.get("ripperdoc_stores", {})
            if not stores:
                await send_ephemeral(interaction, "❌ No ripperdoc stores are registered.")
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
                inventory = await cw_cog._load_inventory(owner_id)
                has_stock = bool(inventory)
                status = "📦 Has stock" if has_stock else "🔲 Empty"
                options.append(discord.SelectOption(
                    label=store_name[:100],
                    value=store_id,
                    description=f"{owner_name} — {status}"[:100] if owner_name else status,
                ))
                if len(options) >= 24:
                    break
            if not options:
                await send_ephemeral(interaction, "❌ No ripperdoc stores found.")
                return
            options.insert(0, discord.SelectOption(
                label="All Empty Stores",
                value="__all_empty__",
                description="Seed only stores with no stock",
                emoji="📋",
            ))
            view = _SeedRipperdocStorePickerView(cog, self.admin_id, options)
            await send_ephemeral(interaction, "💉 **Seed Ripperdoc Stores** — Pick a store to seed (replaces existing stock):", view=view)

    @discord.ui.button(label="Manage Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=1)
    async def manage_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        options = await _build_store_options(interaction, self.store_type)
        if not options:
            await send_ephemeral(interaction, "❌ No stores found.")
            return
        view = _AdminStorePickerView(self.admin_id, options, action="employees", store_type=self.store_type)
        emoji = "🔫" if self.store_type == "gun" else "💉"
        await send_ephemeral(interaction, f"{emoji} **Manage Employees** — Select a store:", view=view)

    @discord.ui.button(label="Change Store Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        options = await _build_store_options(interaction, self.store_type)
        if not options:
            await send_ephemeral(interaction, "❌ No stores found.")
            return
        view = _AdminStorePickerView(self.admin_id, options, action="rename", store_type=self.store_type)
        emoji = "🔫" if self.store_type == "gun" else "💉"
        await send_ephemeral(interaction, f"{emoji} **Change Store Name** — Select a store:", view=view)


async def _build_store_options(interaction: discord.Interaction, store_type: str) -> list:
    guild = interaction.guild
    options = []
    if store_type == "gun":
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            return []
        state = await guns_cog._load_state()
        stores = state.get("stores", {})
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
            emp_count = len(store_info.get("employees", []))
            desc = f"{owner_name} — {emp_count} employee(s)" if owner_name else f"{emp_count} employee(s)"
            options.append(discord.SelectOption(
                label=store_name[:100],
                value=store_id,
                description=desc[:100],
            ))
            if len(options) >= 25:
                break
    else:
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            return []
        state = await cw_cog._load_state()
        stores = state.get("ripperdoc_stores", {})
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
            emp_count = len(store_info.get("employees", []))
            desc = f"{owner_name} — {emp_count} employee(s)" if owner_name else f"{emp_count} employee(s)"
            options.append(discord.SelectOption(
                label=store_name[:100],
                value=store_id,
                description=desc[:100],
            ))
            if len(options) >= 25:
                break
    return options


class _AdminStorePickerView(SafeView):
    def __init__(self, admin_id: int, options: list, action: str, store_type: str):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.action = action
        self.store_type = store_type
        select = discord.ui.Select(placeholder="Choose a store…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        store_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        if self.action == "employees":
            view = _AdminEmployeeActionView(self.admin_id, store_id, self.store_type)
            await send_ephemeral(interaction, "👥 **Manage Employees** — What would you like to do?", view=view)
        elif self.action == "rename":
            await _admin_rename_store(interaction, store_id, self.store_type)


class _AdminEmployeeActionView(SafeView):
    def __init__(self, admin_id: int, store_id: str, store_type: str):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.store_id = store_id
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Add Employee", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = _AdminAddEmployeeSelect(self.admin_id, self.store_id, self.store_type)
        await send_ephemeral(interaction, "👤 **Add Employee** — Select a member:", view=view)

    @discord.ui.button(label="Remove Employee", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_employee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        employees = await _get_store_employees(interaction, self.store_id, self.store_type)
        if not employees:
            await send_ephemeral(interaction, "This store has no employees.")
            return
        guild = interaction.guild
        options = []
        for emp_id in employees[:25]:
            name = str(emp_id)
            if guild:
                m = guild.get_member(int(emp_id))
                if m:
                    name = m.display_name
            options.append(discord.SelectOption(label=name[:100], value=str(emp_id)))
        view = _AdminRemoveEmployeeSelect(self.admin_id, self.store_id, self.store_type, options)
        await send_ephemeral(interaction, "👤 **Remove Employee** — Select who to remove:", view=view)

    @discord.ui.button(label="View Employees", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_employees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        employees = await _get_store_employees(interaction, self.store_id, self.store_type)
        if not employees:
            await send_ephemeral(interaction, "This store has no employees.")
            return
        lines = [f"• <@{eid}>" for eid in employees]
        await send_ephemeral(interaction,
            f"👥 **Employees** ({len(employees)}):\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none())


async def _get_store_employees(interaction: discord.Interaction, store_id: str, store_type: str) -> list:
    if store_type == "gun":
        cog = interaction.client.get_cog("GunsShopCog")
        if not cog:
            return []
        state = await cog._load_state()
        store = state.get("stores", {}).get(store_id, {})
        return store.get("employees", [])
    else:
        cog = interaction.client.get_cog("CyberwareShop")
        if not cog:
            return []
        state = await cog._load_state()
        store = state.get("ripperdoc_stores", {}).get(store_id, {})
        return store.get("employees", [])


class _AdminAddEmployeeSelect(SafeView):
    def __init__(self, admin_id: int, store_id: str, store_type: str):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.store_id = store_id
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a member…", row=0)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        member = select.values[0] if select.values else None
        if not member:
            await send_ephemeral(interaction, "No member selected.")
            return
        guild = interaction.guild
        if guild and not isinstance(member, discord.Member):
            member = guild.get_member(member.id)
        if not member or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "Could not resolve that member in this server.")
            return
        if member.bot:
            await send_ephemeral(interaction, "Cannot add a bot as an employee.")
            return
        await _admin_add_employee(interaction, self.store_id, self.store_type, member)


class _AdminRemoveEmployeeSelect(SafeView):
    def __init__(self, admin_id: int, store_id: str, store_type: str, options: list):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.store_id = store_id
        self.store_type = store_type
        select = discord.ui.Select(placeholder="Choose an employee…", options=options, row=0)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        emp_id = int(interaction.data["values"][0])
        await interaction.response.defer(ephemeral=True)
        await _admin_remove_employee(interaction, self.store_id, self.store_type, emp_id)


async def _admin_add_employee(interaction: discord.Interaction, store_id: str, store_type: str, member: discord.Member):
    guild = interaction.guild
    if not guild:
        await send_ephemeral(interaction, "❌ Must be used in a server.")
        return

    if store_type == "gun":
        cog = interaction.client.get_cog("GunsShopCog")
        if not cog:
            await send_ephemeral(interaction, "❌ Gun shop system unavailable.")
            return
        async with cog.lock:
            state = await cog._load_state()
            store = state.get("stores", {}).get(store_id)
            if not store:
                await send_ephemeral(interaction, "❌ Store not found.")
                return
            employees = store.setdefault("employees", [])
            if member.id in employees:
                await send_ephemeral(interaction, f"❌ {member.display_name} is already an employee.")
                return
            if len(employees) >= 25:
                await send_ephemeral(interaction, "❌ Store has reached the 25-employee limit.")
                return
            employees.append(member.id)
            await cog._save_state(state)
        emp_role = guild.get_role(config.GUN_STORE_EMPLOYEE_ROLE_ID) if hasattr(config, "GUN_STORE_EMPLOYEE_ROLE_ID") and config.GUN_STORE_EMPLOYEE_ROLE_ID else None
        store_name = store.get("store_name", store_id)
    else:
        cog = interaction.client.get_cog("CyberwareShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Cyberware system unavailable.")
            return
        async with cog.lock:
            state = await cog._load_state()
            store = state.get("ripperdoc_stores", {}).get(store_id)
            if not store:
                await send_ephemeral(interaction, "❌ Store not found.")
                return
            employees = store.setdefault("employees", [])
            if member.id in employees:
                await send_ephemeral(interaction, f"❌ {member.display_name} is already an employee.")
                return
            if len(employees) >= 25:
                await send_ephemeral(interaction, "❌ Store has reached the 25-employee limit.")
                return
            employees.append(member.id)
            await cog._save_state(state)
        emp_role = guild.get_role(config.RIPPERDOC_EMPLOYEE_ROLE_ID) if hasattr(config, "RIPPERDOC_EMPLOYEE_ROLE_ID") and config.RIPPERDOC_EMPLOYEE_ROLE_ID else None
        store_name = store.get("store_name", store_id)

    if emp_role:
        try:
            await member.add_roles(emp_role, reason=f"Admin added employee to {store_name}")
        except Exception:
            pass

    try:
        await send_ephemeral(interaction, f"✅ Added **{member.display_name}** as an employee at **{store_name}**.")
    except discord.NotFound:
        pass

    admin_cog = interaction.client.get_cog("AdminShop")
    if admin_cog:
        log_ch = await admin_cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="👥 Admin: Employee Added",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
            embed.add_field(name="Employee", value=member.mention, inline=True)
            embed.add_field(name="Store", value=store_name, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            try:
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass


async def _admin_remove_employee(interaction: discord.Interaction, store_id: str, store_type: str, emp_id: int):
    guild = interaction.guild
    if not guild:
        await send_ephemeral(interaction, "❌ Must be used in a server.")
        return

    if store_type == "gun":
        cog = interaction.client.get_cog("GunsShopCog")
        if not cog:
            await send_ephemeral(interaction, "❌ Gun shop system unavailable.")
            return
        async with cog.lock:
            state = await cog._load_state()
            store = state.get("stores", {}).get(store_id)
            if not store:
                await send_ephemeral(interaction, "❌ Store not found.")
                return
            employees = store.get("employees", [])
            if emp_id not in employees:
                await send_ephemeral(interaction, "❌ That user is not an employee.")
                return
            employees.remove(emp_id)
            await cog._save_state(state)
        emp_role = guild.get_role(config.GUN_STORE_EMPLOYEE_ROLE_ID) if hasattr(config, "GUN_STORE_EMPLOYEE_ROLE_ID") and config.GUN_STORE_EMPLOYEE_ROLE_ID else None
        still_employed = any(emp_id in s.get("employees", []) for s in state.get("stores", {}).values())
        store_name = store.get("store_name", store_id)
    else:
        cog = interaction.client.get_cog("CyberwareShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Cyberware system unavailable.")
            return
        async with cog.lock:
            state = await cog._load_state()
            store = state.get("ripperdoc_stores", {}).get(store_id)
            if not store:
                await send_ephemeral(interaction, "❌ Store not found.")
                return
            employees = store.get("employees", [])
            if emp_id not in employees:
                await send_ephemeral(interaction, "❌ That user is not an employee.")
                return
            employees.remove(emp_id)
            await cog._save_state(state)
        emp_role = guild.get_role(config.RIPPERDOC_EMPLOYEE_ROLE_ID) if hasattr(config, "RIPPERDOC_EMPLOYEE_ROLE_ID") and config.RIPPERDOC_EMPLOYEE_ROLE_ID else None
        still_employed = any(emp_id in s.get("employees", []) for sid, s in state.get("ripperdoc_stores", {}).items())
        store_name = store.get("store_name", store_id)

    if emp_role and not still_employed:
        member = guild.get_member(emp_id)
        if member:
            try:
                await member.remove_roles(emp_role, reason=f"Admin removed employee from {store_name}")
            except Exception:
                pass

    member = guild.get_member(emp_id)
    emp_display = member.display_name if member else str(emp_id)
    try:
        await send_ephemeral(interaction, f"✅ Removed **{emp_display}** from **{store_name}**.")
    except discord.NotFound:
        pass

    admin_cog = interaction.client.get_cog("AdminShop")
    if admin_cog:
        log_ch = await admin_cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="👥 Admin: Employee Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
            embed.add_field(name="Employee", value=f"<@{emp_id}>", inline=True)
            embed.add_field(name="Store", value=store_name, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            try:
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass


async def _admin_rename_store(interaction: discord.Interaction, store_id: str, store_type: str):
    if store_type == "gun":
        cog = interaction.client.get_cog("GunsShopCog")
        if not cog:
            await send_ephemeral(interaction, "❌ Gun shop system unavailable.")
            return
        state = await cog._load_state()
        store = state.get("stores", {}).get(store_id, {})
    else:
        cog = interaction.client.get_cog("CyberwareShop")
        if not cog:
            await send_ephemeral(interaction, "❌ Cyberware system unavailable.")
            return
        state = await cog._load_state()
        store = state.get("ripperdoc_stores", {}).get(store_id, {})

    old_name = store.get("store_name", store_id)
    await send_ephemeral(interaction,
        f"✏️ Current name: **{old_name}**\n"
        f"Enter a new name (or `cancel` to abort):")
    text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
    if text is None or text.strip().lower() == "cancel":
        await interaction.edit_original_response(content="❌ Cancelled.", view=None)
        return
    new_name = text.strip()[:100]
    if not new_name:
        await interaction.edit_original_response(content="❌ Store name cannot be empty.", view=None)
        return

    if store_type == "gun":
        async with cog.lock:
            state = await cog._load_state()
            s = state.get("stores", {}).get(store_id)
            if not s:
                await interaction.edit_original_response(content="❌ Store not found.", view=None)
                return
            s["store_name"] = new_name
            await cog._save_state(state)
    else:
        async with cog.lock:
            state = await cog._load_state()
            s = state.get("ripperdoc_stores", {}).get(store_id)
            if not s:
                await interaction.edit_original_response(content="❌ Store not found.", view=None)
                return
            s["store_name"] = new_name
            await cog._save_state(state)

    await interaction.edit_original_response(
        content=f"✅ Renamed **{old_name}** → **{new_name}**.",
        view=None)

    admin_cog = interaction.client.get_cog("AdminShop")
    if admin_cog:
        log_ch = await admin_cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title="✏️ Admin: Store Renamed",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
            embed.add_field(name="Old Name", value=old_name, inline=True)
            embed.add_field(name="New Name", value=new_name, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            try:
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass


class _AdminCreateStoreOwnerSelect(SafeView):
    def __init__(self, admin_id: int, store_type: str):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0] if select.values else None
        if not member:
            await respond_ephemeral(interaction, "No member selected.")
            return
        guild = interaction.guild
        if guild and not isinstance(member, discord.Member):
            member = guild.get_member(member.id)
        if not member or not isinstance(member, discord.Member):
            await respond_ephemeral(interaction, "Could not resolve that member in this server.")
            return
        if member.bot:
            await respond_ephemeral(interaction, "Cannot create a store for a bot.")
            return

        await respond_ephemeral(interaction,
            f"📝 **Enter a name** for {member.display_name}'s "
            f"{'Gun' if self.store_type == 'gun' else 'Ripperdoc'} Store:\n"
            f"Type `cancel` to abort.")
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None or text.strip().lower() == "cancel":
            await interaction.edit_original_response(content="❌ Cancelled.", view=None)
            return
        store_name = text.strip()[:100]
        if not store_name:
            await interaction.edit_original_response(content="❌ Store name cannot be empty.", view=None)
            return

        await _admin_create_store(interaction, member, self.store_type, store_name)


async def _admin_create_store(interaction: discord.Interaction, owner: discord.Member, store_type: str, store_name: str):
    guild = interaction.guild
    if not guild:
        await interaction.edit_original_response(content="❌ Must be used in a server.", view=None)
        return

    if store_type == "gun":
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if not guns_cog:
            await interaction.edit_original_response(content="❌ Gun shop system unavailable.", view=None)
            return
        store_id = guns_cog._store_id(guild.id, owner.id)
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            existing = state.get("stores", {}).get(store_id)
            if existing and existing.get("store_name"):
                await interaction.edit_original_response(
                    content=f"❌ {owner.display_name} already owns a gun store (**{existing['store_name']}**).",
                    view=None)
                return
            bm_ids = getattr(config, "BLACK_MARKET_OWNER_IDS", set())
            st = "black_market" if owner.id in bm_ids else "standard"
            stores = state.setdefault("stores", {})
            entry = stores.setdefault(store_id, {})
            entry.setdefault("owner_id", owner.id)
            entry.setdefault("lots", [])
            entry.setdefault("controlled_buyers", [])
            entry.setdefault("employees", [])
            entry.setdefault("store_type", st)
            entry["store_name"] = store_name
            saved = await guns_cog._save_state(state)
        if not saved:
            await interaction.edit_original_response(content="❌ Failed to save store data.", view=None)
            return
        owner_role = guild.get_role(config.GUN_STORE_OWNER_ROLE_ID) if hasattr(config, "GUN_STORE_OWNER_ROLE_ID") and config.GUN_STORE_OWNER_ROLE_ID else None
        if not owner_role and hasattr(config, "WHOLESALER_STORE_ROLE_IDS") and config.WHOLESALER_STORE_ROLE_IDS:
            owner_role = guild.get_role(config.WHOLESALER_STORE_ROLE_IDS)
        if owner_role:
            try:
                await owner.add_roles(owner_role, reason=f"Admin created gun store: {store_name}")
            except Exception:
                pass
        emoji = "🔫"
        type_label = "Gun Store"
    else:
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if not cw_cog:
            await interaction.edit_original_response(content="❌ Cyberware system unavailable.", view=None)
            return
        store_id = f"rd:{guild.id}:{owner.id}"
        async with cw_cog.lock:
            state = await cw_cog._load_state()
            existing = state.get("ripperdoc_stores", {}).get(store_id)
            if existing and existing.get("store_name"):
                await interaction.edit_original_response(
                    content=f"❌ {owner.display_name} already owns a ripperdoc store (**{existing['store_name']}**).",
                    view=None)
                return
            state.setdefault("ripperdoc_stores", {})[store_id] = {
                "owner_id": owner.id,
                "employees": [],
                "store_name": store_name,
            }
            saved = await cw_cog._save_state(state)
        if not saved:
            await interaction.edit_original_response(content="❌ Failed to save store data.", view=None)
            return
        owner_role = guild.get_role(config.RIPPERDOC_OWNER_ROLE_ID) if hasattr(config, "RIPPERDOC_OWNER_ROLE_ID") and config.RIPPERDOC_OWNER_ROLE_ID else None
        if owner_role:
            try:
                await owner.add_roles(owner_role, reason=f"Admin created ripperdoc store: {store_name}")
            except Exception:
                pass
        emoji = "💉"
        type_label = "Ripperdoc Store"

    await interaction.edit_original_response(
        content=f"✅ Created **{store_name}** ({type_label}) for {owner.mention}.",
        view=None)

    cog = interaction.client.get_cog("AdminShop")
    if cog:
        log_ch = await cog._audit_channel()
        if log_ch:
            embed = discord.Embed(
                title=f"{emoji} Admin: {type_label} Created",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
            embed.add_field(name="Owner", value=owner.mention, inline=True)
            embed.add_field(name="Store Name", value=store_name, inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            try:
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass


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


class _SeedRipperdocStorePickerView(SafeView):
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
        await _seed_ripperdoc_stores(self.cog, interaction, target_store_id=choice)


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
                "**Manage Stores** — Seed stores with starter inventory\n"
                "**Set Gun/Cyberware Sheet** — Set Google Sheet URL for catalogs\n"
                "**Reload Sheets** — Re-download and refresh both catalogs\n"
                "**Perm Overwrites** — Manage permission overwrites"
            ),
            color=discord.Color.orange(),
        )

    @staticmethod
    def _guide_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📘 Admin Panel — How It Works",
            description=(
                "This panel gives admins full control over the shop systems and item tracking. "
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


_FRIENDLY_BACKUP_LABELS = {
    "collect_housing_after": "Housing Rent collected",
    "collect_business_after": "Business Rent collected",
    "collect_cyberware_after": "Cyberware Rent collected",
    "trauma_after": "Trauma Team service",
}


def _friendly_backup_label(raw_label: str) -> Optional[str]:
    """Convert a backup-file label to a human-readable reason.

    Returns ``None`` for snapshot anchor entries (`*_before`) that do not
    represent real balance changes and should be skipped.
    """
    if not raw_label:
        return None
    if raw_label.endswith("_before"):
        return None
    if raw_label in _FRIENDLY_BACKUP_LABELS:
        return _FRIENDLY_BACKUP_LABELS[raw_label]
    if raw_label.startswith("manual_"):
        return "Admin manual backup/restore"
    return raw_label


async def _load_backup_history(user_id: int, since_dt: datetime) -> list[dict]:
    """Load post-`*_after` snapshots from the per-user backup file.

    Returns rows shaped like ``balance_history_get`` so they can be merged
    transparently with the live audit table.
    """
    backup_path = Path(config.BALANCE_BACKUP_DIR) / f"balance_backup_{user_id}.json"
    entries = await helpers.load_json_file(backup_path, default=[])
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since_dt:
            continue
        reason = _friendly_backup_label(str(entry.get("label", "")))
        if reason is None:
            continue
        try:
            change = int(entry.get("change", 0) or 0)
        except (TypeError, ValueError):
            # Skip a single malformed row rather than dropping the whole file.
            continue
        # Backup snapshots only carry a single combined ``change`` (cash+bank
        # delta). Surface it under cash_delta for display purposes; bank stays 0.
        out.append({
            "id": None,
            "ts": ts,
            "cash_delta": change,
            "bank_delta": 0,
            "reason": f"{reason} (snapshot)",
        })
    return out


def _merge_history(
    live_rows: list[dict],
    backup_rows: list[dict],
    *,
    dedupe_window_seconds: int = 120,
) -> list[dict]:
    """Merge live audit + backup rows, dropping near-duplicate snapshots.

    A backup row is treated as a duplicate when a live row exists within
    ``dedupe_window_seconds`` whose cash AND bank deltas both match the
    backup row exactly. Comparing the components separately (rather than
    their sum) prevents collapsing materially different events that happen
    to share the same net total — e.g. a withdraw (cash=+500, bank=-500)
    vs a no-op snapshot (cash=0, bank=0).
    """
    merged = list(live_rows)
    for b in backup_rows:
        is_dup = False
        b_cash = int(b["cash_delta"])
        b_bank = int(b["bank_delta"])
        b_ts = b["ts"]
        for r in live_rows:
            if int(r["cash_delta"]) != b_cash or int(r["bank_delta"]) != b_bank:
                continue
            try:
                gap = abs((r["ts"] - b_ts).total_seconds())
            except Exception:
                continue
            if gap <= dedupe_window_seconds:
                is_dup = True
                break
        if not is_dup:
            merged.append(b)
    merged.sort(key=lambda r: r["ts"], reverse=True)
    return merged


def _format_history_lines(rows: list[dict], *, max_rows: int = 50) -> tuple[list[str], int]:
    """Format rows for embed display. Returns (lines, omitted_count)."""
    lines: list[str] = []
    show = rows[:max_rows]
    for r in show:
        ts: datetime = r["ts"]
        try:
            ts_str = ts.strftime("%m/%d %H:%M")
        except Exception:
            ts_str = str(ts)
        cash = int(r["cash_delta"])
        bank = int(r["bank_delta"])
        parts: list[str] = []
        if cash:
            parts.append(f"{'+' if cash > 0 else ''}${cash:,} cash")
        if bank:
            parts.append(f"{'+' if bank > 0 else ''}${bank:,} bank")
        if not parts:
            parts.append("$0")
        delta_str = " / ".join(parts)
        reason = (r.get("reason") or "").strip() or "(no reason)"
        if len(reason) > 140:
            reason = reason[:137] + "…"
        lines.append(f"`{ts_str}` **{delta_str}** — {reason}")
    omitted = max(0, len(rows) - len(show))
    return lines, omitted


def _fit_lines_to_description(lines: list[str], cap: int = 3900) -> tuple[str, int]:
    """Join lines into a single description, truncating to fit Discord's limit.

    Returns (description, dropped_lines).
    """
    out: list[str] = []
    total = 0
    for ln in lines:
        add = len(ln) + 1  # +1 for newline
        if total + add > cap:
            break
        out.append(ln)
        total += add
    dropped = len(lines) - len(out)
    return "\n".join(out), dropped


# UnbelievaBoat's economy-log embed fields. UB writes mentions in two
# different ways depending on the command: sometimes as a real Discord
# mention (`<@123>`), but more often — especially for /give-money,
# /add-money, /remove-money — as plain text "@Username (Display Name)"
# to avoid pinging users. We try the snowflake form first, then fall
# back to a name-based match against the target member.
_UB_USER_RE = re.compile(r"User:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_UB_ACTOR_RE = re.compile(r"Actioned by:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_UB_SNOWFLAKE_RE = re.compile(r"<@!?(\d+)>")
_UB_AMOUNT_RE = re.compile(
    r"Amount:\s*Cash:\s*([+\-]?[\d,]+)\s*\|\s*Bank:\s*([+\-]?[\d,]+)",
    re.IGNORECASE,
)
_UB_REASON_RE = re.compile(r"Reason:\s*(.+?)(?:\n|$)", re.IGNORECASE | re.DOTALL)


def _candidate_names_for(member: Any) -> list[str]:
    """All plausible name strings UnbelievaBoat may print for a member.

    Includes username, global_name, display_name, nick, and the
    parenthesized "Username (Display Name)" combo UB uses in its
    plain-text mentions. Names are lowercased for case-insensitive
    comparison and de-duplicated while preserving order.
    """
    if member is None:
        return []
    raw: list[str] = []
    for attr in ("name", "global_name", "display_name", "nick"):
        v = getattr(member, attr, None)
        if v:
            raw.append(str(v))
    # UB combo form: "Username (DisplayName)"
    uname = getattr(member, "name", None)
    dname = getattr(member, "display_name", None) or getattr(member, "global_name", None)
    if uname and dname and uname != dname:
        raw.append(f"{uname} ({dname})")
    seen: set[str] = set()
    out: list[str] = []
    for n in raw:
        low = n.lower()
        if low and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _ub_line_matches(line: str, target_id: int, target_names: list[str]) -> bool:
    """True if a 'User:'/'Actioned by:' value refers to ``target_id``.

    Checks for the literal Discord snowflake first (most reliable),
    then falls back to substring-matching any of ``target_names`` in
    the line. Name match is intentionally substring-based because UB's
    plaintext form may be wrapped in punctuation we can't predict.
    """
    if not line:
        return False
    snow = _UB_SNOWFLAKE_RE.search(line)
    if snow:
        return int(snow.group(1)) == int(target_id)
    low = line.lower()
    return any(name in low for name in target_names)


def _parse_ub_amount(raw: str) -> int:
    """Parse '+477' or '-3,000' or '0' into an int. Returns 0 on failure."""
    try:
        return int(raw.replace(",", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        return 0


def _extract_ub_text(embed: discord.Embed) -> str:
    """Concatenate every text-bearing slot of an UnbelievaBoat balance embed.

    UnbelievaBoat has historically put the User/Amount/Reason block in the
    embed description, but they've also shipped versions that use embed
    fields. We join everything so a single regex pass works for both.
    """
    parts: list[str] = []
    if embed.title:
        parts.append(str(embed.title))
    if embed.description:
        parts.append(str(embed.description))
    for f in getattr(embed, "fields", []) or []:
        name = getattr(f, "name", "") or ""
        value = getattr(f, "value", "") or ""
        # UnbelievaBoat sometimes uses field names as the row label
        # ("User", "Amount", "Reason") and field values as the data.
        # Joining "name: value" produces text that the same regexes match.
        parts.append(f"{name}: {value}" if name and ":" not in name else f"{name}\n{value}")
    return "\n".join(p for p in parts if p)


def _parse_ub_balance_embed(
    embed: discord.Embed,
    target_user_id: int,
    target_names: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Parse a single UnbelievaBoat balance-update embed for ``target_user_id``.

    Returns a row dict {ts: None, cash_delta, bank_delta, reason} when the
    embed concerns the target user, else None. ``ts`` is filled in by the
    caller from the parent message timestamp.

    For ``give-money`` style transfers UnbelievaBoat emits two separate
    embeds (one per side of the transaction), so each is parsed
    independently and only kept if its ``User:`` line matches the target.
    """
    text = _extract_ub_text(embed)
    if not text:
        return None
    target_names = target_names or []
    user_m = _UB_USER_RE.search(text)
    if not user_m:
        return None
    user_line = user_m.group(1).strip()
    if not _ub_line_matches(user_line, target_user_id, target_names):
        return None
    amount_m = _UB_AMOUNT_RE.search(text)
    if not amount_m:
        return None
    cash_delta = _parse_ub_amount(amount_m.group(1))
    bank_delta = _parse_ub_amount(amount_m.group(2))
    if cash_delta == 0 and bank_delta == 0:
        # Skip no-op rows — they add noise without informational value.
        return None
    reason_m = _UB_REASON_RE.search(text)
    reason = reason_m.group(1).strip() if reason_m else "(no reason)"
    actor_m = _UB_ACTOR_RE.search(text)
    if actor_m:
        actor_line = actor_m.group(1).strip()
        if not _ub_line_matches(actor_line, target_user_id, target_names):
            # Someone else acted on this user (e.g. received give-money).
            # Prefer the snowflake suffix when present, otherwise show
            # the literal actor text UB printed.
            snow = _UB_SNOWFLAKE_RE.search(actor_line)
            actor_label = f"<@{snow.group(1)}>" if snow else actor_line
            reason = f"{reason} — by {actor_label}"
    return {
        "id": None,
        "ts": None,  # filled by caller
        "cash_delta": cash_delta,
        "bank_delta": bank_delta,
        "reason": f"UB: {reason}",
    }


async def _load_economy_log_history(
    bot: commands.Bot,
    user_id: int,
    since_dt: datetime,
    *,
    max_messages: int = 2000,
) -> list[dict]:
    """Scrape #economy-logs for UnbelievaBoat balance-updates affecting ``user_id``.

    Read-only. Returns rows shaped like ``balance_history_get`` so they
    can be merged with the live audit table and backup snapshots.
    """
    channel_id = getattr(config, "ECONOMY_LOG_CHANNEL_ID", 0) or 0
    if not channel_id:
        return []
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("economy_log channel %s not accessible", channel_id)
            return []
    if not hasattr(channel, "history"):
        return []
    ub_id = int(getattr(config, "UNBELIEVABOAT_BOT_ID", 0) or 0)
    # Resolve plausible name strings for the target — UnbelievaBoat
    # often writes mentions as plain "@Username (Display Name)" text
    # rather than real Discord snowflake mentions, so we need names
    # to fall back to when the snowflake isn't present.
    target_names: list[str] = []
    try:
        guild = getattr(channel, "guild", None)
        member = None
        if guild is not None:
            member = guild.get_member(int(user_id))
            if member is None and hasattr(guild, "fetch_member"):
                try:
                    member = await guild.fetch_member(int(user_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
        if member is None:
            user_obj = bot.get_user(int(user_id))
            if user_obj is None and hasattr(bot, "fetch_user"):
                try:
                    user_obj = await bot.fetch_user(int(user_id))
                except (discord.NotFound, discord.HTTPException):
                    user_obj = None
            member = user_obj
        target_names = _candidate_names_for(member)
    except Exception:
        logger.exception("Failed resolving names for user %s", user_id)
        target_names = []
    out: list[dict] = []
    stats = {
        "scanned": 0,
        "ub_msgs": 0,
        "embeds_seen": 0,
        "parsed": 0,
        "target_names": list(target_names),
        "channel_name": getattr(channel, "name", None) or str(channel_id),
        "ub_id_filter": ub_id,
        "error": None,
    }
    # Walk newest→oldest from now and stop ourselves once we cross the
    # since_dt boundary. This avoids any discord.py quirk with passing
    # `after=<datetime>` combined with `oldest_first` flags, and gives
    # us a deterministic, bounded scrape.
    try:
        async for msg in channel.history(limit=max_messages):
            stats["scanned"] += 1
            ts = getattr(msg, "created_at", None) or datetime.now(timezone.utc)
            if isinstance(ts, datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since_dt:
                break
            if ub_id and getattr(getattr(msg, "author", None), "id", None) != ub_id:
                continue
            stats["ub_msgs"] += 1
            for embed in getattr(msg, "embeds", []) or []:
                stats["embeds_seen"] += 1
                row = _parse_ub_balance_embed(embed, user_id, target_names)
                if row is None:
                    continue
                row["ts"] = ts
                out.append(row)
                stats["parsed"] += 1
    except (discord.Forbidden, discord.HTTPException) as exc:
        stats["error"] = type(exc).__name__
        logger.exception("Failed reading economy_log history for user %s", user_id)
    logger.info(
        "economy_log scrape user=%s scanned=%d ub_msgs=%d embeds=%d parsed=%d names=%s err=%s",
        user_id, stats["scanned"], stats["ub_msgs"], stats["embeds_seen"],
        stats["parsed"], target_names, stats["error"],
    )
    # Stash stats on the function for the picker view to surface.
    _load_economy_log_history.last_stats = stats  # type: ignore[attr-defined]
    return out


class BalanceHistoryPickerView(SafeView):
    """User picker for the Balance History admin panel button."""

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
        since_dt = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            live_rows = await balance_history_get(str(member.id), since=since_dt, limit=500)
        except Exception:
            logger.exception("balance_history_get failed for user %s", member.id)
            live_rows = []
        try:
            backup_rows = await _load_backup_history(member.id, since_dt)
        except Exception:
            logger.exception("_load_backup_history failed for user %s", member.id)
            backup_rows = []
        try:
            ub_rows = await _load_economy_log_history(
                interaction.client, member.id, since_dt
            )
        except Exception:
            logger.exception("_load_economy_log_history failed for user %s", member.id)
            ub_rows = []
        ub_stats = getattr(_load_economy_log_history, "last_stats", None) or {}
        # Normalize live row timestamps to aware UTC for comparisons
        for r in live_rows:
            ts = r.get("ts")
            if isinstance(ts, datetime) and ts.tzinfo is None:
                r["ts"] = ts.replace(tzinfo=timezone.utc)
        # _merge_history dedupes by (delta-total within 120s). Run it twice:
        # first to drop economy-log rows that match an internal-bot live row
        # (since our update_balance call shows up in BOTH places), then
        # again to merge in the older backup snapshots.
        merged = _merge_history(live_rows, ub_rows)
        merged = _merge_history(merged, backup_rows)
        if not merged:
            diag = ""
            if ub_stats:
                diag = (
                    f"\n_UB scrape: scanned={ub_stats.get('scanned', 0)} "
                    f"ub={ub_stats.get('ub_msgs', 0)} "
                    f"embeds={ub_stats.get('embeds_seen', 0)} "
                    f"parsed={ub_stats.get('parsed', 0)}"
                    + (f" err={ub_stats['error']}" if ub_stats.get('error') else "")
                    + "_"
                )
            await send_ephemeral(
                interaction,
                f"No balance changes recorded for **{member.display_name}** in the last 30 days.{diag}",
            )
            return
        lines, omitted = _format_history_lines(merged, max_rows=50)
        description, dropped = _fit_lines_to_description(lines)
        footer_bits = [f"{len(merged)} change(s) in window"]
        if omitted:
            footer_bits.append(f"{omitted} older entries hidden")
        if dropped:
            footer_bits.append(f"{dropped} lines truncated for length")
        if ub_stats:
            footer_bits.append(
                "UB scrape: scanned={s} ub={u} embeds={e} parsed={p}{err}".format(
                    s=ub_stats.get("scanned", 0),
                    u=ub_stats.get("ub_msgs", 0),
                    e=ub_stats.get("embeds_seen", 0),
                    p=ub_stats.get("parsed", 0),
                    err=f" err={ub_stats['error']}" if ub_stats.get("error") else "",
                )
            )
        embed = discord.Embed(
            title=f"Balance History — {member.display_name}",
            description=description or "(no entries fit)",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Last 30 days • " + " • ".join(footer_bits))
        await send_ephemeral(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminShopCog(bot))
