"""Player hub — interactive !player panel for viewing inventory, trading, and giving items."""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView
from NightCityBot.utils.player_inventory import (
    query_player_inventory as pi_get_by_owner,
    get_player_item as pi_get_item,
    delete_player_item as pi_delete_item,
    transfer_player_item as pi_update_owner,
    insert_player_item as pi_add_item,
)
from NightCityBot.utils.db import ih_record_event, pt_create
from NightCityBot.utils.inline_helpers import collect_text_input
from NightCityBot.utils.characters import (
    create_character,
    get_active_characters,
    get_all_characters,
    get_character_by_name,
    get_inactive_characters,
    deactivate_character,
    reactivate_character,
    character_name_exists,
)
from NightCityBot.utils.panel_context import PanelContext

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


async def _resolve_member_name(guild: discord.Guild, owner_id) -> str:
    if not owner_id:
        return "Unknown"
    try:
        oid = int(owner_id)
    except (TypeError, ValueError):
        return "Unknown"
    member = guild.get_member(oid)
    if member:
        return member.display_name
    try:
        member = await guild.fetch_member(oid)
        return member.display_name
    except Exception:
        return f"User#{oid}"


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
        self._panel_view = PlayerHubView()
        bot.add_view(self._panel_view)

    def _inv_system_enabled(self) -> bool:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("player_inventory"):
            return False
        return True

    @staticmethod
    def _panel_embed() -> discord.Embed:
        return discord.Embed(
            title="🎒 Player Hub",
            description="Manage your inventory, trade with other players, give items, or sell guns to a store.",
            color=discord.Color.blue(),
        )

    @commands.hybrid_command(name="player")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def player_cmd(self, ctx: commands.Context):
        """Post (or refresh) the persistent Player Hub panel in the designated channel."""
        channel = self.bot.get_channel(config.PLAYER_HUB_CHANNEL_ID)
        if channel is None:
            await ctx.send("❌ Player hub channel not found.", ephemeral=True)
            return
        view = PlayerHubView()
        await channel.send(embed=self._panel_embed(), view=view)
        await ctx.send("✅ Player Hub panel posted.", ephemeral=True)
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
                f"Head to <#{config.PLAYER_HUB_CHANNEL_ID}> and use the **Player Hub** panel.\n\n"
                "From the panel you can:\n"
                "• **View Inventory** — pick a character and see their items\n"
                "• **Sell to Player** — sell an item to another player (with payment)\n"
                "• **Give Item** — transfer an item for free\n"
                "• **Sell to Store** — sell guns to gun stores or cyberware to ripperdoc stores\n"
                "• **Create Character / View Characters / Deactivate Character** — create, view, deactivate, or reactivate characters\n"
                "• **Start LOA / End LOA** — start or end your Leave of Absence"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Use !helpme for general help")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayerHubCog(bot))


def _build_inventory_embed(
    display_name: str,
    items: list[dict],
    inv_cog,
    char_filter: str | None = None,
) -> discord.Embed:
    if char_filter:
        label = f"{display_name}'s Inventory — {char_filter}"
    else:
        label = f"{display_name}'s Inventory"
    display_lines, all_groups = inv_cog._build_display(items, char_filter=char_filter)
    if not all_groups:
        return discord.Embed(title=f"📦 {label}", description="No items.", color=discord.Color.blue())
    item_lines = [(rn, ln) for rn, ln in display_lines if rn is not None]
    total_groups = len(item_lines)
    total_pages = max(1, (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page_rows = {rn for rn, _ in item_lines[:GROUPS_PER_PAGE]}
    page_lines: list[str] = []
    pending_headers: list[str] = []
    for rn, ln in display_lines:
        if rn is None:
            pending_headers.append(ln)
        else:
            if rn in page_rows:
                for hdr in pending_headers:
                    page_lines.append(hdr)
                pending_headers = []
                page_lines.append(ln)
            else:
                pending_headers = []
    embed = discord.Embed(
        title=f"📦 {label} (1/{total_pages})",
        description="\n".join(page_lines) if page_lines else "No items.",
        color=discord.Color.blue(),
    )
    hint = "Use the page buttons to see more." if total_pages > 1 else ""
    embed.set_footer(
        text=f"{total_groups} total item(s)"
        + (f" | {hint}" if hint else "")
    )
    return embed


class InventoryCharFilterView(SafeView):
    def __init__(self, cog, ctx, items, inv_cog, char_names: list[str]):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.items = items
        self.inv_cog = inv_cog
        options = [discord.SelectOption(label="All Characters", value="__all__")]
        for name in char_names[:24]:
            options.append(discord.SelectOption(label=name, value=name))
        self.char_select = discord.ui.Select(
            placeholder="Select a character…",
            options=options,
            row=0,
        )
        self.char_select.callback = self._on_char_select
        self.add_item(self.char_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def _on_char_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        chosen = self.char_select.values[0]
        char_filter = None if chosen == "__all__" else chosen
        embed = _build_inventory_embed(
            interaction.user.display_name, self.items, self.inv_cog, char_filter
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlayerHubView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.primary, emoji="📦", row=0, custom_id="player_hub:view_inv")
    async def view_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        if not cog or not cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty.", ephemeral=True)
            return
        inv_cog = interaction.client.get_cog("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        ctx = PanelContext(interaction)
        char_names = sorted({item.get("character_name", "") for item in items if item.get("character_name")})
        if char_names:
            view = InventoryCharFilterView(cog, ctx, items, inv_cog, char_names)
            await interaction.followup.send(
                "🔎 **Select a character to view their inventory:**",
                view=view,
                ephemeral=True,
            )
        else:
            embed = _build_inventory_embed(interaction.user.display_name, items, inv_cog)
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Manage Inventory", style=discord.ButtonStyle.success, emoji="💼", row=0, custom_id="player_hub:manage_inv")
    async def manage_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        if not cog or not cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        view = ManageInventoryView(interaction.user.id)
        await interaction.followup.send(
            "💼 **Manage Inventory** — Choose an action:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Manage Characters", style=discord.ButtonStyle.primary, emoji="🧑", row=1, custom_id="player_hub:manage_chars_menu")
    async def manage_chars_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ManageCharactersMenuView(interaction.user.id)
        await interaction.followup.send(
            "🧑 **Manage Characters** — Choose an action:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Manage Businesses", style=discord.ButtonStyle.secondary, emoji="🏢", row=1, custom_id="player_hub:manage_biz")
    async def manage_biz(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ManageBusinessesView(interaction.user.id)
        await interaction.followup.send(
            "🏢 **Manage Businesses** — Choose an action:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Start LOA", style=discord.ButtonStyle.secondary, emoji="🏖️", row=2, custom_id="player_hub:start_loa")
    async def start_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("loa"):
            await interaction.followup.send("⚠️ The LOA system is currently disabled.", ephemeral=True)
            return
        from NightCityBot.cogs.loa import get_loa_role
        loa_role = get_loa_role(guild)
        if loa_role is None:
            await interaction.followup.send("⚠️ LOA role is not configured.", ephemeral=True)
            return
        member = interaction.user
        if any(r.id == loa_role.id for r in member.roles):
            await interaction.followup.send("You are already on LOA.", ephemeral=True)
            return
        try:
            await member.add_roles(loa_role, reason="LOA start via Player Hub")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Could not assign LOA role: {e}", ephemeral=True)
            return
        await interaction.followup.send("✅ You are now on LOA.", ephemeral=True)

    @discord.ui.button(label="End LOA", style=discord.ButtonStyle.secondary, emoji="🔙", row=2, custom_id="player_hub:end_loa")
    async def end_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("loa"):
            await interaction.followup.send("⚠️ The LOA system is currently disabled.", ephemeral=True)
            return
        from NightCityBot.cogs.loa import get_loa_role
        loa_role = get_loa_role(guild)
        if loa_role is None:
            await interaction.followup.send("⚠️ LOA role is not configured.", ephemeral=True)
            return
        member = interaction.user
        if not any(r.id == loa_role.id for r in member.roles):
            await interaction.followup.send("You are not currently on LOA.", ephemeral=True)
            return
        try:
            await member.remove_roles(loa_role, reason="LOA end via Player Hub")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Could not remove LOA role: {e}", ephemeral=True)
            return
        await interaction.followup.send("✅ Your LOA has ended.", ephemeral=True)

    @discord.ui.button(label="Attend", style=discord.ButtonStyle.success, emoji="📋", row=3, custom_id="player_hub:attend")
    async def attend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        econ_cog = interaction.client.get_cog("Economy")
        if not econ_cog:
            await interaction.followup.send("⚠️ Economy system unavailable.", ephemeral=True)
            return
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("attend"):
            await interaction.followup.send("⚠️ The attend system is currently disabled.", ephemeral=True)
            return
        member = interaction.user
        if not any(r.id == config.VERIFIED_ROLE_ID for r in member.roles):
            await interaction.followup.send("❌ You must be verified to use this.", ephemeral=True)
            return

        from NightCityBot.utils import helpers
        from zoneinfo import ZoneInfo
        from datetime import timedelta
        from NightCityBot.utils.db import attendance_get_user, attendance_append, warn_db_failure

        now = helpers.get_tz_now()
        if econ_cog.event_active():
            event_start = econ_cog.event_started_at or now
        else:
            if now.weekday() != 6:
                await interaction.followup.send(
                    "❌ Attendance is only allowed during Sunday events (2pm to 7pm Pacific).",
                    ephemeral=True,
                )
                return
            tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
            local_now = now.astimezone(tz)
            start = econ_cog._sunday_event_start(now)
            end = start + timedelta(hours=5)
            if not (start <= local_now <= end):
                await interaction.followup.send(
                    "❌ Attendance is only allowed during Sunday events (2pm to 7pm Pacific).",
                    ephemeral=True,
                )
                return
            event_start = start

        user_id = str(interaction.user.id)
        now_str = now.isoformat()

        async with econ_cog._attend_locks.acquire(user_id):
            all_logs = await attendance_get_user(user_id)
            parsed = [datetime.fromisoformat(ts) for ts in all_logs]
            if any(ts >= event_start for ts in parsed):
                await interaction.followup.send("❌ You've already logged attendance for this event.", ephemeral=True)
                return
            ok = await attendance_append(user_id, now_str)
            if not ok:
                await warn_db_failure(
                    interaction.client, "attendance_append",
                    f"user {user_id} — attendance not recorded",
                )

        from NightCityBot.utils import config_loader as _cfg
        reward = _cfg.get_attend_reward()
        ub = getattr(econ_cog, "unbelievaboat", None)
        if ub:
            ok = await ub.update_balance(
                interaction.user.id, {"cash": reward}, reason="Attendance reward"
            )
            if not ok:
                await warn_db_failure(
                    interaction.client, "update_balance",
                    f"user {user_id} — attendance reward ${reward} not credited",
                )
                await interaction.followup.send(
                    "✅ Attendance logged! "
                    f"⚠️ Balance update failed — please contact an admin if your ${reward} reward is missing.",
                    ephemeral=True,
                )
                return
        await interaction.followup.send(f"✅ Attendance logged! You received ${reward}.", ephemeral=True)

    @discord.ui.button(label="Open Shop", style=discord.ButtonStyle.success, emoji="🏪", row=3, custom_id="player_hub:open_shop")
    async def open_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        econ_cog = interaction.client.get_cog("Economy")
        if not econ_cog:
            await interaction.followup.send("⚠️ Economy system unavailable.", ephemeral=True)
            return
        control = interaction.client.get_cog("SystemControl")
        if control and not control.is_enabled("open_shop"):
            await interaction.followup.send("⚠️ The open_shop system is currently disabled.", ephemeral=True)
            return
        member = interaction.user
        if not any(r.name.startswith("Business") for r in member.roles):
            await interaction.followup.send("❌ You must have a business role to use this.", ephemeral=True)
            return

        from NightCityBot.utils import helpers
        from NightCityBot.utils.db import open_log_exists_today, open_log_count_month, open_log_add, warn_db_failure
        from NightCityBot.utils import config_loader as _cfg

        now = helpers.get_tz_now()
        if now.weekday() != 6 and not econ_cog.event_active():
            await interaction.followup.send("❌ Business openings can only be logged on Sundays.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        duplicate = False
        open_count_before = 0
        open_count_after = 0
        open_count_total = 0
        async with econ_cog._open_log_locks.acquire(user_id):
            if await open_log_exists_today(user_id):
                duplicate = True
            else:
                open_count_before = min(await open_log_count_month(user_id, now.year, now.month), 4)
                ok = await open_log_add(user_id, now)
                if not ok:
                    await warn_db_failure(
                        interaction.client, "open_log_add",
                        f"user {user_id} — opening not recorded",
                    )
                open_count_total = open_count_before + 1
                open_count_after = min(open_count_total, 4)

        if duplicate:
            await interaction.followup.send("❌ You've already logged a business opening today.", ephemeral=True)
            return

        reward = 0
        role_names = [r.name for r in member.roles]
        t0_scale = _cfg.get_tier0_income_scale()
        biz_costs = _cfg.get_role_costs_business()
        open_pct = _cfg.get_open_percent()
        for role in role_names:
            if "Business Tier" in role:
                if role == "Business Tier 0":
                    total_after = t0_scale.get(open_count_after, 0)
                    total_before = t0_scale.get(open_count_before, 0)
                else:
                    base = biz_costs.get(role, 500)
                    total_after = int(base * open_pct.get(open_count_after, 0))
                    total_before = int(base * open_pct.get(open_count_before, 0))
                reward += total_after - total_before

        if reward > 0:
            ub = getattr(econ_cog, "unbelievaboat", None)
            if ub:
                ok = await ub.update_balance(
                    interaction.user.id, {"cash": reward}, reason="Business activity reward"
                )
                if not ok:
                    await warn_db_failure(
                        interaction.client, "update_balance",
                        f"user {user_id} — business reward ${reward} not credited",
                    )
                    await interaction.followup.send(
                        f"✅ Business opening logged! ({open_count_total} this month) "
                        "⚠️ Balance update failed — please contact an admin if your reward is missing.",
                        ephemeral=True,
                    )
                    return
            await interaction.followup.send(
                f"✅ Business opening logged! You earned ${reward}. ({open_count_total} this month)",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Business opening logged! ({open_count_total} this month)",
                ephemeral=True,
            )

    @discord.ui.button(label="View Due", style=discord.ButtonStyle.secondary, emoji="💸", row=3, custom_id="player_hub:due")
    async def view_due(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        econ_cog = interaction.client.get_cog("Economy")
        if not econ_cog:
            await interaction.followup.send("⚠️ Economy system unavailable.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        total, details = econ_cog.calculate_due(member)
        header = f"💸 **Estimated Due:** ${total}"
        lines = [header] + [f"• {d}" for d in details]

        from NightCityBot.utils.db import last_payment_get_with_ts
        from zoneinfo import ZoneInfo
        from NightCityBot.utils import helpers

        _, paid_at = await last_payment_get_with_ts(str(member.id))
        if paid_at is not None:
            tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
            now_local = helpers.get_tz_now()
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=ZoneInfo("UTC"))
            paid_local = paid_at.astimezone(tz)
            if paid_local.year == now_local.year and paid_local.month == now_local.month:
                paid_str = paid_local.strftime("%b %d")
                lines.append(
                    f"✅ **Housing & baseline already paid this month** (recorded {paid_str})."
                    " Cyberware meds are collected separately by staff."
                )

        await interaction.followup.send("\n".join(lines), ephemeral=True)


class ManageInventoryView(SafeView):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self._user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sell to Player", style=discord.ButtonStyle.success, emoji="💱", row=0)
    async def trade_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        if not cog or not cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        inv_cog = interaction.client.get_cog("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        ctx = PanelContext(interaction)
        view = TradeSetupView(cog, ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the buyer and the item to trade:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Sell to Store", style=discord.ButtonStyle.primary, emoji="🏪", row=0)
    async def sell_to_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        if not cog or not cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        seller_chars = await get_active_characters(str(interaction.user.id))
        if not seller_chars:
            await interaction.followup.send(
                "❌ You have no active characters. Create a character first before selling.",
                ephemeral=True,
            )
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to sell.", ephemeral=True)
            return
        sellable_items = [i for i in items if i.get("item_type") in ("gun", "cyberware")]
        if not sellable_items:
            await interaction.followup.send("📦 You have no guns or cyberware to sell to a store.", ephemeral=True)
            return
        inv_cog = interaction.client.get_cog("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(sellable_items)
        if not all_groups:
            await interaction.followup.send("📦 You have no guns or cyberware to sell to a store.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        store_entries = []
        guns_cog = interaction.client.get_cog("GunsShopCog")
        if guns_cog:
            try:
                gun_state = await guns_cog._load_state()
            except Exception:
                gun_state = {}
            prefix = f"{guild.id}:"
            for sid, store in gun_state.get("stores", {}).items():
                if sid.startswith(prefix):
                    owner_id = store.get("owner_id") or int(sid.split(":", 1)[-1])
                    owner_id = int(owner_id)
                    if owner_id == interaction.user.id:
                        continue
                    store_name = store.get("store_name") or "Gun Store"
                    owner_name = await _resolve_member_name(guild, owner_id)
                    store_entries.append({
                        "label": f"🔫 {store_name}",
                        "description": f"Owner: {owner_name}",
                        "value": f"gun:{owner_id}",
                        "store_type": "gun",
                        "owner_id": owner_id,
                    })
        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog:
            try:
                cw_state = await cw_cog._load_state()
            except Exception:
                cw_state = {}
            rd_prefix = f"rd:{guild.id}:"
            for sid, store in cw_state.get("ripperdoc_stores", {}).items():
                if sid.startswith(rd_prefix):
                    owner_id = store.get("owner_id") or int(sid.rsplit(":", 1)[-1])
                    owner_id = int(owner_id)
                    if owner_id == interaction.user.id:
                        continue
                    store_name = store.get("store_name") or "Ripperdoc Clinic"
                    owner_name = await _resolve_member_name(guild, owner_id)
                    store_entries.append({
                        "label": f"💉 {store_name}",
                        "description": f"Owner: {owner_name}",
                        "value": f"rd:{owner_id}",
                        "store_type": "ripperdoc",
                        "owner_id": owner_id,
                    })
        if not store_entries:
            await interaction.followup.send("📦 No stores available to sell to.", ephemeral=True)
            return

        ctx = PanelContext(interaction)
        view = SellToStoreSetupView(cog, ctx, all_groups, seller_chars, store_entries)
        await interaction.followup.send(
            "**Step 1** — Select the store, your character, and the item to sell:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Give Item", style=discord.ButtonStyle.secondary, emoji="🎁", row=0)
    async def give_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        if not cog or not cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        inv_cog = interaction.client.get_cog("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        ctx = PanelContext(interaction)
        view = GiveSetupView(cog, ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the recipient and the item to give:",
            view=view,
            ephemeral=True,
        )


class ManageCharactersMenuView(SafeView):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self._user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Character", style=discord.ButtonStyle.success, emoji="🧑", row=0)
    async def create_char(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        await interaction.followup.send(
            "🧑 **Create Character** — Please type your new character's name below (max 64 characters).\n"
            "You have 60 seconds to reply.",
            ephemeral=True,
        )

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Character creation timed out.", ephemeral=True)
            return

        char_name = msg.content.strip()
        try:
            await msg.delete()
        except Exception:
            pass

        if not char_name:
            await interaction.followup.send("❌ Character name cannot be empty.", ephemeral=True)
            return
        if len(char_name) > 64:
            await interaction.followup.send("❌ Character name must be 64 characters or fewer.", ephemeral=True)
            return

        exists = await character_name_exists(str(interaction.user.id), char_name)
        if exists:
            await interaction.followup.send(
                f"❌ You already have a character named **{char_name}**.", ephemeral=True
            )
            return

        try:
            result = await create_character(str(interaction.user.id), char_name)
        except ValueError as ve:
            await interaction.followup.send(f"❌ {ve}", ephemeral=True)
            return
        if result is None:
            await interaction.followup.send("❌ Failed to create character. Please try again.", ephemeral=True)
            return

        log_ch = await _log_channel(bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
        if log_ch:
            embed = discord.Embed(
                title="🧑 Character Created",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Player", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False)
            embed.add_field(name="Character", value=char_name, inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await interaction.followup.send(
            f"✅ Character **{char_name}** created successfully!", ephemeral=True
        )

    @discord.ui.button(label="View Characters", style=discord.ButtonStyle.primary, emoji="🪪", row=0)
    async def view_characters(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        characters = await get_all_characters(str(interaction.user.id))
        if not characters:
            await interaction.followup.send("🪪 You have no characters yet. Use **Create Character** to make one!", ephemeral=True)
            return
        status_emoji = {"active": "🟢", "inactive": "🔴"}
        lines = []
        for c in characters:
            emoji = status_emoji.get(c.get("status", ""), "⚪")
            name = c.get("name", "?")
            status = c.get("status", "unknown")
            created = str(c.get("created_at", ""))[:10]
            lines.append(f"{emoji} **{name}** — {status} (created {created})")
        embed = discord.Embed(
            title=f"🪪 {interaction.user.display_name}'s Characters",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        active = sum(1 for c in characters if c.get("status") == "active")
        embed.set_footer(text=f"{len(characters)} character(s) total — {active} active")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Deactivate Character", style=discord.ButtonStyle.danger, emoji="⏸️", row=0)
    async def deactivate_char(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("PlayerHub")
        ctx = PanelContext(interaction)
        view = ManageCharactersView(cog, ctx)
        await interaction.followup.send(
            "📋 **Deactivate Character** — Choose an action:",
            view=view,
            ephemeral=True,
        )


class ManageBusinessesView(SafeView):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self._user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="My Businesses", style=discord.ButtonStyle.primary, emoji="👑", row=0)
    async def owned_biz(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        user_id = interaction.user.id
        lines = []

        guns_cog = interaction.client.get_cog("GunsShopCog")
        if guns_cog:
            try:
                state = await guns_cog._load_state()
            except Exception:
                state = {}
            store_id = f"{guild.id}:{user_id}"
            store = state.get("stores", {}).get(store_id)
            if store:
                name = store.get("store_name") or "Gun Store"
                lot_count = len(store.get("lots", []))
                emp_count = len(store.get("employees", []))
                lines.append(f"🔫 **{name}** — {lot_count} lot(s), {emp_count} employee(s)")

        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog:
            try:
                cw_state = await cw_cog._load_state()
            except Exception:
                cw_state = {}
            rd_id = f"rd:{guild.id}:{user_id}"
            rd_store = cw_state.get("ripperdoc_stores", {}).get(rd_id)
            if rd_store:
                name = rd_store.get("store_name") or "Ripperdoc Clinic"
                emp_count = len(rd_store.get("employees", []))
                lines.append(f"💉 **{name}** — {emp_count} employee(s)")

        if not lines:
            await interaction.followup.send("👑 You don't own any businesses.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"👑 {interaction.user.display_name}'s Businesses",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="My Employment", style=discord.ButtonStyle.secondary, emoji="💼", row=0)
    async def employed_biz(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in a server.", ephemeral=True)
            return
        user_id = interaction.user.id
        prefix_gun = f"{guild.id}:"
        prefix_rd = f"rd:{guild.id}:"
        lines = []

        guns_cog = interaction.client.get_cog("GunsShopCog")
        if guns_cog:
            try:
                state = await guns_cog._load_state()
            except Exception:
                state = {}
            for sid, store in state.get("stores", {}).items():
                if sid.startswith(prefix_gun) and user_id in store.get("employees", []):
                    name = store.get("store_name") or "Gun Store"
                    owner_id = store.get("owner_id") or sid.split(":", 1)[-1]
                    owner_name = await _resolve_member_name(guild, owner_id)
                    lines.append(f"🔫 **{name}** — Owner: {owner_name}")

        cw_cog = interaction.client.get_cog("CyberwareShop")
        if cw_cog:
            try:
                cw_state = await cw_cog._load_state()
            except Exception:
                cw_state = {}
            for sid, store in cw_state.get("ripperdoc_stores", {}).items():
                if sid.startswith(prefix_rd) and user_id in store.get("employees", []):
                    name = store.get("store_name") or "Ripperdoc Clinic"
                    owner_id = store.get("owner_id") or sid.rsplit(":", 1)[-1]
                    owner_name = await _resolve_member_name(guild, owner_id)
                    lines.append(f"💉 **{name}** — Owner: {owner_name}")

        if not lines:
            await interaction.followup.send("💼 You're not employed at any businesses.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"💼 {interaction.user.display_name}'s Employment",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ManageCharactersView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    @discord.ui.button(label="Deactivate", style=discord.ButtonStyle.danger, emoji="⏸️", row=0)
    async def deactivate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        chars = await get_active_characters(str(interaction.user.id))
        if not chars:
            await interaction.followup.send("You have no active characters to deactivate.", ephemeral=True)
            return
        view = DeactivateCharacterView(self.cog, self.ctx, chars)
        await interaction.followup.send(
            "Select a character to deactivate:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Reactivate", style=discord.ButtonStyle.success, emoji="▶️", row=0)
    async def reactivate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        chars = await get_inactive_characters(str(interaction.user.id))
        if not chars:
            await interaction.followup.send("You have no inactive characters to reactivate.", ephemeral=True)
            return
        view = ReactivateCharacterView(self.cog, self.ctx, chars)
        await interaction.followup.send(
            "Select a character to reactivate:",
            view=view,
            ephemeral=True,
        )


class DeactivateCharacterView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, chars: list[dict]):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.chars = chars
        self.selected_char_id: Optional[str] = None
        self.selected_char_name: Optional[str] = None

        options = [
            discord.SelectOption(label=c["name"][:100], value=str(c["character_id"]))
            for c in chars[:25]
        ]
        char_select = discord.ui.Select(
            placeholder="Choose a character to deactivate…",
            options=options,
            row=0,
        )
        char_select.callback = self._on_select
        self.add_item(char_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def _on_select(self, interaction: discord.Interaction):
        val = interaction.data["values"][0]
        self.selected_char_id = val
        for c in self.chars:
            if str(c["character_id"]) == val:
                self.selected_char_name = c["name"]
                break
        await interaction.response.send_message(
            f"Selected: **{self.selected_char_name}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Confirm Deactivate", style=discord.ButtonStyle.danger, emoji="⏸️", row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_char_id is None:
            await interaction.response.send_message("Please select a character first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await deactivate_character(self.selected_char_id, user_id=str(interaction.user.id))
        if not ok:
            await interaction.followup.send("❌ Failed to deactivate character.", ephemeral=True)
            return

        log_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
        if log_ch:
            embed = discord.Embed(
                title="⏸️ Character Deactivated",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Player", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False)
            embed.add_field(name="Character", value=self.selected_char_name, inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await interaction.followup.send(
            f"✅ Character **{self.selected_char_name}** has been deactivated.", ephemeral=True
        )
        self.stop()


class ReactivateCharacterView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, chars: list[dict]):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.chars = chars
        self.selected_char_id: Optional[str] = None
        self.selected_char_name: Optional[str] = None

        options = [
            discord.SelectOption(label=c["name"][:100], value=str(c["character_id"]))
            for c in chars[:25]
        ]
        char_select = discord.ui.Select(
            placeholder="Choose a character to reactivate…",
            options=options,
            row=0,
        )
        char_select.callback = self._on_select
        self.add_item(char_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def _on_select(self, interaction: discord.Interaction):
        val = interaction.data["values"][0]
        self.selected_char_id = val
        for c in self.chars:
            if str(c["character_id"]) == val:
                self.selected_char_name = c["name"]
                break
        await interaction.response.send_message(
            f"Selected: **{self.selected_char_name}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Confirm Reactivate", style=discord.ButtonStyle.success, emoji="▶️", row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_char_id is None:
            await interaction.response.send_message("Please select a character first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await reactivate_character(self.selected_char_id, user_id=str(interaction.user.id))
        if not ok:
            await interaction.followup.send("❌ Failed to reactivate character.", ephemeral=True)
            return

        log_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
        if log_ch:
            embed = discord.Embed(
                title="▶️ Character Reactivated",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Player", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False)
            embed.add_field(name="Character", value=self.selected_char_name, inline=False)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await interaction.followup.send(
            f"✅ Character **{self.selected_char_name}** has been reactivated.", ephemeral=True
        )
        self.stop()


class TradeConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

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


class TradeSetupView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_buyer: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None
        self.selected_buyer_char_name: Optional[str] = None
        self._buyer_char_select = None

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
            row=2,
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

        self.selected_buyer_char_name = None
        if self._buyer_char_select is not None:
            self.remove_item(self._buyer_char_select)
            self._buyer_char_select = None

        buyer_chars = await get_active_characters(str(self.selected_buyer.id))
        if not buyer_chars:
            await interaction.response.send_message(
                f"❌ **{self.selected_buyer.display_name}** has no active characters and cannot receive items.",
                ephemeral=True,
            )
            self.selected_buyer = None
            return

        char_options = [
            discord.SelectOption(label=c["name"][:100], value=c["name"])
            for c in buyer_chars[:25]
        ]
        char_select = discord.ui.Select(
            placeholder="Choose buyer's character…",
            options=char_options,
            row=1,
        )
        char_select.callback = self._on_buyer_char_select
        self._buyer_char_select = char_select
        self.add_item(char_select)

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Buyer: **{self.selected_buyer.display_name}** ✓ — Now select their character.",
            ephemeral=True,
        )

    async def _on_buyer_char_select(self, interaction: discord.Interaction):
        self.selected_buyer_char_name = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Buyer's character: **{self.selected_buyer_char_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        await interaction.response.send_message(
            f"Item: **{g['name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_buyer is None:
            await interaction.response.send_message("Please select a buyer first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select an item first.", ephemeral=True)
            return
        if self.selected_buyer_char_name is None:
            await interaction.response.send_message("Please select the buyer's character first.", ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter the sale price** (number only, `0` for free), or type `cancel`:",
            ephemeral=True,
        )
        price_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if price_text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            self.stop()
            return
        try:
            price = int(price_text.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Price must be a number.", ephemeral=True)
            self.stop()
            return
        if price < 0:
            await interaction.followup.send("❌ Price cannot be negative.", ephemeral=True)
            self.stop()
            return

        item_char = selected_item.get("character_name", "")
        if item_char:
            seller_char = item_char
        else:
            seller_chars = await get_active_characters(str(interaction.user.id))
            if not seller_chars:
                await interaction.followup.send("❌ You have no active characters.", ephemeral=True)
                self.stop()
                return
            if len(seller_chars) == 1:
                seller_char = seller_chars[0]["name"]
            else:
                char_view = _SenderCharSelectView(interaction.user.id, seller_chars)
                await interaction.followup.send(
                    "📝 **Which of your characters is selling this item?**",
                    view=char_view,
                    ephemeral=True,
                )
                await char_view.wait()
                if char_view.selected_name is None:
                    await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                    self.stop()
                    return
                seller_char = char_view.selected_name

        await _process_trade(
            self.cog, interaction, self.selected_buyer, group,
            self.selected_buyer_char_name, price, seller_char,
        )
        self.stop()


async def _process_trade(cog, interaction, buyer, group, buyer_character, price, seller_character=None):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    if not cog._inv_system_enabled():
        await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
        return

    if buyer.id == interaction.user.id:
        await interaction.followup.send(
            "❌ You cannot trade items to yourself.",
            ephemeral=True,
        )
        return

    if not buyer_character:
        await interaction.followup.send("❌ Buyer character name is required.", ephemeral=True)
        return

    selected_item = group["items"][0]
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

    inv_cog = cog.bot.cogs.get("PlayerInventory")

    if buyer.id != interaction.user.id:
        price_str = f"**${price:,}**" if price > 0 else "**free**"
        confirm_view = TradeConfirmView(recipient_id=buyer.id, timeout=60)
        try:
            dm_msg = await buyer.send(
                f"**{interaction.user.display_name}** wants to trade you **{item_name}** "
                f"for {price_str} (character: **{buyer_character}**).\n"
                "Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
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

        live_item = await pi_get_item(item_id)
        if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ **{item_name}** is no longer in your inventory. Trade cancelled.",
                ephemeral=True,
            )
            return

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
            alert_ch = await _log_channel(cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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

    buyer_char_record = await get_character_by_name(str(buyer.id), buyer_character)
    buyer_char_id = buyer_char_record["character_id"] if buyer_char_record else None
    ok_transfer = await pi_update_owner(
        item_id, str(buyer.id), buyer_character, str(interaction.user.id),
        new_character_id=buyer_char_id,
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
            alert_ch = await _log_channel(cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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
                    {"bank": -price},
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

    log_ch = await _route_log_channel(cog.bot, item_type)
    if log_ch:
        seller_char = seller_character or selected_item.get("character_name") or "—"
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
            "seller_character": seller_character or selected_item.get("character_name", ""),
        },
    )

    price_str = f"for **${price:,}**" if price else "for free"
    await interaction.followup.send(
        f"✅ Traded **{item_name}** to **{buyer_character}** ({buyer.display_name}) {price_str}.",
        ephemeral=True,
    )


class GiveSetupView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_recipient: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None
        self.selected_recipient_char_name: Optional[str] = None
        self._recipient_char_select = None
        self._is_ripperdoc_recipient = False

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

        self.selected_recipient_char_name = None
        if self._recipient_char_select is not None:
            self.remove_item(self._recipient_char_select)
            self._recipient_char_select = None

        target_roles = getattr(self.selected_recipient, "roles", [])
        self._is_ripperdoc_recipient = any(
            getattr(r, "id", None) == getattr(config, "RIPPERDOC_ROLE_ID", None)
            for r in target_roles
        )

        if self._is_ripperdoc_recipient:
            await interaction.response.send_message(
                f"Recipient: **{self.selected_recipient.display_name}** (Ripperdoc) ✓", ephemeral=True
            )
            return

        recipient_chars = await get_active_characters(str(self.selected_recipient.id))
        if not recipient_chars:
            await interaction.response.send_message(
                f"❌ **{self.selected_recipient.display_name}** has no active characters and cannot receive items.",
                ephemeral=True,
            )
            self.selected_recipient = None
            return

        char_options = [
            discord.SelectOption(label=c["name"][:100], value=c["name"])
            for c in recipient_chars[:25]
        ]
        char_select = discord.ui.Select(
            placeholder="Choose recipient's character…",
            options=char_options,
            row=2,
        )
        char_select.callback = self._on_recipient_char_select
        self._recipient_char_select = char_select
        self.add_item(char_select)

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Recipient: **{self.selected_recipient.display_name}** ✓ — Now select their character.",
            ephemeral=True,
        )

    async def _on_recipient_char_select(self, interaction: discord.Interaction):
        self.selected_recipient_char_name = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Recipient's character: **{self.selected_recipient_char_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        await interaction.response.send_message(
            f"Item: **{g['name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_recipient is None:
            await interaction.response.send_message("Please select a recipient first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select an item first.", ephemeral=True)
            return
        if not self._is_ripperdoc_recipient and self.selected_recipient_char_name is None:
            await interaction.response.send_message("Please select the recipient's character first.", ephemeral=True)
            return
        group = self.all_groups[self.selected_group_idx]
        selected_item = group["items"][0]
        item_char = selected_item.get("character_name", "")

        if item_char:
            sender_char = item_char
            await interaction.response.defer(ephemeral=True)
            await _process_give(
                self.cog, interaction, self.selected_recipient, group,
                self.selected_recipient_char_name or "", sender_char,
            )
            self.stop()
            return

        await interaction.response.defer(ephemeral=True)
        sender_chars = await get_active_characters(str(interaction.user.id))
        if not sender_chars:
            await interaction.followup.send("❌ You have no active characters.", ephemeral=True)
            self.stop()
            return
        if len(sender_chars) == 1:
            sender_char = sender_chars[0]["name"]
        else:
            char_view = _SenderCharSelectView(interaction.user.id, sender_chars)
            await interaction.followup.send(
                "📝 **Which of your characters is giving this item?**",
                view=char_view,
                ephemeral=True,
            )
            await char_view.wait()
            if char_view.selected_name is None:
                await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
                self.stop()
                return
            sender_char = char_view.selected_name

        await _process_give(
            self.cog, interaction, self.selected_recipient, group,
            self.selected_recipient_char_name or "", sender_char,
        )
        self.stop()


class _SenderCharSelectView(SafeView):
    def __init__(self, author_id: int, characters: list):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.selected_name: Optional[str] = None
        options = [
            discord.SelectOption(label=c["name"][:100], value=c["name"])
            for c in characters[:25]
        ]
        select = discord.ui.Select(placeholder="Choose your character…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_name = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=f"Character: **{self.selected_name}** ✓", view=None
        )
        self.stop()


async def _process_give(cog, interaction, target, group, receiver_character, sender_char):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    if not cog._inv_system_enabled():
        await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
        return

    if not sender_char:
        await interaction.followup.send("❌ Your character name is required.", ephemeral=True)
        return

    selected_item = group["items"][0]
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
        receiver_char = None
        dm_description = (
            f"**{interaction.user.display_name}** ({sender_char}) wants to give you:\n"
            f"• **{item_name}** ({item_type})\n"
            f"This will be added to your ripperdoc stock.\n\n"
            "Do you accept this item?"
        )
    else:
        receiver_char = receiver_character
        if not receiver_char:
            await interaction.followup.send(
                "❌ Recipient's character name is required for player-to-player gives.",
                ephemeral=True,
            )
            return
        dm_description = (
            f"**{interaction.user.display_name}** ({sender_char}) wants to give you:\n"
            f"• **{item_name}** ({item_type})\n"
            f"Receiving character: **{receiver_char}**\n\n"
            "Do you accept this item?"
        )

    confirm_view = GiveConfirmView(recipient_id=target.id, timeout=120)
    try:
        dm_msg = await target.send(dm_description, view=confirm_view)
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            f"❌ Cannot DM {target.display_name}. They may have DMs disabled.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"📩 Give offer sent to {target.display_name} via DM. Waiting for their response…",
        ephemeral=True,
    )
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="You declined or the offer timed out.", view=None)
        except Exception:
            pass
        await interaction.followup.send(
            f"❌ {target.display_name} declined or didn't respond in time. Give cancelled.",
            ephemeral=True,
        )
        return
    try:
        await dm_msg.edit(view=None)
    except Exception:
        pass

    live_item = await pi_get_item(item_id)
    if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
        await interaction.followup.send(
            f"❌ **{item_name}** is no longer in your inventory.",
            ephemeral=True,
        )
        return

    if item_type == "cyberware" and is_ripperdoc_target:
        cw_cog = cog.bot.cogs.get("CyberwareShop")
        if cw_cog is None:
            await interaction.followup.send("❌ CyberwareShop cog unavailable. Contact an admin.", ephemeral=True)
            return

        ok_del = await pi_delete_item(item_id, expected_owner_id=str(interaction.user.id))
        if not ok_del:
            await interaction.followup.send("❌ Failed to remove item from your inventory.", ephemeral=True)
            return

        async with cw_cog._locks.acquire(str(target.id)):
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

        log_ch = await _log_channel(cog.bot, "CYBERWARE_LOG_CHANNEL_ID")
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
        try:
            await target.send(f"✅ **{item_name}** has been added to your ripperdoc stock from **{interaction.user.display_name}**.")
        except Exception:
            pass
        return

    recv_char_record = await get_character_by_name(str(target.id), receiver_char)
    recv_char_id = recv_char_record["character_id"] if recv_char_record else None
    ok = await pi_update_owner(
        item_id, str(target.id), receiver_char, str(interaction.user.id),
        new_character_id=recv_char_id,
    )
    if not ok:
        await interaction.followup.send("❌ Failed to transfer item. Please try again.", ephemeral=True)
        return

    log_ch = await _route_log_channel(cog.bot, item_type)
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
    try:
        await target.send(
            f"✅ You received **{item_name}** from **{sender_char}** ({interaction.user.display_name})."
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


class GiveConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="You accepted the item.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="You declined the item.", view=None)
        self.stop()


class SellToStoreSetupView(SafeView):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list, seller_chars: list | None = None, store_entries: list | None = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_store_value: Optional[str] = None
        self.selected_group_idx: Optional[int] = None
        self.selected_seller_char_name: Optional[str] = None
        self.seller_chars = seller_chars
        self.store_entries = store_entries or []
        self._store_select = None

        options = []
        for i, g in enumerate(all_groups[:25]):
            item = g["items"][0]
            item_type = item.get("item_type", "gun")
            char = item.get("character_name", "")
            restriction = item.get("restriction", "basic")
            r_tag = f" [{restriction}]" if restriction != "basic" else ""
            count = g.get("count", 1)
            count_str = f" ×{count}" if count > 1 else ""
            char_str = f" ({char})" if char else ""
            type_tag = f" [{item_type}]" if item_type != "gun" else ""
            label = f"{g['name']}{count_str}{r_tag}{type_tag}{char_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))

        item_select = discord.ui.Select(
            placeholder="Choose an item to sell…",
            options=options,
            row=1,
        )
        item_select.callback = self._on_item_select
        self.add_item(item_select)

        self._build_store_select()

        if seller_chars:
            char_options = [
                discord.SelectOption(label=c["name"][:100], value=c["name"])
                for c in seller_chars[:25]
            ]
            seller_char_select = discord.ui.Select(
                placeholder="Which character is selling?",
                options=char_options,
                row=2,
            )
            seller_char_select.callback = self._on_seller_char_select
            self.add_item(seller_char_select)

    def _build_store_select(self, filter_type: Optional[str] = None):
        if self._store_select is not None:
            self.remove_item(self._store_select)
            self._store_select = None

        if filter_type == "gun":
            entries = [e for e in self.store_entries if e["store_type"] == "gun"]
        elif filter_type == "cyberware":
            entries = [e for e in self.store_entries if e["store_type"] == "ripperdoc"]
        else:
            entries = self.store_entries

        options = []
        for e in entries[:25]:
            options.append(discord.SelectOption(
                label=e["label"][:100],
                description=e["description"][:100],
                value=e["value"],
            ))

        if not options:
            options = [discord.SelectOption(label="No compatible stores available", value="__none__")]

        store_select = discord.ui.Select(
            placeholder="Choose a store…",
            options=options,
            row=0,
        )
        store_select.callback = self._on_store_select
        self._store_select = store_select
        self.add_item(store_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_store_select(self, interaction: discord.Interaction):
        val = interaction.data["values"][0]
        if val == "__none__":
            await interaction.response.send_message("No stores available for this item type.", ephemeral=True)
            return
        self.selected_store_value = val
        entry = next((e for e in self.store_entries if e["value"] == val), None)
        store_label = entry["label"] if entry else val
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Store: **{store_label}** ✓", ephemeral=True
        )

    async def _on_seller_char_select(self, interaction: discord.Interaction):
        self.selected_seller_char_name = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Selling character: **{self.selected_seller_char_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        item = g["items"][0]
        item_type = item.get("item_type", "gun")
        self.selected_store_value = None
        self._build_store_select(filter_type=item_type)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Item: **{g['name']}** ✓ — Now select a store.", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_store_value is None:
            await interaction.response.send_message("Please select a store first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select an item first.", ephemeral=True)
            return
        if self.seller_chars and not self.selected_seller_char_name:
            await interaction.response.send_message("Please select a selling character first.", ephemeral=True)
            return
        entry = next((e for e in self.store_entries if e["value"] == self.selected_store_value), None)
        if not entry:
            await interaction.response.send_message("❌ Invalid store selection.", ephemeral=True)
            return
        owner_id = entry["owner_id"]
        store_type = entry["store_type"]
        group = self.all_groups[self.selected_group_idx]
        item_type = group["items"][0].get("item_type", "gun")
        if item_type == "gun" and store_type != "gun":
            await interaction.response.send_message("❌ You can only sell guns to gun stores.", ephemeral=True)
            return
        if item_type == "cyberware" and store_type != "ripperdoc":
            await interaction.response.send_message("❌ You can only sell cyberware to ripperdoc stores.", ephemeral=True)
            return
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message("Must be used in a server.", ephemeral=True)
            return
        store_owner = guild.get_member(owner_id)
        if not store_owner:
            try:
                store_owner = await guild.fetch_member(owner_id)
            except Exception:
                await interaction.response.send_message("❌ Could not find the store owner in the server.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "📝 **Enter the asking price** (number only, `0` for free), or type `cancel`:",
            ephemeral=True,
        )
        price_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if price_text is None:
            await interaction.followup.send("⏰ Timed out or cancelled.", ephemeral=True)
            self.stop()
            return
        try:
            price = int(price_text.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Price must be a number.", ephemeral=True)
            self.stop()
            return
        if price < 0:
            await interaction.followup.send("❌ Price cannot be negative.", ephemeral=True)
            self.stop()
            return
        await _process_sell_to_store(
            self.cog, interaction, store_owner, group,
            self.selected_seller_char_name or "", price, store_type=store_type,
        )
        self.stop()


class StoreBuyConfirmView(SafeView):
    def __init__(self, recipient_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.recipient_id = recipient_id
        self.accepted: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="You accepted the purchase.", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content="You declined the purchase.", view=None)
        self.stop()


async def _process_sell_to_store(cog, interaction, store_owner, group, seller_character, price, store_type="gun"):
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Must be used in server.", ephemeral=True)
        return
    if not cog._inv_system_enabled():
        await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
        return

    if store_owner.id == interaction.user.id:
        await interaction.followup.send("❌ You cannot sell items to yourself.", ephemeral=True)
        return

    selected_item = group["items"][0]
    item_name = selected_item["name"]
    item_id = selected_item["item_id"]
    item_type = selected_item.get("item_type", "gun")
    restriction = selected_item.get("restriction", "basic")
    character_name = seller_character or selected_item.get("character_name", "")

    if store_type == "ripperdoc" and item_type != "cyberware":
        await interaction.followup.send(
            "❌ Only cyberware can be sold to ripperdoc stores.", ephemeral=True
        )
        return
    if store_type == "gun" and item_type not in ("gun", "misc"):
        await interaction.followup.send(
            "❌ This item type cannot be sold to a gun store.", ephemeral=True
        )
        return

    live_item = await pi_get_item(item_id)
    if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
        await interaction.followup.send(
            f"❌ **{item_name}** is no longer in your inventory.",
            ephemeral=True,
        )
        return

    if store_type == "ripperdoc":
        cw_cog = cog.bot.cogs.get("CyberwareShop")
        if not cw_cog:
            await interaction.followup.send("❌ Cyberware shop system unavailable.", ephemeral=True)
            return
    else:
        guns_cog = cog.bot.cogs.get("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("❌ Gun shop system unavailable.", ephemeral=True)
            return

    price_str = f"**${price:,}**" if price > 0 else "**free**"
    confirm_view = StoreBuyConfirmView(recipient_id=store_owner.id, timeout=60)
    try:
        dm_msg = await store_owner.send(
            f"**{interaction.user.display_name}** wants to sell you **{item_name}** "
            f"for {price_str}.\n"
            f"Restriction: **{restriction}**\n"
            "Do you want to buy it for your store?",
            view=confirm_view,
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            f"❌ Cannot DM {store_owner.display_name}. They may have DMs disabled.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"📩 Offer sent to {store_owner.display_name} via DM. Waiting…",
        ephemeral=True,
    )
    await confirm_view.wait()

    if not confirm_view.accepted:
        try:
            await dm_msg.edit(content="Purchase declined or timed out.", view=None)
        except Exception:
            pass
        await interaction.followup.send(
            f"❌ {store_owner.display_name} declined or didn't respond.",
            ephemeral=True,
        )
        return
    try:
        await dm_msg.edit(view=None)
    except Exception:
        pass

    live_item = await pi_get_item(item_id)
    if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
        await interaction.followup.send(
            f"❌ **{item_name}** is no longer in your inventory. Sale cancelled.",
            ephemeral=True,
        )
        return

    inv_cog = cog.bot.cogs.get("PlayerInventory")
    b_cash_deduct = 0
    b_bank_deduct = 0

    if price > 0:
        ub = getattr(inv_cog, "unbelievaboat", None) if inv_cog else None
        if not ub:
            await interaction.followup.send("❌ Economy system unavailable.", ephemeral=True)
            return
        owner_balance = await ub.get_balance(store_owner.id)
        if owner_balance is None:
            await interaction.followup.send("❌ Could not fetch store owner's balance.", ephemeral=True)
            return

        o_cash = int(owner_balance.get("cash", 0))
        o_bank = int(owner_balance.get("bank", 0))
        if o_cash + o_bank < price:
            await interaction.followup.send(
                f"❌ {store_owner.display_name} cannot afford **${price:,}**.",
                ephemeral=True,
            )
            return

        b_cash_deduct = min(max(o_cash, 0), price)
        b_bank_deduct = max(0, price - b_cash_deduct)

        ok_buyer = await ub.update_balance(
            store_owner.id,
            {"cash": -b_cash_deduct, "bank": -b_bank_deduct},
            reason=f"Store purchase: {item_name} from {interaction.user.display_name}",
        )
        if not ok_buyer:
            await interaction.followup.send("❌ Failed to deduct from store owner's balance.", ephemeral=True)
            return

        ok_seller = await ub.update_balance(
            interaction.user.id,
            {"cash": price},
            reason=f"Sold gun to store: {item_name} to {store_owner.display_name}",
        )
        if not ok_seller:
            logger.error(
                "sell_to_store: owner debited but seller credit failed — seller=%s owner=%s item=%s",
                interaction.user.id, store_owner.id, item_id,
            )
            pt_id = str(uuid.uuid4())
            await pt_create({
                "transfer_id": pt_id,
                "seller_id": str(interaction.user.id),
                "buyer_id": str(store_owner.id),
                "item_id": item_id,
                "amount": price,
            })
            alert_ch = await _log_channel(cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
            if alert_ch:
                await alert_ch.send(
                    f"🚨 **PENDING STORE PURCHASE** — seller credit failed!\n"
                    f"Transfer ID: `{pt_id}`\n"
                    f"Seller: {interaction.user.mention} | Store Owner: {store_owner.mention}\n"
                    f"Item: **{item_name}** | Amount: **${price:,}**\n"
                    "Store owner has been debited. Please resolve manually.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await interaction.followup.send(
                "⚠️ Store owner was charged but seller payout failed. "
                "This has been flagged for admin review.",
                ephemeral=True,
            )
            return

    ok_delete = await pi_delete_item(item_id, expected_owner_id=str(interaction.user.id))
    if not ok_delete:
        if price > 0:
            ub = getattr(inv_cog, "unbelievaboat", None) if inv_cog else None
            if ub:
                await ub.update_balance(
                    store_owner.id,
                    {"cash": b_cash_deduct, "bank": b_bank_deduct},
                    reason=f"Store buy refund (DB failure): {item_name}",
                )
                await ub.update_balance(
                    interaction.user.id,
                    {"bank": -price},
                    reason=f"Store sale refund (DB failure): {item_name}",
                )
        await interaction.followup.send(
            "❌ Failed to remove item from your inventory. Refunds attempted. Please contact an admin.",
            ephemeral=True,
        )
        return

    if store_type == "ripperdoc":
        cw_cog = cog.bot.cogs.get("CyberwareShop")
        try:
            async with cw_cog._locks.acquire(str(store_owner.id)):
                rd_inventory = await cw_cog._load_inventory(store_owner.id)
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
                ok_save = await cw_cog._save_inventory(store_owner.id, rd_inventory)
            if not ok_save:
                raise RuntimeError("_save_inventory returned falsy")
        except Exception:
            logger.error(
                "sell_to_store: ripperdoc inventory save failed — seller=%s owner=%s item=%s",
                interaction.user.id, store_owner.id, item_id,
            )
            pt_id = str(uuid.uuid4())
            await pt_create({
                "transfer_id": pt_id,
                "seller_id": str(interaction.user.id),
                "buyer_id": str(store_owner.id),
                "item_id": item_id,
                "amount": price,
                "reason": f"Ripperdoc inventory save failed: {item_name}",
            })
            alert_ch = await _log_channel(cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
            if alert_ch:
                await alert_ch.send(
                    f"🚨 **STORE PURCHASE — ripperdoc inventory save failed!**\n"
                    f"Transfer ID: `{pt_id}`\n"
                    f"Seller: {interaction.user.mention} | Store Owner: {store_owner.mention}\n"
                    f"Item: **{item_name}** | Amount: **${price:,}**\n"
                    "Item removed from seller; payment processed. Ripperdoc inventory NOT updated. Resolve manually.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await interaction.followup.send(
                "⚠️ Payment processed and item removed, but store inventory update failed. "
                "This has been flagged for admin review.",
                ephemeral=True,
            )
            return

        rd_store_id = f"rd:{guild.id}:{store_owner.id}"
        log_ch = await _route_log_channel(cog.bot, "cyberware")
        if log_ch:
            embed = discord.Embed(
                title="🏪 Cyberware Sold to Ripperdoc Store",
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Seller",
                value=f"{interaction.user.mention} ({interaction.user.display_name})"
                      + (f" — {character_name}" if character_name else ""),
                inline=False,
            )
            embed.add_field(
                name="Ripperdoc Store Owner",
                value=f"{store_owner.mention} ({store_owner.display_name})",
                inline=False,
            )
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Price", value=f"${price:,}" if price else "Free", inline=True)
            embed.set_footer(text="NightCityBot Audit Log")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ih_record_event(
            item_id, "sold_to_store",
            actor_id=str(interaction.user.id),
            target_id=str(store_owner.id),
            price=price,
            metadata={
                "item_name": item_name,
                "item_type": item_type,
                "store_id": rd_store_id,
                "character_name": character_name,
                "routed_to": "ripperdoc_stock",
            },
        )

        price_str = f"for **${price:,}**" if price else "for free"
        await interaction.followup.send(
            f"✅ Sold **{item_name}** to **{store_owner.display_name}**'s ripperdoc store {price_str}.",
            ephemeral=True,
        )
        return

    guns_cog = cog.bot.cogs.get("GunsShopCog")
    store_id = guns_cog._store_id(guild.id, store_owner.id)

    lot_id = f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    weapon_type = ""
    if hasattr(guns_cog, "_derive_weapon_type"):
        weapon_type = guns_cog._derive_weapon_type(item_name, "") or ""
    store_lot = {
        "lot_id": lot_id,
        "gun_name": item_name,
        "gun_level": selected_item.get("gun_level", ""),
        "weapon_type": weapon_type,
        "unit_cost": price,
        "qty_remaining": 1,
        "restriction": restriction,
        "item_ids": [item_id],
    }

    try:
        async with guns_cog.lock:
            state = await guns_cog._load_state()
            store = state.setdefault("stores", {}).setdefault(
                store_id, {"owner_id": store_owner.id, "lots": []}
            )
            store["lots"].append(store_lot)
            await guns_cog._save_state(state)
    except Exception:
        logger.error(
            "sell_to_store: store lot save failed — seller=%s owner=%s item=%s",
            interaction.user.id, store_owner.id, item_id,
        )
        pt_id = str(uuid.uuid4())
        await pt_create({
            "transfer_id": pt_id,
            "seller_id": str(interaction.user.id),
            "buyer_id": str(store_owner.id),
            "item_id": item_id,
            "amount": price,
            "reason": f"Store lot save failed: {item_name}",
        })
        alert_ch = await _log_channel(cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
        if alert_ch:
            await alert_ch.send(
                f"🚨 **STORE PURCHASE — lot save failed!**\n"
                f"Transfer ID: `{pt_id}`\n"
                f"Seller: {interaction.user.mention} | Store Owner: {store_owner.mention}\n"
                f"Item: **{item_name}** | Amount: **${price:,}**\n"
                "Item removed from seller; payment processed. Store lot NOT saved. Resolve manually.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.followup.send(
            "⚠️ Payment processed and item removed, but store inventory update failed. "
            "This has been flagged for admin review.",
            ephemeral=True,
        )
        return

    log_ch = await _route_log_channel(cog.bot, "gun")
    if log_ch:
        embed = discord.Embed(
            title="🏪 Gun Sold to Store",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Seller",
            value=f"{interaction.user.mention} ({interaction.user.display_name})"
                  + (f" — {character_name}" if character_name else ""),
            inline=False,
        )
        embed.add_field(
            name="Store Owner",
            value=f"{store_owner.mention} ({store_owner.display_name})",
            inline=False,
        )
        embed.add_field(name="Gun", value=f"**{item_name}**", inline=True)
        embed.add_field(name="Price", value=f"${price:,}" if price else "Free", inline=True)
        if restriction != "basic":
            embed.add_field(name="Restriction", value=restriction.title(), inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    await ih_record_event(
        item_id, "sold_to_store",
        actor_id=str(interaction.user.id),
        target_id=str(store_owner.id),
        price=price,
        metadata={
            "item_name": item_name,
            "item_type": item_type,
            "restriction": restriction,
            "store_id": store_id,
            "lot_id": lot_id,
            "character_name": character_name,
        },
    )

    price_str = f"for **${price:,}**" if price else "for free"
    await interaction.followup.send(
        f"✅ Sold **{item_name}** to **{store_owner.display_name}**'s store {price_str}.",
        ephemeral=True,
    )
