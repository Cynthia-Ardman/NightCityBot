"""Unified !fixer hub — interactive panel for Fixer-level management.

Three top-level categories: Player, Store, Wholesaler.
Each opens a sub-menu with relevant actions.
"""
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView, send_ephemeral, respond_ephemeral, log_panel_failure
from NightCityBot.utils.db import (
    pi_add_item,
    pi_get_item,
    pi_get_by_owner,
    pi_delete_item,
    pi_update_owner,
    pi_update_character,
    ih_record_event,
    ih_get_history,
    pt_create,
    mission_log_get,
    mission_log_record,
    mission_log_remove_date,
    mission_event_create,
    mission_event_list_recent,
    mission_event_list_active,
    mission_event_get,
    mission_event_update,
    mission_event_cancel,
    mission_event_get_for_user,
    actor_attendance_record,
    actor_attendance_get_by_user,
    bot_config_get,
    bot_config_set,
)
from zoneinfo import ZoneInfo
from NightCityBot.cogs.missions import (
    _format_check_line,
    _parse_date_token,
    compute_payout_ts,
)
from NightCityBot.utils.characters import get_active_characters, ensure_character_active, get_character_by_name
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.inline_helpers import collect_text_input
from NightCityBot.utils.panel_context import PanelContext
from NightCityBot.utils.constants import VALID_GUN_CLASSES, GUN_CLASS_DISPLAY_NAMES

logger = logging.getLogger(__name__)

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


def _fmt_actor_line(uid: Optional[str], uname: Optional[str]) -> str:
    if not uid:
        return f"`{uname or '?'}`"
    label = uname or uid
    return f"<@{uid}> (`{label}`, `{uid}`)"


def _fmt_attendee_list(ids: list[str], max_chars: int = 1000) -> str:
    if not ids:
        return "_(none)_"
    line = " ".join(f"<@{uid}>" for uid in ids)
    if len(line) > max_chars:
        line = line[: max_chars - 1] + "…"
    return f"({len(ids)}) {line}"


async def post_mission_audit(
    bot: commands.Bot,
    *,
    action: str,
    actor: "Optional[discord.abc.User]" = None,
    actor_id: Optional[str] = None,
    actor_name: Optional[str] = None,
    mission_id: Optional[str] = None,
    mission_name: Optional[str] = None,
    fields: Optional[list[tuple[str, str]]] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    color: Optional[discord.Color] = None,
) -> None:
    """Best-effort post a mission-audit embed to NIGHTCITYBOT_LOG_CHANNEL_ID.

    Never raises — logs to stderr if the channel can't be reached or the
    embed send fails, so caller flows are never disrupted by audit issues.
    """
    try:
        ch = await _audit_channel(bot)
        if ch is None:
            return
        embed = discord.Embed(
            title=f"📓 {action}",
            color=color or discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if actor is not None:
            uid = str(getattr(actor, "id", "") or "")
            uname = (
                getattr(actor, "display_name", None)
                or getattr(actor, "name", None)
                or uid
            )
            embed.add_field(name="Actor", value=_fmt_actor_line(uid, uname), inline=False)
        elif actor_id or actor_name:
            embed.add_field(
                name="Actor",
                value=_fmt_actor_line(actor_id, actor_name),
                inline=False,
            )
        if mission_name or mission_id:
            mid_part = f"\n`{mission_id}`" if mission_id else ""
            embed.add_field(
                name="Mission",
                value=f"**{mission_name or '?'}**{mid_part}"[:1024],
                inline=False,
            )
        for name, value in (fields or []):
            if value is None or value == "":
                continue
            embed.add_field(name=name[:256], value=str(value)[:1024], inline=False)
        if before is not None:
            embed.add_field(name="Before", value=str(before)[:1024], inline=False)
        if after is not None:
            embed.add_field(name="After", value=str(after)[:1024], inline=False)
        await ch.send(embed=embed)
    except Exception:
        logger.error("Failed to post mission audit log (%s)", action, exc_info=True)


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
            await respond_ephemeral(interaction, "Could not verify your role.")
            return False
        allowed_role_ids = {
            int(config.FIXER_ROLE_ID),
            int(getattr(config, "TRIAL_FIXER_ROLE_ID", 0) or 0),
        }
        allowed_role_ids.discard(0)
        if not (any(r.id in allowed_role_ids for r in member.roles) or member.guild_permissions.administrator):
            await respond_ephemeral(interaction, "This panel is for Fixers only.")
            await log_panel_failure(interaction.client, "NIGHTCITYBOT_LOG_CHANNEL_ID", "Fixer Panel", interaction.user, "Missing fixer role")
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
        await respond_ephemeral(interaction, embed=embed, view=view)

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
        await respond_ephemeral(interaction, embed=embed, view=view)

    @discord.ui.button(label="Wholesaler", style=discord.ButtonStyle.primary, emoji="🏭", row=0, custom_id="fixer:wholesaler_menu")
    async def wholesaler_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("FixerHub")
        ctx = PanelContext(interaction)
        view = WholesalerSubView(cog, ctx)
        embed = discord.Embed(
            title="🏭 Fixer Panel — Wholesaler",
            description=(
                "**View Stock** — See current gun + cyberware catalogue and custom Fixer-added lots\n"
                "**Add Gun** — Add a custom gun lot (overlays the catalogue)\n"
                "**Add Cyberware** — Add a custom cyberware lot (overlays the catalogue)"
            ),
            color=discord.Color.orange(),
        )
        await respond_ephemeral(interaction, embed=embed, view=view)

    @discord.ui.button(label="Missions", style=discord.ButtonStyle.primary, emoji="🎯", row=1, custom_id="fixer:missions_menu")
    async def missions_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("FixerHub")
        ctx = PanelContext(interaction)
        view = MissionsSubView(cog, ctx)
        embed = discord.Embed(
            title="🎯 Fixer Panel — Missions",
            description=(
                "**🔎 Check Missions** — Mission count, last date, and recent "
                "mission titles + the fixer who created each one.\n"
                "**✅ Record Mission** — Log today's mission for one or more "
                "players (use `!mission_record … date=YYYY-MM-DD` for a "
                "custom date).\n"
                "**🆕 Create Mission** — Short modal for name / pay / "
                "location / optional description (shown on the Discord "
                "event card), then dropdowns for date, start hour, duration, and timezone "
                "(defaults to your last-used). Schedules a Discord 'Actors "
                "Needed: …' event, credits each attendee's gig log "
                "immediately, and auto-pays everyone at the next midnight "
                "US-Eastern after the start time. The fixer who created the "
                "mission is never paid. Tap **📎 Attach Banner** on the "
                "attendees screen for a custom cover image.\n"
                "**🎭 Actor Pay** — Pick actor(s) and a recorded mission, "
                "then enter a per-actor amount paid via UnbelievaBoat.\n"
                "**🔎 Check Actor** — How many times someone has acted, the "
                "dates, and which missions + fixer they acted for.\n"
                "**✏️ Edit Mission** — Only lists **active** missions (not "
                "yet paid, not canceled). Change date/time, attendees, or "
                "payout — or cancel the mission entirely. Attendee edits and "
                "cancellations have optional gig-log credit toggles."
            ),
            color=discord.Color.gold(),
        )
        await respond_ephemeral(interaction, embed=embed, view=view)


class WholesalerSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="View Stock", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def view_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show the stock that storefronts can buy from.

        Under the full-catalog model this is: catalog items (unlimited stock)
        plus any Fixer custom-overlay additions in ``state["wholesale_lots"]``
        / ``state["cw_wholesale_lots"]`` (finite stock).

        Sends one embed per category (guns, cyberware) so the full catalog is
        visible — with safe truncation if either side exceeds Discord's
        4096-char description limit.
        """
        await interaction.response.defer(ephemeral=True)
        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        cw_cog = self.cog.bot.cogs.get("CyberwareShop")
        embeds: list[discord.Embed] = []

        def _fit_description(lines: list[str], cap: int = 3900) -> str:
            joined = "\n".join(lines)
            if len(joined) <= cap:
                return joined
            kept = []
            total = 0
            for ln in lines:
                if total + len(ln) + 1 > cap - 60:
                    break
                kept.append(ln)
                total += len(ln) + 1
            kept.append("\n_(list truncated — open a Gun/Ripper store panel to see the full catalog)_")
            return "\n".join(kept)

        if guns_cog:
            from NightCityBot.cogs.gunstore_hub import _build_combined_gun_lots
            from NightCityBot.utils.helpers import format_gun_lines_grouped
            combined = await _build_combined_gun_lots(guns_cog)
            available = [l for l in combined if int(l.get("qty_available", 0)) > 0]
            if available:
                gun_lines = format_gun_lines_grouped(available, qty_key="qty_available", max_items=500)
            else:
                gun_lines = ["_(empty)_"]
            embeds.append(discord.Embed(
                title="🔫 Gun Wholesale Stock",
                description=_fit_description(gun_lines),
                color=discord.Color.orange(),
            ))
        if cw_cog:
            from NightCityBot.cogs.ripperdoc_hub import _build_combined_cw_lots
            from NightCityBot.utils.helpers import format_cw_lines_grouped
            combined = await _build_combined_cw_lots(cw_cog)
            available = [l for l in combined if int(l.get("qty_available", 0)) > 0]
            if available:
                cw_lines = format_cw_lines_grouped(available, max_items=500)
            else:
                cw_lines = ["_(empty)_"]
            embeds.append(discord.Embed(
                title="💉 Cyberware Wholesale Stock",
                description=_fit_description(cw_lines),
                color=discord.Color.teal(),
            ))
        if not embeds:
            await send_ephemeral(interaction, "No wholesale systems available.")
            return
        await send_ephemeral(interaction, embeds=embeds)

    @discord.ui.button(label="Add Gun", style=discord.ButtonStyle.primary, emoji="🔫", row=1)
    async def add_gun(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        msg = await send_ephemeral(interaction,
            "📝 **Enter gun wholesale details** in this format:\n"
            "`gun name, quantity, unit cost, restriction, power level, type, gun class`\n"
            "Example: `Militech Mk.31, 10, 5000, basic, medium, power, pistol`\n"
            "• **Restriction:** basic / controlled / restricted\n"
            "• **Power Level:** low / medium / high\n"
            "• **Type:** power / smart / tech\n"
            "• **Gun Class:** " + ", ".join(f"`{c}`" for c in sorted(VALID_GUN_CLASSES)) + "\n"
            "Type `cancel` to abort.",
            wait=True)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await msg.edit(content="⏰ Timed out or cancelled.")
            return
        await _process_wh_add_gun(self.cog, interaction, text, msg)

    @discord.ui.button(label="Add Cyberware", style=discord.ButtonStyle.primary, emoji="💉", row=1)
    async def add_cw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        msg = await send_ephemeral(interaction,
            "📝 **Enter cyberware wholesale details** in this format:\n"
            "`cyberware name, quantity, unit cost, cwp, slot`\n"
            "Example: `Neural Link, 10, 5000, 14, neural`\n"
            "• **CWP:** Cyberware Power (integer)\n"
            "• **Slot:** " + ", ".join(sorted(VALID_CW_SLOTS)) + "\n"
            "Type `cancel` to abort.",
            wait=True)
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await msg.edit(content="⏰ Timed out or cancelled.")
            return
        await _process_wh_add_cw(self.cog, interaction, text, msg)


class PlayerSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=0)
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerInvPickerView(self.cog, self.ctx)
        await send_ephemeral(interaction, "Select a player to view their inventory:", view=view)

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerAddItemPickerView(self.cog, self.ctx)
        await send_ephemeral(interaction, "**Step 1** — Select the player to add an item to:", view=view)

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = PlayerRemoveItemView(self.cog, self.ctx)
        await send_ephemeral(interaction, 
            "**Remove Item** — Select the player, then enter the item UUID:",
            view=view)

    @discord.ui.button(label="Reassign Item", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def reassign_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ReassignSourcePickerView(self.cog, self.ctx)
        await send_ephemeral(interaction, 
            "✏️ **Reassign Item — Step 1** — Select the player who currently owns the item:",
            view=view)

    @discord.ui.button(label="Start LOA", style=discord.ButtonStyle.success, emoji="🏖️", row=1)
    async def start_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="start")
        await send_ephemeral(interaction, "Select a player to put on LOA:", view=view)

    @discord.ui.button(label="End LOA", style=discord.ButtonStyle.danger, emoji="🔚", row=1)
    async def end_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = LOAPickerView(self.cog, self.ctx, action="end")
        await send_ephemeral(interaction, "Select a player to take off LOA:", view=view)


class MissionsSubView(SafeView):
    """Sub-menu for mission tracking on the Fixer panel."""

    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Check Missions", style=discord.ButtonStyle.secondary, emoji="🔎", row=0)
    async def check_missions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = MissionCheckPickerView(self.cog, self.ctx)
        await send_ephemeral(
            interaction,
            "Pick up to 25 players to check their mission history:",
            view=view,
        )

    @discord.ui.button(label="Record Mission", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def record_mission(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = MissionRecordPickerView(self.cog, self.ctx)
        await send_ephemeral(
            interaction,
            "Pick up to 25 players to record **today's** mission for:",
            view=view,
        )

    @discord.ui.button(label="Create Mission", style=discord.ButtonStyle.primary, emoji="🆕", row=0)
    async def create_mission(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Pre-load the fixer's preferred timezone so the dropdown auto-fills.
        default_tz = await get_fixer_tz_pref(interaction.user.id)
        await interaction.response.send_modal(
            CreateMissionModal(self.cog, self.ctx, default_tz=default_tz)
        )

    @discord.ui.button(label="Actor Pay", style=discord.ButtonStyle.primary, emoji="🎭", row=1)
    async def actor_pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        recent = await mission_event_list_recent(25) or []
        view = ActorPayPickerView(self.cog, self.ctx, recent)
        prompt = (
            "Pick the actor(s), select the mission they acted in (or tap "
            "**✏️ Type Mission Name** to enter one manually), then click "
            "**Continue** to enter pay:"
        )
        if not recent:
            prompt = (
                "No missions on record yet — tap **✏️ Type Mission Name** "
                "to enter one manually, pick the actor(s), then **Continue** "
                "to enter pay."
            )
        await send_ephemeral(interaction, prompt, view=view)

    @discord.ui.button(label="Check Actor", style=discord.ButtonStyle.secondary, emoji="🔎", row=1)
    async def check_actor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = CheckActorPickerView(self.cog, self.ctx)
        await send_ephemeral(
            interaction,
            "Pick up to 25 users to view their actor history:",
            view=view,
        )

    @discord.ui.button(label="Edit Mission", style=discord.ButtonStyle.danger, emoji="✏️", row=2)
    async def edit_mission(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        # Only active missions (not paid, not canceled) are editable.
        active = await mission_event_list_active(25)
        if not active:
            await send_ephemeral(
                interaction,
                "No editable missions — only missions that haven't been paid out "
                "or canceled show up here. Create one with **Create Mission**.",
            )
            return
        view = EditMissionPickerView(self.cog, self.ctx, active)
        await send_ephemeral(
            interaction,
            "Pick a mission to edit (only upcoming / in-progress missions are listed):",
            view=view,
        )


class MissionCheckPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Choose player(s)…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        users = list(select.values)
        if not users:
            await send_ephemeral(interaction, "No players selected.")
            return
        today = datetime.now(timezone.utc).date()
        lines: list[str] = []
        for u in users:
            row = await mission_log_get(str(u.id))
            events = await mission_event_get_for_user(str(u.id), limit=25)
            display = getattr(u, "display_name", None) or getattr(u, "name", str(u.id))
            lines.append(_format_check_line(display, row, today, events))
        embed = discord.Embed(
            title="🎯 Mission History",
            description="\n".join(lines)[:4096],
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{len(users)} player(s) checked • as of {today.isoformat()}")
        await send_ephemeral(interaction, embed=embed)


class MissionRecordPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Choose player(s)…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        users = list(select.values)
        if not users:
            await send_ephemeral(interaction, "No players selected.")
            return
        today = datetime.now(timezone.utc).date()
        recorded: list[str] = []
        failed: list[str] = []
        for u in users:
            display = getattr(u, "display_name", None) or getattr(u, "name", str(u.id))
            result = await mission_log_record(str(u.id), display, today)
            if result is None:
                failed.append(display)
                continue
            recorded.append(
                f"• **{result.get('username') or display}** → "
                f"total {int(result.get('mission_count') or 0)} mission(s)"
            )
        body = f"Recorded mission on **{today.isoformat()} (today)**:\n" + "\n".join(recorded)
        if failed:
            body += "\nFailed: " + ", ".join(f"`{t}`" for t in failed)
        body += (
            "\n\n_For a custom date, use_ `!mission_record @user date=YYYY-MM-DD`."
        )
        await send_ephemeral(interaction, body[:1900])
        recorded_ids = [str(u.id) for u in users]
        await post_mission_audit(
            self.cog.bot,
            action="Mission Recorded (panel)",
            actor=interaction.user,
            fields=[
                ("Date", today.isoformat()),
                (
                    f"Players ({len(recorded_ids)})",
                    _fmt_attendee_list(recorded_ids),
                ),
                (
                    f"Failed ({len(failed)})",
                    ", ".join(f"`{t}`" for t in failed) if failed else "_(none)_",
                ),
            ],
            color=discord.Color.green(),
        )


def _short(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


class ActorPayAmountModal(discord.ui.Modal, title="Actor Pay"):
    amount = discord.ui.TextInput(
        label="Pay per actor (¥, bank)",
        placeholder="e.g. 2500",
        required=True,
        max_length=12,
    )

    def __init__(
        self,
        cog: "FixerHubCog",
        ctx,
        actor_ids: list[str],
        actor_names: dict[str, str],
        mission_id: str,
        mission_name: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.actor_ids = actor_ids
        self.actor_names = actor_names
        self.mission_id = mission_id
        self.mission_name = mission_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from NightCityBot.cogs.missions import _parse_int_amount

        await interaction.response.defer(ephemeral=True)
        pay = _parse_int_amount(str(self.amount.value or ""))
        if pay is None or pay <= 0:
            await send_ephemeral(interaction, "Pay must be a positive integer (e.g. `2500`).")
            return

        ub = getattr(self.cog.bot, "unbelievaboat", None)
        if ub is None:
            await send_ephemeral(interaction, "UnbelievaBoat client not configured — cannot pay.")
            return

        fixer_id = str(interaction.user.id)
        fixer_username = (
            getattr(interaction.user, "display_name", None)
            or getattr(interaction.user, "name", fixer_id)
        )
        paid: list[str] = []
        failed: list[str] = []
        reason = f"NCRP Actor Pay: {self.mission_name}"
        for uid in self.actor_ids:
            display = self.actor_names.get(uid, uid)
            try:
                ok = await ub.update_balance(
                    int(uid), {"cash": 0, "bank": pay}, reason=reason
                )
            except Exception:
                logger.error(
                    "Actor pay UB call failed for user %s mission %s",
                    uid, self.mission_id, exc_info=True,
                )
                ok = False
            if not ok:
                failed.append(f"• **{display}** — UB payout failed")
                continue
            rec = await actor_attendance_record(
                user_id=str(uid),
                username=str(display)[:128],
                mission_id=str(self.mission_id) if self.mission_id else None,
                mission_name=str(self.mission_name),
                fixer_id=fixer_id,
                fixer_username=str(fixer_username),
                pay_amount=int(pay),
            )
            if rec is None:
                failed.append(
                    f"• **{display}** — paid ¥{pay:,} but ledger write failed"
                )
            else:
                paid.append(f"• **{display}** — +¥{pay:,} bank")

        embed = discord.Embed(
            title=f"🎭 Actor Pay — {_short(self.mission_name, 60)}",
            color=discord.Color.gold(),
        )
        if paid:
            embed.add_field(
                name=f"Paid ({len(paid)})",
                value="\n".join(paid)[:1024],
                inline=False,
            )
        if failed:
            embed.add_field(
                name=f"Failed ({len(failed)})",
                value="\n".join(failed)[:1024],
                inline=False,
            )
        if not paid and not failed:
            embed.description = "No actors selected — nothing to pay."
        await send_ephemeral(interaction, embed=embed)
        await post_mission_audit(
            self.cog.bot,
            action="Actor Pay",
            actor=interaction.user,
            mission_id=str(self.mission_id) if self.mission_id else None,
            mission_name=self.mission_name,
            fields=[
                ("Pay per actor", f"¥{pay:,} → bank"),
                (
                    f"Paid ({len(paid)})",
                    "\n".join(paid) if paid else "_(none)_",
                ),
                (
                    f"Failed ({len(failed)})",
                    "\n".join(failed) if failed else "_(none)_",
                ),
            ],
            color=discord.Color.gold(),
        )


class ActorPayTypedMissionModal(discord.ui.Modal, title="Type Mission Name"):
    mission_name = discord.ui.TextInput(
        label="Mission name",
        placeholder="e.g. Heist at Konpeki Plaza",
        required=True,
        max_length=100,
    )

    def __init__(self, picker: "ActorPayPickerView"):
        super().__init__(timeout=300)
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.mission_name.value or "").strip()
        if not name:
            await respond_ephemeral(interaction, "Mission name can't be empty.")
            return
        self.picker.selected_mission_id = None
        self.picker.selected_mission_name = name
        await respond_ephemeral(
            interaction,
            f"✅ Mission set to **{_short(name, 80)}**. Tap **Continue →** to enter the pay amount.",
        )


class ActorPayPickerView(SafeView):
    """Two-step picker: select actors + mission, then Continue → modal for pay."""

    def __init__(self, cog: "FixerHubCog", ctx, recent_missions: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.selected_actor_ids: list[str] = []
        self.selected_actor_names: dict[str, str] = {}
        self.selected_mission_id: Optional[str] = None
        self.selected_mission_name: Optional[str] = None

        options: list[discord.SelectOption] = []
        for m in recent_missions[:25]:
            mid = str(m.get("mission_id") or "")
            mname = str(m.get("mission_name") or "Mission")
            start_ts = m.get("start_ts")
            try:
                desc = (
                    f"{start_ts.strftime('%Y-%m-%d')} • by "
                    f"{_short(m.get('creator_username') or '', 40) or 'unknown'}"
                )
            except Exception:
                desc = _short(m.get("creator_username") or "", 80)
            options.append(
                discord.SelectOption(
                    label=_short(mname, 100),
                    value=mid,
                    description=_short(desc, 100) or None,
                )
            )
        # Cache for label resolution when the user picks.
        self._mission_name_by_id = {
            str(m.get("mission_id") or ""): str(m.get("mission_name") or "Mission")
            for m in recent_missions
        }
        self.mission_select.options = options or [
            discord.SelectOption(label="(no missions)", value="", default=True)
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Actor(s) to pay…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def actor_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.selected_actor_ids = [str(u.id) for u in select.values]
        self.selected_actor_names = {
            str(u.id): (getattr(u, "display_name", None) or getattr(u, "name", str(u.id)))
            for u in select.values
        }
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="Mission they acted in…",
        min_values=1,
        max_values=1,
        row=1,
    )
    async def mission_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0] if select.values else ""
        self.selected_mission_id = val or None
        self.selected_mission_name = self._mission_name_by_id.get(val) or "Mission"
        await interaction.response.defer()

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_actor_ids:
            await respond_ephemeral(interaction, "Pick at least one actor first.")
            return
        if not self.selected_mission_id and not self.selected_mission_name:
            await respond_ephemeral(
                interaction,
                "Pick a mission from the dropdown — or tap **✏️ Type Mission Name** to enter one manually.",
            )
            return
        await interaction.response.send_modal(
            ActorPayAmountModal(
                self.cog, self.ctx,
                actor_ids=list(self.selected_actor_ids),
                actor_names=dict(self.selected_actor_names),
                mission_id=str(self.selected_mission_id) if self.selected_mission_id else "",
                mission_name=str(self.selected_mission_name or "Mission"),
            )
        )

    @discord.ui.button(label="Type Mission Name", style=discord.ButtonStyle.secondary, emoji="✏️", row=2)
    async def type_mission(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActorPayTypedMissionModal(self))


class CheckActorPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Pick user(s) to check…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        users = list(select.values)
        if not users:
            await send_ephemeral(interaction, "No users selected.")
            return
        sections: list[str] = []
        for u in users:
            uid = str(u.id)
            display = getattr(u, "display_name", None) or getattr(u, "name", uid)
            rows = await actor_attendance_get_by_user(uid, limit=50)
            if not rows:
                sections.append(f"**{display}** — no actor record.")
                continue
            count = len(rows)
            dates = sorted({r["acted_at"].date().isoformat() for r in rows if r.get("acted_at")})
            mission_lines = []
            for r in rows[:15]:
                d = r.get("acted_at")
                d_str = d.date().isoformat() if hasattr(d, "date") else str(d or "")
                mname = _short(r.get("mission_name") or "Mission", 60)
                fname = _short(r.get("fixer_username") or "", 30) or "unknown"
                pay = int(r.get("pay_amount") or 0)
                mission_lines.append(f"  • {d_str} — *{mname}* (fixer: {fname}, ¥{pay:,})")
            more = "" if count <= 15 else f"\n  …and {count - 15} more."
            sections.append(
                f"**{display}** — acted **{count}** time(s)\n"
                f"  Dates: {', '.join(dates[:10])}{' …' if len(dates) > 10 else ''}\n"
                f"  Recent acts:\n" + "\n".join(mission_lines) + more
            )
        embed = discord.Embed(
            title="🎭 Actor History",
            description="\n\n".join(sections)[:4096],
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{len(users)} user(s) checked")
        await send_ephemeral(interaction, embed=embed)


MISSION_EVENT_IMAGE_CANDIDATES = [
    Path("attached_assets/NCRP_SquareBanner2_1779474926073.png"),
    Path("attached_assets/NCRP_SquareBanner_1779474930891.png"),
    Path("attached_assets/NCRP_GroupBanner_1779474930892.png"),
]
# Legacy single-banner path. If present it's added to the rotation so
# operators who already dropped a `mission_banner.png` keep working.
_LEGACY_BANNER = Path("attached_assets/mission_banner.png")


def _pick_mission_banner_bytes() -> Optional[bytes]:
    """Return the bytes of a random available mission banner (≤8 MiB), or None."""
    candidates = list(MISSION_EVENT_IMAGE_CANDIDATES)
    if _LEGACY_BANNER.is_file():
        candidates.append(_LEGACY_BANNER)
    available = [p for p in candidates if p.is_file()]
    if not available:
        return None
    random.shuffle(available)
    for path in available:
        try:
            raw = path.read_bytes()
        except Exception:
            continue
        if len(raw) <= 8 * 1024 * 1024:
            return raw
        logger.warning(
            "Mission banner %s is %d bytes (>8MiB), trying next.",
            path, len(raw),
        )
    return None


MISSION_DATETIME_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})\s*$")


def _parse_mission_start(text: str) -> Optional[datetime]:
    """Parse `YYYY-MM-DD HH:MM` as UTC. Returns None on failure."""
    if not text:
        return None
    m = MISSION_DATETIME_RE.match(text.strip())
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), 0,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _parse_int_amount(text: str) -> Optional[int]:
    if text is None:
        return None
    cleaned = "".join(c for c in str(text).strip() if c.isdigit() or c == "-")
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


# ─── Create Mission: timezone / date / time helpers ───────────────────────────

MISSION_TZ_OPTIONS: list[tuple[str, str]] = [
    ("US Eastern", "America/New_York"),
    ("US Central", "America/Chicago"),
    ("US Mountain", "America/Denver"),
    ("US Pacific", "America/Los_Angeles"),
    ("UTC", "UTC"),
]
MISSION_TZ_LABELS: dict[str, str] = {tz: label for label, tz in MISSION_TZ_OPTIONS}
MISSION_DEFAULT_TZ: str = "America/New_York"

MISSION_DURATION_OPTIONS: list[tuple[str, float]] = [
    ("30 minutes", 0.5),
    ("1 hour", 1.0),
    ("1.5 hours", 1.5),
    ("2 hours", 2.0),
    ("3 hours", 3.0),
    ("4 hours", 4.0),
    ("6 hours", 6.0),
    ("8 hours", 8.0),
    ("12 hours", 12.0),
    ("24 hours", 24.0),
]


def _fixer_tz_key(user_id: int | str) -> str:
    return f"fixer_tz:{user_id}"


async def get_fixer_tz_pref(user_id: int | str) -> str:
    """Return the saved IANA timezone for *user_id*, defaulting to ET."""
    raw = await bot_config_get(_fixer_tz_key(user_id), MISSION_DEFAULT_TZ)
    tz_name = (raw or MISSION_DEFAULT_TZ).strip()
    # Validate it's one of our supported zones (covers stale data / typos).
    if tz_name not in MISSION_TZ_LABELS:
        return MISSION_DEFAULT_TZ
    return tz_name


async def set_fixer_tz_pref(user_id: int | str, tz_name: str) -> bool:
    """Persist a fixer's preferred timezone for next time."""
    if tz_name not in MISSION_TZ_LABELS:
        return False
    return await bot_config_set(
        _fixer_tz_key(user_id), tz_name,
        description="Per-fixer preferred timezone for Create Mission",
    )


def _format_time_option_label(hour: int) -> str:
    """Render a 24h hour as a 12-hour AM/PM label, e.g. 20 → '8:00 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:00 {suffix}"


def _build_date_options(tz: ZoneInfo, count: int = 14) -> list[discord.SelectOption]:
    """Build the next ``count`` date options starting from today in *tz*."""
    today = datetime.now(tz).date()
    opts: list[discord.SelectOption] = []
    for i in range(count):
        d = today + timedelta(days=i)
        if i == 0:
            label = f"Today — {d.strftime('%a, %b %d')}"
        elif i == 1:
            label = f"Tomorrow — {d.strftime('%a, %b %d')}"
        else:
            label = d.strftime("%a, %b %d")
        opts.append(discord.SelectOption(label=label, value=d.isoformat()))
    return opts


def _build_time_options() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=_format_time_option_label(h), value=str(h))
        for h in range(24)
    ]


def _build_duration_options() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=label, value=str(hours))
        for label, hours in MISSION_DURATION_OPTIONS
    ]


def _build_tz_options(default_tz: str) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=label, value=tz, default=(tz == default_tz),
        )
        for label, tz in MISSION_TZ_OPTIONS
    ]


class CreateMissionScheduleView(SafeView):
    """Step-2 view after the create-mission modal: dropdowns for date / time /
    duration / timezone, then Continue advances to the attendees view.
    """

    def __init__(
        self,
        *,
        cog: "FixerHubCog",
        ctx,
        mission_name: str,
        pay_per_player: int,
        location: str,
        default_tz: str,
        origin_channel_id: Optional[int],
        mission_description: str = "",
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx
        self.mission_name = mission_name
        self.pay_per_player = pay_per_player
        self.location = location
        self.mission_description = (mission_description or "").strip()
        self.origin_channel_id = origin_channel_id

        self.tz_name: str = default_tz if default_tz in MISSION_TZ_LABELS else MISSION_DEFAULT_TZ
        self.selected_date: Optional[str] = None  # ISO YYYY-MM-DD (wall-clock in tz_name)
        self.selected_hour: Optional[int] = None  # 0–23 (wall-clock in tz_name)
        self.selected_duration_h: float = 4.0      # default to 4h to match prior modal default

        # Build selects with sensible defaults populated.
        self._date_select = discord.ui.Select(
            placeholder="Date…",
            options=_build_date_options(ZoneInfo(self.tz_name)),
            min_values=1, max_values=1, row=0,
        )
        self._date_select.callback = self._on_date  # type: ignore[assignment]

        self._time_select = discord.ui.Select(
            placeholder="Start time (hour)…",
            options=_build_time_options(),
            min_values=1, max_values=1, row=1,
        )
        self._time_select.callback = self._on_time  # type: ignore[assignment]

        # Mark the default duration (4h) as default-selected.
        dur_opts = _build_duration_options()
        for opt in dur_opts:
            if opt.value == "4.0":
                opt.default = True
        self._duration_select = discord.ui.Select(
            placeholder="Duration…",
            options=dur_opts,
            min_values=1, max_values=1, row=2,
        )
        self._duration_select.callback = self._on_duration  # type: ignore[assignment]

        self._tz_select = discord.ui.Select(
            placeholder="Timezone…",
            options=_build_tz_options(self.tz_name),
            min_values=1, max_values=1, row=3,
        )
        self._tz_select.callback = self._on_tz  # type: ignore[assignment]

        self.add_item(self._date_select)
        self.add_item(self._time_select)
        self.add_item(self._duration_select)
        self.add_item(self._tz_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_date(self, interaction: discord.Interaction):
        self.selected_date = self._date_select.values[0]
        # Persist the user's pick as the default for re-renders.
        for opt in self._date_select.options:
            opt.default = (opt.value == self.selected_date)
        await interaction.response.defer()

    async def _on_time(self, interaction: discord.Interaction):
        try:
            self.selected_hour = int(self._time_select.values[0])
        except (ValueError, TypeError):
            self.selected_hour = None
        for opt in self._time_select.options:
            opt.default = (opt.value == str(self.selected_hour))
        await interaction.response.defer()

    async def _on_duration(self, interaction: discord.Interaction):
        try:
            self.selected_duration_h = float(self._duration_select.values[0])
        except (ValueError, TypeError):
            self.selected_duration_h = 4.0
        for opt in self._duration_select.options:
            opt.default = (opt.value == str(self.selected_duration_h))
        await interaction.response.defer()

    async def _on_tz(self, interaction: discord.Interaction):
        new_tz = self._tz_select.values[0]
        if new_tz in MISSION_TZ_LABELS:
            self.tz_name = new_tz
        # Reflect the new default + rebuild date options so "Today" reflects
        # the chosen timezone's wall-clock.
        for opt in self._tz_select.options:
            opt.default = (opt.value == self.tz_name)
        self._date_select.options = _build_date_options(ZoneInfo(self.tz_name))
        # Preserve a previously-picked date if it's still in range.
        if self.selected_date:
            still_present = any(o.value == self.selected_date for o in self._date_select.options)
            if still_present:
                for opt in self._date_select.options:
                    opt.default = (opt.value == self.selected_date)
            else:
                self.selected_date = None
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, emoji="➡️", row=4)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_date or self.selected_hour is None:
            await respond_ephemeral(interaction, "Pick a date and a start hour first.")
            return
        if not (0.25 <= self.selected_duration_h <= 24):
            await respond_ephemeral(interaction, "Pick a duration.")
            return
        try:
            tz = ZoneInfo(self.tz_name)
        except Exception:
            await respond_ephemeral(interaction, "Invalid timezone — please pick one from the dropdown.")
            return
        try:
            y, m, d = (int(x) for x in self.selected_date.split("-"))
            local_dt = datetime(y, m, d, int(self.selected_hour), 0, 0, tzinfo=tz)
        except Exception:
            await respond_ephemeral(interaction, "Couldn't build the start datetime — please re-pick.")
            return
        start_utc = local_dt.astimezone(timezone.utc)
        if start_utc < datetime.now(timezone.utc) - timedelta(minutes=5):
            await respond_ephemeral(
                interaction,
                f"That start time ({local_dt.strftime('%a %b %d %I:%M %p')} "
                f"{MISSION_TZ_LABELS.get(self.tz_name, self.tz_name)}) is in the past. "
                "Pick a future time.",
            )
            return
        # Persist the fixer's tz pref so it auto-fills next time.
        await set_fixer_tz_pref(interaction.user.id, self.tz_name)

        end_utc = start_utc + timedelta(hours=self.selected_duration_h)
        attendees_view = CreateMissionAttendeesView(
            cog=self.cog,
            ctx=self.ctx,
            mission_name=self.mission_name,
            start_utc=start_utc,
            end_utc=end_utc,
            pay_per_player=self.pay_per_player,
            location=self.location,
            mission_description=self.mission_description,
            origin_channel_id=self.origin_channel_id,
        )
        payout_utc = compute_payout_ts(start_utc)
        tz_label = MISSION_TZ_LABELS.get(self.tz_name, self.tz_name)
        desc_preview = ""
        if self.mission_description:
            snippet = self.mission_description[:300]
            if len(self.mission_description) > 300:
                snippet += "…"
            desc_preview = f"**Description:** {snippet}\n"
        embed = discord.Embed(
            title=f"🆕 New Mission — {self.mission_name}",
            description=(
                f"**Location:** {self.location}\n"
                f"{desc_preview}"
                f"**Start ({tz_label}):** {local_dt.strftime('%a %b %d, %Y · %I:%M %p')}\n"
                f"**Start (your local time):** <t:{int(start_utc.timestamp())}:F> "
                f"(<t:{int(start_utc.timestamp())}:R>)\n"
                f"**End:** <t:{int(end_utc.timestamp())}:F>\n"
                f"**Pay each:** ¥{self.pay_per_player:,} → bank\n"
                f"**Auto-payout:** <t:{int(payout_utc.timestamp())}:F> "
                "(midnight US Eastern after start)\n\n"
                "Select up to 25 attendees below, then press **Confirm & Create**."
                "\n_Note: you (the Fixer) will be excluded from payout automatically._"
            ),
            color=discord.Color.gold(),
        )
        try:
            await interaction.response.edit_message(embed=embed, view=attendees_view)
        except Exception:
            await respond_ephemeral(interaction, embed=embed, view=attendees_view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️", row=4)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(
                content="Cancelled.", embed=None, view=None,
            )
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass


class EditMissionPickerView(SafeView):
    """Step 1 of Edit Mission: pick which recorded mission to operate on."""

    def __init__(self, cog: "FixerHubCog", ctx, recent_missions: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.selected_mission_id: Optional[str] = None

        options: list[discord.SelectOption] = []
        for m in recent_missions[:25]:
            mid = str(m.get("mission_id") or "")
            mname = str(m.get("mission_name") or "Mission")
            start_ts = m.get("start_ts")
            try:
                desc = (
                    f"{start_ts.strftime('%Y-%m-%d')} • by "
                    f"{_short(m.get('creator_username') or '', 40) or 'unknown'}"
                )
            except Exception:
                desc = _short(m.get("creator_username") or "", 80)
            options.append(
                discord.SelectOption(
                    label=_short(mname, 100),
                    value=mid,
                    description=_short(desc, 100) or None,
                )
            )
        self.mission_select.options = options or [
            discord.SelectOption(label="(no missions)", value="", default=True)
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        placeholder="Mission to edit…",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def mission_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_mission_id = (select.values[0] if select.values else "") or None
        await interaction.response.defer()

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.selected_mission_id:
            await send_ephemeral(interaction, "Pick a mission first.")
            return
        row = await mission_event_get(self.selected_mission_id)
        if row is None:
            await send_ephemeral(interaction, "That mission no longer exists.")
            return
        view = EditMissionPanelView(self.cog, self.ctx, row)
        await send_ephemeral(
            interaction,
            embed=_edit_mission_summary_embed(row),
            view=view,
        )


def _edit_mission_summary_embed(row: dict) -> discord.Embed:
    name = str(row.get("mission_name") or "Mission")
    start_ts = row.get("start_ts")
    end_ts = row.get("end_ts")
    payout_ts = row.get("payout_ts")
    pay = int(row.get("pay_per_player") or 0)
    attendees = list(row.get("attendee_ids") or [])
    creator = row.get("creator_username") or row.get("creator_id") or "unknown"
    location = row.get("location") or "?"
    color = discord.Color.dark_grey() if row.get("canceled") else (
        discord.Color.green() if row.get("paid") else discord.Color.blurple()
    )
    embed = discord.Embed(title=f"✏️ Edit Mission — {_short(name, 80)}", color=color)

    def _fmt(ts):
        if not isinstance(ts, datetime):
            return "—"
        return f"<t:{int(ts.timestamp())}:F>"

    status_bits = []
    if row.get("canceled"):
        status_bits.append("🗑️ canceled")
    if row.get("paid"):
        status_bits.append("✅ paid")
    if not status_bits:
        status_bits.append("⏳ pending")

    embed.add_field(name="Status", value=" • ".join(status_bits), inline=False)
    embed.add_field(name="Fixer", value=str(creator), inline=True)
    embed.add_field(name="Location", value=str(location)[:64], inline=True)
    embed.add_field(name="Pay / attendee", value=f"¥{pay:,}", inline=True)
    embed.add_field(name="Start", value=_fmt(start_ts), inline=True)
    embed.add_field(name="End", value=_fmt(end_ts), inline=True)
    embed.add_field(name="Payout", value=_fmt(payout_ts), inline=True)
    embed.add_field(
        name=f"Attendees ({len(attendees)})",
        value=(", ".join(f"<@{a}>" for a in attendees[:25]) or "—")[:1024],
        inline=False,
    )
    embed.set_footer(text=f"mission_id: {row.get('mission_id')}")
    return embed


class EditMissionPanelView(SafeView):
    """Step 2 of Edit Mission: per-field edit buttons."""

    def __init__(self, cog: "FixerHubCog", ctx, row: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx
        self.row = dict(row)
        self._apply_locked_state()

    def _apply_locked_state(self) -> None:
        """Disable mutating actions when the mission is paid or canceled.

        Paid missions are locked because we can't undo a payout, and
        canceled missions are locked because nothing the buttons do still
        makes sense. Refresh and Close stay enabled in both cases.
        """
        locked = bool(self.row.get("paid") or self.row.get("canceled"))
        if not locked:
            return
        allowed = {"Close", "Refresh"}
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label not in allowed:
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        fresh = await mission_event_get(str(self.row.get("mission_id")))
        if fresh is None:
            return
        self.row = fresh
        # Re-evaluate locked state in case the mission was just paid/canceled.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        self._apply_locked_state()
        try:
            if interaction.message is not None:
                await interaction.message.edit(
                    embed=_edit_mission_summary_embed(self.row), view=self
                )
        except Exception:
            pass

    @discord.ui.button(label="Edit Date/Time", style=discord.ButtonStyle.primary, emoji="📅", row=0)
    async def edit_datetime(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditMissionDateTimeModal(self))

    @discord.ui.button(label="Edit Attendees", style=discord.ButtonStyle.primary, emoji="👥", row=0)
    async def edit_attendees(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = EditMissionAttendeesView(self)
        await send_ephemeral(
            interaction,
            f"Select the new attendee list for **{_short(self.row.get('mission_name') or '', 60)}** "
            "(replaces the existing list). Up to 25.",
            view=view,
        )

    @discord.ui.button(label="Edit Payout", style=discord.ButtonStyle.primary, emoji="💰", row=0)
    async def edit_payout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditMissionPayoutModal(self))

    @discord.ui.button(label="Cancel Mission", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def cancel_mission(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ConfirmCancelMissionView(self)
        await send_ephemeral(
            interaction,
            f"⚠️ Cancel **{self.row.get('mission_name')}**? This deletes the Discord event "
            "and marks the mission as canceled. No payout will be issued.",
            view=view,
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._refresh_message(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except Exception:
            pass


async def _edit_discord_event(
    bot, row: dict, **kwargs
) -> tuple[bool, Optional[str]]:
    """Best-effort `event.edit(**kwargs)` for the mission's Discord event.

    Returns (ok, error_message). If the event is missing, returns (False, msg)
    so callers can decide whether to still apply DB updates.
    """
    event_id = row.get("event_id")
    guild_id = row.get("guild_id")
    if not event_id or not guild_id:
        return False, "No linked Discord event."
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        try:
            guild = await bot.fetch_guild(int(guild_id))
        except Exception:
            return False, "Couldn't reach the guild on Discord."
    try:
        event = await guild.fetch_scheduled_event(int(event_id))
    except discord.NotFound:
        return False, "The Discord event no longer exists."
    except Exception as e:
        logger.warning("fetch_scheduled_event failed for %s", event_id, exc_info=True)
        return False, f"Discord fetch failed: `{e}`"
    try:
        await event.edit(**kwargs)
        return True, None
    except Exception as e:
        logger.warning("event.edit failed for %s", event_id, exc_info=True)
        return False, f"Discord update failed: `{e}`"


class EditMissionDateTimeModal(discord.ui.Modal, title="Edit Mission Date/Time"):
    start_time = discord.ui.TextInput(
        label="New start (UTC) — YYYY-MM-DD HH:MM",
        placeholder="2026-05-23 20:00",
        max_length=20,
        required=True,
    )
    duration_hours = discord.ui.TextInput(
        label="Duration (hours)",
        placeholder="4",
        max_length=6,
        required=True,
    )

    def __init__(self, panel: "EditMissionPanelView"):
        super().__init__(timeout=300)
        self.panel = panel
        # Pre-fill with current values for convenience.
        cur_start = panel.row.get("start_ts")
        cur_end = panel.row.get("end_ts")
        if isinstance(cur_start, datetime):
            self.start_time.default = cur_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        if isinstance(cur_start, datetime) and isinstance(cur_end, datetime):
            hours = max(1, round((cur_end - cur_start).total_seconds() / 3600))
            self.duration_hours.default = str(hours)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        new_start = _parse_mission_start(str(self.start_time.value or ""))
        if new_start is None:
            await send_ephemeral(
                interaction,
                "Start must be `YYYY-MM-DD HH:MM` in UTC (e.g. `2026-05-23 20:00`).",
            )
            return
        try:
            hours = float(str(self.duration_hours.value or "").strip())
        except ValueError:
            hours = -1.0
        if not (0.25 <= hours <= 24):
            await send_ephemeral(interaction, "Duration must be between 0.25 and 24 hours.")
            return
        new_end = new_start + timedelta(hours=hours)
        new_payout = compute_payout_ts(new_start)

        ok, err = await _edit_discord_event(
            self.panel.cog.bot, self.panel.row,
            start_time=new_start, end_time=new_end,
        )
        if not ok:
            await send_ephemeral(
                interaction,
                f"❌ Couldn't update the Discord event — leaving the DB row untouched.\n{err}",
            )
            return
        db_ok = await mission_event_update(
            str(self.panel.row.get("mission_id")),
            start_ts=new_start, end_ts=new_end, payout_ts=new_payout,
        )
        if not db_ok:
            await send_ephemeral(
                interaction,
                "⚠️ Discord was updated but the DB write failed. Try again or contact the dev.",
            )
            return
        await send_ephemeral(
            interaction,
            f"✅ Mission rescheduled to <t:{int(new_start.timestamp())}:F>. "
            f"Auto-payout now lands at <t:{int(new_payout.timestamp())}:F>.",
        )
        old_start = self.panel.row.get("start_ts")
        old_end = self.panel.row.get("end_ts")
        old_payout = self.panel.row.get("payout_ts")
        def _fmt_dt(dt):
            if isinstance(dt, datetime):
                d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                return f"<t:{int(d.timestamp())}:F>"
            return "_(unknown)_"
        await post_mission_audit(
            self.panel.cog.bot,
            action="Mission Edited — Date/Time",
            actor=interaction.user,
            mission_id=str(self.panel.row.get("mission_id") or ""),
            mission_name=str(self.panel.row.get("mission_name") or ""),
            before=(
                f"Start: {_fmt_dt(old_start)}\n"
                f"End: {_fmt_dt(old_end)}\n"
                f"Payout: {_fmt_dt(old_payout)}"
            ),
            after=(
                f"Start: {_fmt_dt(new_start)}\n"
                f"End: {_fmt_dt(new_end)}\n"
                f"Payout: {_fmt_dt(new_payout)}"
            ),
            color=discord.Color.blue(),
        )
        await self.panel._refresh_message(interaction)


class EditMissionPayoutModal(discord.ui.Modal, title="Edit Mission Payout"):
    amount = discord.ui.TextInput(
        label="New pay per attendee (¥, bank)",
        placeholder="e.g. 2500",
        required=True,
        max_length=12,
    )

    def __init__(self, panel: "EditMissionPanelView"):
        super().__init__(timeout=300)
        self.panel = panel
        cur = int(panel.row.get("pay_per_player") or 0)
        if cur:
            self.amount.default = str(cur)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        pay = _parse_int_amount(str(self.amount.value or ""))
        if pay is None or pay < 0:
            await send_ephemeral(interaction, "Pay must be a non-negative integer (e.g. `2500`).")
            return
        ok = await mission_event_update(
            str(self.panel.row.get("mission_id")),
            pay_per_player=pay,
        )
        if not ok:
            await send_ephemeral(interaction, "DB write failed. Try again.")
            return
        # Push the new pay into the event description so attendees see it.
        row = await mission_event_get(str(self.panel.row.get("mission_id"))) or self.panel.row
        new_desc = _build_mission_description(row)
        push_ok, push_err = await _edit_discord_event(
            self.panel.cog.bot, row, description=new_desc[:1000]
        )
        msg = f"✅ Updated pay to ¥{pay:,} per attendee."
        if not push_ok:
            msg += f"\n⚠️ DB updated but Discord push failed: {push_err}"
        await send_ephemeral(interaction, msg)
        await post_mission_audit(
            self.panel.cog.bot,
            action="Mission Edited — Pay",
            actor=interaction.user,
            mission_id=str(self.panel.row.get("mission_id") or ""),
            mission_name=str(self.panel.row.get("mission_name") or ""),
            before=f"¥{int(self.panel.row.get('pay_per_player') or 0):,} per attendee",
            after=f"¥{pay:,} per attendee",
            color=discord.Color.blue(),
        )
        await self.panel._refresh_message(interaction)


def _build_mission_description(row: dict) -> str:
    pay = int(row.get("pay_per_player") or 0)
    attendees = list(row.get("attendee_ids") or [])
    creator_id = row.get("creator_id") or ""
    location = row.get("location") or "?"
    user_desc = (row.get("mission_description") or "").strip()
    parts: list[str] = []
    if user_desc:
        # Fixer's own description first so it leads the event card.
        parts.append(user_desc)
        parts.append("")  # blank line separator
    parts.extend([
        f"📍 **Location:** {location}",
        f"💰 **Pay:** ¥{pay:,} per attendee (auto-paid the morning after).",
        f"🎬 **Fixer:** <@{creator_id}>",
        f"🎯 **Attendees:** {len(attendees)}",
    ])
    return "\n".join(parts)


class EditMissionAttendeesView(SafeView):
    def __init__(self, panel: "EditMissionPanelView"):
        super().__init__(timeout=300)
        self.panel = panel
        self.selected: list[discord.abc.User] = []
        # Defaults: match the creation flow (which always credits new
        # attendees) and the historical cancel behavior (which never
        # reverses credits for removed ones).
        self.credit_adds: bool = True
        self.reverse_removed: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.panel.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    def _toggle_button_styles(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label and child.label.startswith("Credit Added"):
                    child.style = (
                        discord.ButtonStyle.success if self.credit_adds
                        else discord.ButtonStyle.secondary
                    )
                elif child.label and child.label.startswith("Reverse Removed"):
                    child.style = (
                        discord.ButtonStyle.success if self.reverse_removed
                        else discord.ButtonStyle.secondary
                    )

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="New attendee list (replaces existing)…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def attendee_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.selected = list(select.values)
        names = ", ".join(getattr(u, "display_name", None) or u.name for u in self.selected)
        await respond_ephemeral(
            interaction,
            f"Selected {len(self.selected)} attendee(s): {names[:1500]}\n"
            "Press **Save** to apply.",
        )

    @discord.ui.button(
        label="Credit Added: ON",
        style=discord.ButtonStyle.success,
        emoji="📋",
        row=1,
    )
    async def toggle_credit_adds(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.credit_adds = not self.credit_adds
        button.label = f"Credit Added: {'ON' if self.credit_adds else 'OFF'}"
        self._toggle_button_styles()
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await interaction.response.defer()

    @discord.ui.button(
        label="Reverse Removed: OFF",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        row=1,
    )
    async def toggle_reverse_removed(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.reverse_removed = not self.reverse_removed
        button.label = f"Reverse Removed: {'ON' if self.reverse_removed else 'OFF'}"
        self._toggle_button_styles()
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await interaction.response.defer()

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.selected:
            await send_ephemeral(interaction, "Pick at least one attendee first.")
            return
        creator_id = str(self.panel.row.get("creator_id") or "")
        new_ids = [str(u.id) for u in self.selected if str(u.id) != creator_id]
        if not new_ids:
            await send_ephemeral(
                interaction,
                "All selected attendees were filtered (the fixer can't be paid). "
                "Pick at least one other player.",
            )
            return

        old_ids = [str(x) for x in (self.panel.row.get("attendee_ids") or [])]
        old_set = set(old_ids)
        new_set = set(new_ids)
        added = [u for u in self.selected if str(u.id) in (new_set - old_set)]
        removed_ids = [uid for uid in old_ids if uid not in new_set]

        ok = await mission_event_update(
            str(self.panel.row.get("mission_id")),
            attendee_ids=new_ids,
        )
        if not ok:
            await send_ephemeral(interaction, "DB write failed. Try again.")
            return
        row = await mission_event_get(str(self.panel.row.get("mission_id"))) or self.panel.row
        new_desc = _build_mission_description(row)
        push_ok, push_err = await _edit_discord_event(
            self.panel.cog.bot, row, description=new_desc[:1000]
        )

        # Gig-log adjustments based on the toggles.
        start_ts = row.get("start_ts")
        mission_date = start_ts.date() if isinstance(start_ts, datetime) else None
        credited = 0
        reversed_ = 0
        log_warnings: list[str] = []
        mission_title = str(row.get("mission_name") or "")
        if mission_date is not None:
            if self.credit_adds and added:
                for u in added:
                    display = getattr(u, "display_name", None) or getattr(u, "name", str(u.id))
                    try:
                        res = await mission_log_record(
                            str(u.id), str(display)[:128], mission_date, mission_title
                        )
                        if res is not None:
                            credited += 1
                        else:
                            log_warnings.append(f"credit failed for <@{u.id}>")
                    except Exception:
                        logger.error(
                            "mission_log_record failed for user %s on Edit Attendees",
                            u.id, exc_info=True,
                        )
                        log_warnings.append(f"credit failed for <@{u.id}>")
            if self.reverse_removed and removed_ids:
                for uid in removed_ids:
                    try:
                        # Match this mission's title first so we don't yank a
                        # credit belonging to a different same-day mission.
                        res = await mission_log_remove_date(
                            str(uid), mission_date, mission_title
                        )
                        if res is not None:
                            reversed_ += 1
                        # If no entry existed for that date, silently skip —
                        # the player may have never been credited in the
                        # first place (legacy mission).
                    except Exception:
                        logger.error(
                            "mission_log_remove_date failed for user %s on Edit Attendees",
                            uid, exc_info=True,
                        )
                        log_warnings.append(f"reverse failed for <@{uid}>")

        msg = f"✅ Attendee list updated ({len(new_ids)} player(s))."
        if added or removed_ids:
            details = []
            if added:
                detail = f"added {len(added)}"
                if self.credit_adds:
                    detail += f" (credited {credited})"
                else:
                    detail += " (not credited)"
                details.append(detail)
            if removed_ids:
                detail = f"removed {len(removed_ids)}"
                if self.reverse_removed:
                    detail += f" (reversed {reversed_})"
                else:
                    detail += " (credits kept)"
                details.append(detail)
            msg += f"\n• " + " • ".join(details)
        if log_warnings:
            msg += "\n⚠️ Some gig-log updates failed: " + ", ".join(log_warnings[:5])
            if len(log_warnings) > 5:
                msg += f", +{len(log_warnings) - 5} more"
        added_ids = [str(u.id) for u in added]
        await post_mission_audit(
            self.panel.cog.bot,
            action="Mission Edited — Attendees",
            actor=interaction.user,
            mission_id=str(self.panel.row.get("mission_id") or ""),
            mission_name=str(self.panel.row.get("mission_name") or ""),
            fields=[
                (
                    f"Added ({len(added_ids)})",
                    (_fmt_attendee_list(added_ids) if added_ids else "_(none)_")
                    + (
                        f" — credited {credited}" if (self.credit_adds and added_ids)
                        else (" — not credited" if added_ids else "")
                    ),
                ),
                (
                    f"Removed ({len(removed_ids)})",
                    (_fmt_attendee_list(removed_ids) if removed_ids else "_(none)_")
                    + (
                        f" — reversed {reversed_}" if (self.reverse_removed and removed_ids)
                        else (" — credits kept" if removed_ids else "")
                    ),
                ),
            ],
            before=_fmt_attendee_list(old_ids),
            after=_fmt_attendee_list(new_ids),
            color=discord.Color.blue(),
        )
        if not push_ok:
            msg += f"\n⚠️ DB updated but Discord push failed: {push_err}"
        await send_ephemeral(interaction, msg)
        await self.panel._refresh_message(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except Exception:
            pass


class ConfirmCancelMissionView(SafeView):
    def __init__(self, panel: "EditMissionPanelView"):
        super().__init__(timeout=120)
        self.panel = panel
        # Default ON: a canceled mission shouldn't keep counting toward
        # attendees' gig logs. Fixer can toggle OFF if they want to keep
        # the credits (rare — e.g. canceling for an OOC reason after the
        # session actually played).
        self.reverse_credits: bool = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.panel.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(
        label="Reverse Gig-Log Credits: ON",
        style=discord.ButtonStyle.success,
        emoji="↩️",
        row=0,
    )
    async def toggle_reverse(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.reverse_credits = not self.reverse_credits
        button.label = (
            f"Reverse Gig-Log Credits: {'ON' if self.reverse_credits else 'OFF'}"
        )
        button.style = (
            discord.ButtonStyle.success if self.reverse_credits
            else discord.ButtonStyle.secondary
        )
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await interaction.response.defer()

    @discord.ui.button(label="Yes, cancel it", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        # Try to delete the Discord event (best-effort).
        bot = self.panel.cog.bot
        row = self.panel.row
        event_id = row.get("event_id")
        guild_id = row.get("guild_id")
        discord_msg = "Discord event removed."
        if event_id and guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild is None:
                try:
                    guild = await bot.fetch_guild(int(guild_id))
                except Exception:
                    guild = None
            if guild is not None:
                try:
                    event = await guild.fetch_scheduled_event(int(event_id))
                    await event.delete()
                except discord.NotFound:
                    discord_msg = "Discord event was already gone."
                except Exception as e:
                    discord_msg = f"Couldn't delete Discord event: `{e}`"
        ok = await mission_event_cancel(str(row.get("mission_id")))
        if not ok:
            await send_ephemeral(
                interaction,
                f"⚠️ {discord_msg} But the DB cancel failed — try again.",
            )
            return

        # Optional gig-log credit reversal for every attendee on the mission.
        reverse_msg = (
            "Gig-log credits kept (toggle **Reverse Gig-Log Credits** to undo)."
        )
        if self.reverse_credits:
            start_ts = row.get("start_ts")
            mission_date = start_ts.date() if isinstance(start_ts, datetime) else None
            mission_title = str(row.get("mission_name") or "")
            attendees = list(row.get("attendee_ids") or [])
            reversed_ = 0
            failures: list[str] = []
            if mission_date is not None:
                for uid in attendees:
                    try:
                        # Match this mission's title so a player with two
                        # missions on the same date only loses the right one.
                        res = await mission_log_remove_date(
                            str(uid), mission_date, mission_title
                        )
                        if res is not None:
                            reversed_ += 1
                    except Exception:
                        logger.error(
                            "mission_log_remove_date failed for user %s on Cancel",
                            uid, exc_info=True,
                        )
                        failures.append(f"<@{uid}>")
            reverse_msg = (
                f"Gig-log credits reversed for {reversed_} / {len(attendees)} attendee(s)."
            )
            if failures:
                reverse_msg += f" Failures: {', '.join(failures[:5])}"
                if len(failures) > 5:
                    reverse_msg += f", +{len(failures) - 5} more"

        await send_ephemeral(
            interaction,
            f"🗑️ Mission canceled. {discord_msg} No auto-payout will be issued.\n"
            f"{reverse_msg}",
        )
        await post_mission_audit(
            self.panel.cog.bot,
            action="Mission Canceled",
            actor=interaction.user,
            mission_id=str(row.get("mission_id") or ""),
            mission_name=str(row.get("mission_name") or ""),
            fields=[
                ("Discord event", discord_msg),
                ("Gig-log credits", reverse_msg),
                (
                    "Attendees at cancel",
                    _fmt_attendee_list([str(x) for x in (row.get("attendee_ids") or [])]),
                ),
            ],
            color=discord.Color.red(),
        )
        await self.panel._refresh_message(interaction)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary, row=1)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except Exception:
            pass


class CreateMissionModal(discord.ui.Modal, title="Create Mission"):
    mission_name = discord.ui.TextInput(
        label="Mission Name",
        placeholder="e.g. Smash & Grab at Watson Med",
        max_length=120,
        required=True,
    )
    pay_per_player = discord.ui.TextInput(
        label="Pay per player (eddies, to bank)",
        placeholder="5000",
        max_length=10,
        required=True,
    )
    location = discord.ui.TextInput(
        label="Location",
        placeholder="Watson — Kabuki",
        max_length=200,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description (optional)",
        placeholder="Brief brief: hook, vibe, gear notes, OOC details… Shown on the Discord event card.",
        style=discord.TextStyle.paragraph,
        max_length=800,
        required=False,
    )

    def __init__(self, cog: "FixerHubCog", ctx, default_tz: str = MISSION_DEFAULT_TZ):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.default_tz = default_tz

    async def on_submit(self, interaction: discord.Interaction):
        pay = _parse_int_amount(str(self.pay_per_player.value))
        if pay is None or pay < 0:
            await respond_ephemeral(
                interaction,
                f"Couldn't parse pay `{self.pay_per_player.value}`. "
                "Use a non-negative integer (e.g. `5000`).",
            )
            return
        location_text = str(self.location.value).strip()
        if not location_text:
            await respond_ephemeral(interaction, "Location is required.")
            return
        name_text = str(self.mission_name.value).strip()
        if not name_text:
            await respond_ephemeral(interaction, "Mission name is required.")
            return
        description_text = str(self.description.value or "").strip()

        schedule_view = CreateMissionScheduleView(
            cog=self.cog,
            ctx=self.ctx,
            mission_name=name_text,
            pay_per_player=pay,
            location=location_text,
            mission_description=description_text,
            default_tz=self.default_tz,
            origin_channel_id=interaction.channel_id,
        )
        tz_label = MISSION_TZ_LABELS.get(self.default_tz, self.default_tz)
        embed = discord.Embed(
            title=f"🆕 New Mission — {name_text}",
            description=(
                f"**Location:** {location_text}\n"
                f"**Pay each:** ¥{pay:,} → bank\n\n"
                f"Pick the **date**, **start hour**, **duration**, and "
                f"**timezone** below, then press **Continue**.\n"
                f"_Default timezone: {tz_label} (your last-used). Everyone "
                "viewing the Discord event sees it in their own local time._"
            ),
            color=discord.Color.gold(),
        )
        await respond_ephemeral(interaction, embed=embed, view=schedule_view)


class CreateMissionAttendeesView(SafeView):
    def __init__(
        self,
        *,
        cog: "FixerHubCog",
        ctx,
        mission_name: str,
        start_utc: datetime,
        end_utc: datetime,
        pay_per_player: int,
        location: str,
        origin_channel_id: Optional[int],
        mission_description: str = "",
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx
        self.mission_name = mission_name
        self.start_utc = start_utc
        self.end_utc = end_utc
        self.pay_per_player = pay_per_player
        self.location = location
        self.mission_description = (mission_description or "").strip()
        self.origin_channel_id = origin_channel_id
        self.selected: list[discord.abc.User] = []
        self.custom_image_bytes: Optional[bytes] = None
        self.custom_image_name: Optional[str] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Choose attendees (up to 25)…",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def attendee_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.selected = list(select.values)
        names = ", ".join(getattr(u, "display_name", None) or u.name for u in self.selected)
        await respond_ephemeral(
            interaction,
            f"Selected {len(self.selected)} attendee(s): {names[:1500]}\n"
            "Press **Confirm & Create** to publish the event.",
        )

    @discord.ui.button(label="Attach Banner", style=discord.ButtonStyle.secondary, emoji="📎", row=1)
    async def attach_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await send_ephemeral(
            interaction,
            "📎 Paste / upload your banner image **in this channel** within "
            "the next 60 seconds. PNG / JPEG / WebP / GIF, ≤ 8 MiB. Send "
            "anything else (or wait it out) to keep the default banner.",
        )

        bot = interaction.client
        channel_id = interaction.channel_id
        author_id = interaction.user.id

        def _check(m: discord.Message) -> bool:
            return (
                m.author.id == author_id
                and m.channel.id == channel_id
                and bool(m.attachments)
            )

        try:
            msg = await bot.wait_for("message", check=_check, timeout=60)
        except Exception:
            await send_ephemeral(
                interaction,
                "⏱️ No image received — keeping the default banner.",
            )
            return

        att = msg.attachments[0]
        ctype = (att.content_type or "").lower()
        is_image = ctype.startswith("image/") or any(
            att.filename.lower().endswith(ext)
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")
        )
        if not is_image:
            await send_ephemeral(
                interaction,
                f"❌ `{att.filename}` doesn't look like an image — keeping the default banner.",
            )
            return
        if att.size and att.size > 8 * 1024 * 1024:
            await send_ephemeral(
                interaction,
                f"❌ `{att.filename}` is {att.size / (1024*1024):.1f} MiB "
                "(>8 MiB Discord cap) — keeping the default banner.",
            )
            return

        try:
            raw = await att.read()
        except Exception:
            logger.exception("Failed to read attached mission banner")
            await send_ephemeral(
                interaction,
                "❌ Failed to download that image — keeping the default banner.",
            )
            return
        if len(raw) > 8 * 1024 * 1024:
            await send_ephemeral(
                interaction,
                "❌ Image is over 8 MiB after download — keeping the default banner.",
            )
            return

        self.custom_image_bytes = raw
        self.custom_image_name = att.filename
        # Reflect on the button so the fixer knows it stuck.
        button.label = "Banner Attached ✓"
        button.style = discord.ButtonStyle.success
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except Exception:
            pass
        await send_ephemeral(
            interaction,
            f"✅ Using your custom banner `{att.filename}` "
            f"({len(raw) / 1024:.0f} KiB) for this mission.",
        )

    @discord.ui.button(label="Confirm & Create", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.selected:
            await send_ephemeral(interaction, "Pick at least one attendee first.")
            return
        creator_id = str(self.ctx.author.id)
        attendees = [str(u.id) for u in self.selected if str(u.id) != creator_id]
        if not attendees:
            await send_ephemeral(
                interaction,
                "All selected attendees were filtered (you can't pay yourself). "
                "Pick at least one player other than yourself.",
            )
            return

        guild = interaction.guild
        if guild is None:
            await send_ephemeral(interaction, "This must be used in a guild.")
            return

        # Build the scheduled event.
        event_title = f"Actors Needed: {self.mission_name}"
        image_bytes: Optional[bytes] = self.custom_image_bytes or _pick_mission_banner_bytes()
        # Compose the event description: fixer's optional brief on top,
        # then the auto-generated location/pay/fixer/attendees block. We
        # build a temporary row dict and reuse _build_mission_description
        # so the create-time text matches what Edit Mission later rewrites.
        event_description = _build_mission_description({
            "pay_per_player": self.pay_per_player,
            "attendee_ids": attendees,
            "creator_id": creator_id,
            "location": self.location,
            "mission_description": self.mission_description,
        })[:1000]
        kwargs: dict = dict(
            name=event_title[:100],
            description=event_description,
            start_time=self.start_utc,
            end_time=self.end_utc,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=self.location[:100],
            reason=f"Mission created by {self.ctx.author} via Fixer panel",
        )
        if image_bytes:
            kwargs["image"] = image_bytes

        try:
            event = await guild.create_scheduled_event(**kwargs)
        except Exception as e:
            logger.exception("create_scheduled_event failed")
            await send_ephemeral(
                interaction,
                f"Failed to create the Discord event: `{e}`",
            )
            return

        creator_username = (
            getattr(interaction.user, "display_name", None)
            or getattr(interaction.user, "name", str(creator_id))
        )
        mission_id = str(uuid.uuid4())
        payout_utc = compute_payout_ts(self.start_utc)
        ok = await mission_event_create(
            mission_id=mission_id,
            guild_id=str(guild.id),
            channel_id=str(self.origin_channel_id) if self.origin_channel_id else None,
            event_id=str(event.id),
            mission_name=self.mission_name,
            location=self.location,
            creator_id=creator_id,
            creator_username=str(creator_username),
            pay_per_player=self.pay_per_player,
            start_ts=self.start_utc,
            end_ts=self.end_utc,
            payout_ts=payout_utc,
            attendee_ids=attendees,
            mission_description=self.mission_description,
        )

        # Record an entry in each attendee's gig log immediately on creation
        # so the mission counts toward their record at sign-up time. Track
        # failures so the Fixer can see them and re-record manually.
        log_failures: list[str] = []
        if ok:
            attendee_date = self.start_utc.date()
            for uid in attendees:
                display_name = uid
                try:
                    member = guild.get_member(int(uid)) if guild else None
                    if member is None:
                        member = await self.cog.bot.fetch_user(int(uid))
                    display_name = (
                        getattr(member, "display_name", None)
                        or getattr(member, "name", uid)
                    )
                except Exception:
                    pass
                try:
                    result = await mission_log_record(
                        str(uid),
                        str(display_name)[:128],
                        attendee_date,
                        self.mission_name,
                    )
                except Exception:
                    logger.error(
                        "Failed to record mission_log on creation for user %s mission %s",
                        uid, mission_id, exc_info=True,
                    )
                    result = None
                if result is None:
                    log_failures.append(f"<@{uid}>")
                    logger.error(
                        "mission_log_record returned None for user %s mission %s "
                        "— attendee will be undercounted until re-recorded.",
                        uid, mission_id,
                    )
        if not ok:
            await send_ephemeral(
                interaction,
                "⚠️ Discord event was created but the DB record failed — the "
                "auto-payout will NOT fire. Please record the mission manually "
                "with `!mission_record` and pay the players via UnbelievaBoat. "
                f"Mission id: `{mission_id}`",
            )
            return

        # Disable the view so the Fixer doesn't double-submit. `interaction.message`
        # is the message that hosts this view (the ephemeral sent from the modal);
        # editing `interaction.edit_original_response` here would target the deferred
        # ephemeral of THIS button click, not the parent view container.
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except Exception:
            pass

        try:
            event_url = event.url  # type: ignore[attr-defined]
        except Exception:
            event_url = None
        attendee_mentions = ", ".join(f"<@{a}>" for a in attendees)
        ok_embed = discord.Embed(
            title=f"✅ Mission Created — {self.mission_name}",
            description=(
                f"**Event:** [Actors Needed: {self.mission_name}]({event_url})\n"
                if event_url else ""
            ) + (
                f"**Location:** {self.location}\n"
                f"**Start (UTC):** <t:{int(self.start_utc.timestamp())}:F>\n"
                f"**Pay each:** ¥{self.pay_per_player:,} → bank\n"
                f"**Auto-payout:** <t:{int(payout_utc.timestamp())}:F>\n"
                f"**Attendees ({len(attendees)}):** {attendee_mentions[:900]}"
            ),
            color=discord.Color.green() if not log_failures else discord.Color.orange(),
        )
        if log_failures:
            ok_embed.add_field(
                name=f"⚠️ Gig-log write failed ({len(log_failures)})",
                value=(
                    "These attendees were added to the mission but their gig "
                    "log entry failed to save. Use `!mission_record` to "
                    "credit them manually:\n"
                    + " ".join(log_failures)[:1000]
                ),
                inline=False,
            )
        await send_ephemeral(interaction, embed=ok_embed)
        await post_mission_audit(
            self.cog.bot,
            action="Mission Created",
            actor=interaction.user,
            mission_id=mission_id,
            mission_name=self.mission_name,
            fields=[
                ("Location", self.location),
                ("Start (UTC)", f"<t:{int(self.start_utc.timestamp())}:F>"),
                ("Pay each", f"¥{self.pay_per_player:,} → bank"),
                ("Auto-payout", f"<t:{int(payout_utc.timestamp())}:F>"),
                ("Attendees", _fmt_attendee_list(attendees)),
                (
                    "Description",
                    (self.mission_description or "_(none)_")[:1000],
                ),
            ],
            color=discord.Color.green(),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        await interaction.response.edit_message(
            content="Mission creation cancelled.",
            embed=None,
            view=self,
        )


GUN_STORE_OWNER_ROLE_ID = config.GUN_STORE_OWNER_ROLE_ID
RIPPERDOC_ROLE_ID = config.RIPPERDOC_ROLE_ID


class StoreSubView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="View Gun Store", style=discord.ButtonStyle.secondary, emoji="🔫", row=0)
    async def view_gun_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in a server.")
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
            await send_ephemeral(interaction, "No gun stores found.")
            return
        view = StoreOwnerPickerView(self.cog, self.ctx, options, store_type="gun")
        await send_ephemeral(interaction, 
            "🔫 **Gun Store** — Select a store:", view=view)

    @discord.ui.button(label="View Ripperdoc Store", style=discord.ButtonStyle.secondary, emoji="💉", row=0)
    async def view_ripperdoc_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in a server.")
            return
        cw_cog = interaction.client.get_cog("CyberwareShop")
        rd_stores = {}
        if cw_cog:
            state = await cw_cog._load_state()
            rd_stores = state.get("ripperdoc_stores", {})
        role = guild.get_role(RIPPERDOC_ROLE_ID)
        if not role or not role.members:
            await send_ephemeral(interaction, "No Ripperdocs found.")
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
            await send_ephemeral(interaction, "No Ripperdoc stores found. Ripperdocs must set up a store first.")
            return
        view = StoreOwnerPickerView(self.cog, self.ctx, options, store_type="cw")
        await send_ephemeral(interaction, 
            "💉 **Ripperdoc Store** — Select a Ripperdoc:", view=view)


class ReassignSourcePickerView(SafeView):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the item's current owner…", row=0)
    async def source_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await send_ephemeral(interaction, "Please select a player.")
            return
        guild = self.ctx.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in server.")
            return
        member = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if member is None:
            try:
                member = await guild.fetch_member(raw_user.id)
            except Exception:
                await send_ephemeral(interaction, "Could not find that member.")
                return
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await send_ephemeral(interaction, 
                f"{member.display_name} has no items to reassign.")
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
        await send_ephemeral(interaction, 
            f"✏️ **Step 2** — Select the item from **{member.display_name}**:",
            view=view)
        self.stop()


class ReassignItemPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, items: list):
        super().__init__(timeout=300)
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
            await respond_ephemeral(interaction, "Item not found.")
            return
        view = ReassignDestPickerView(self.cog, self.ctx, self.source_owner, item)
        await respond_ephemeral(interaction, 
            f"✏️ **Step 3** — Select the new owner for **{item.get('name', '?')}**:",
            view=view)
        self.stop()


class ReassignDestPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, item: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.source_owner = source_owner
        self.item = item

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select the new owner…", row=0)
    async def dest_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        raw_user = select.values[0] if select.values else None
        if raw_user is None:
            await send_ephemeral(interaction, "Please select a player.")
            return
        guild = self.ctx.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in server.")
            return
        member = raw_user if isinstance(raw_user, discord.Member) else guild.get_member(raw_user.id)
        if member is None:
            try:
                member = await guild.fetch_member(raw_user.id)
            except Exception:
                await send_ephemeral(interaction, "Could not find that member.")
                return
        chars = await get_active_characters(str(member.id))
        if not chars:
            await send_ephemeral(interaction, 
                f"{member.display_name} has no active characters.")
            return
        view = ReassignCharPickerView(
            self.cog, self.ctx, self.source_owner, self.item, member, chars[:25]
        )
        await send_ephemeral(interaction, 
            f"✏️ **Step 4** — Select the character on **{member.display_name}** to receive the item:",
            view=view)
        self.stop()


class ReassignCharPickerView(SafeView):
    def __init__(self, cog, ctx, source_owner: discord.Member, item: dict,
                 dest_owner: discord.Member, chars: list):
        super().__init__(timeout=300)
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
            await send_ephemeral(interaction, 
                f"❌ Character **{new_char_name}** is not active.")
            return

        new_char_id = char_record["character_id"] if char_record else None
        if str(self.dest_owner.id) == old_owner_id:
            ok = await pi_update_character(item_id, new_char_name, expected_owner_id=old_owner_id, new_character_id=new_char_id)
        else:
            ok = await pi_update_owner(item_id, str(self.dest_owner.id), new_char_name, old_owner_id, new_character_id=new_char_id)
        if not ok:
            await send_ephemeral(interaction, "Failed to reassign item.")
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
        await send_ephemeral(interaction, 
            f"✅ Reassigned **{item_name}** from {self.source_owner.display_name} "
            f"to {self.dest_owner.display_name} — {new_char_name}.")
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


class PlayerInvPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
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
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
        items = await pi_get_by_owner(str(member.id))
        if not items:
            await send_ephemeral(interaction, f"{member.display_name} has no items.")
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
        await send_ephemeral(interaction, embed=embed)


class PlayerAddItemPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.selected_player: Optional[discord.Member] = None
        self.selected_character: Optional[dict] = None
        self._character_select: Optional[discord.ui.Select] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
        self.selected_player = member
        self.selected_character = None
        characters = await get_active_characters(str(member.id))
        if not characters:
            await send_ephemeral(interaction, 
                f"❌ {member.display_name} has no active characters. "
                "They must create a character before receiving items.")
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
        await interaction.edit_original_response(
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
            await respond_ephemeral(interaction, "Character not found.")

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=2)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_player is None:
            await respond_ephemeral(interaction, "Please select a player first.")
            return
        if self.selected_character is None:
            await respond_ephemeral(interaction, "Please select a character.")
            return
        await interaction.response.defer(ephemeral=True)
        if not await ensure_character_active(self.selected_character["character_id"]):
            await send_ephemeral(interaction, 
                f"❌ Character **{self.selected_character['name']}** is no longer active.")
            return
        await send_ephemeral(interaction, 
            "📝 **Enter item details** in this format:\n"
            "`item name, type, quantity, cost, restriction`\n\n"
            "**For guns:** `name, gun, qty, cost, restriction, power_level, type, gun_class`\n"
            "Example: `Militech Pistol, gun, 1, 5000, basic, high, power, pistol`\n"
            "power_level: low/medium/high — type: power/smart/tech\n"
            "gun_class: pistol, revolver, submachine_gun, shotgun, assault_rifle, etc.\n\n"
            "**For cyberware:** `name, cyberware, qty, cost, restriction, cwp, slot`\n"
            "Example: `Kerenzikov, cyberware, 1, 3000, basic, 14, Neural`\n\n"
            "**For other items:** `name, type, qty, cost`\n"
            "Available types: **gun**, **cyberware**, **gear**, **misc** (default)\n"
            "Type and price are optional (defaults: `misc`, no price). Type `cancel` to abort.")
        text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.target_user_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        await interaction.response.edit_message(content="✅ **Accepted** — processing…", view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        await interaction.response.edit_message(content="❌ **Declined** — transaction cancelled.", view=None)
        self.stop()

    async def on_timeout(self):
        self.result = None
        self.stop()


async def _process_fixer_add_item(cog, interaction, player, character, text):
    guild = interaction.guild
    if not guild:
        await send_ephemeral(interaction, "Must be used in server.")
        return
    char_name = character.get("name", "")
    character_id = character.get("character_id")
    if not char_name:
        await send_ephemeral(interaction, "Character selection required.")
        return
    if character_id and not await ensure_character_active(character_id):
        await send_ephemeral(interaction, 
            f"❌ Character **{char_name}** is no longer active.")
        return
    parts = [p.strip() for p in text.split(",")]
    name = parts[0] if parts else ""
    if not name:
        await send_ephemeral(interaction, "❌ Item name is required.")
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

    gun_class_val = None
    if item_type == "gun":
        if len(parts) < 8:
            await send_ephemeral(interaction, 
                "❌ Gun items require: `name, gun, quantity, cost, restriction, power_level, type, gun_class`\n"
                "power_level: low/medium/high — type: power/smart/tech\n"
                "gun_class: pistol, revolver, shotgun, assault_rifle, etc.")
            return
        pl_raw = parts[5].strip().lower()
        type_raw = parts[6].strip().lower()
        gc_raw = parts[7].strip().lower().replace(" ", "_")
        if pl_raw not in VALID_GUN_POWER_LEVELS:
            await send_ephemeral(interaction, 
                f"❌ Invalid power_level `{pl_raw}`. Must be one of: low, medium, high.")
            return
        if type_raw not in VALID_GUN_TYPES:
            await send_ephemeral(interaction, 
                f"❌ Invalid gun type `{type_raw}`. Must be one of: power, smart, tech.")
            return
        if gc_raw not in VALID_GUN_CLASSES:
            gun_class_list = ", ".join(sorted(VALID_GUN_CLASSES))
            await send_ephemeral(interaction, 
                f"❌ Invalid gun class `{gc_raw}`. Must be one of: {gun_class_list}.")
            return
        power_level = pl_raw
        weapon_subtype = type_raw
        gun_class_val = gc_raw

    elif item_type == "cyberware":
        if len(parts) < 7:
            await send_ephemeral(interaction, 
                "❌ Cyberware items require: `name, cyberware, quantity, cost, restriction, cwp, slot`\n"
                "cwp: integer — slot: one of the valid body locations")
            return
        try:
            cwp_val = str(int(parts[5].strip()))
        except ValueError:
            await send_ephemeral(interaction, 
                f"❌ Invalid CWP `{parts[5].strip()}`. Must be an integer.")
            return
        slot_raw = parts[6].strip().lower()
        if slot_raw not in VALID_CW_SLOTS:
            await send_ephemeral(interaction, 
                f"❌ Invalid slot `{parts[6].strip()}`. Valid slots:\n"
                + "\n".join(f"• {s.title()}" for s in sorted(VALID_CW_SLOTS)))
            return
        slot_val = parts[6].strip()

    total_cost = (price or 0) * qty
    cash_deducted = 0
    bank_deducted = 0
    if price is not None and price > 0:
        confirm_view = _ConfirmItemView(target_user_id=player.id)
        try:
            dm_msg = await player.send(
                f"💰 A Fixer wants to add **{name}** ×{qty} to your inventory "
                f"for **${total_cost:,}** total. Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await send_ephemeral(interaction, 
                f"Cannot DM {player.display_name}. They may have DMs disabled.")
            return
        await send_ephemeral(interaction, 
            f"Confirmation sent to {player.display_name} via DM. Waiting…")
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await dm_msg.edit(content="⏰ Confirmation timed out — transaction cancelled.", view=None)
            except Exception:
                pass
            await send_ephemeral(interaction, "⏰ Player did not respond in time. Item not added.")
            return
        if confirm_view.result is False:
            await send_ephemeral(interaction, "❌ Player declined the transaction. Item not added.")
            return
        try:
            await dm_msg.edit(view=None)
        except Exception:
            pass
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await send_ephemeral(interaction, "❌ Economy system unavailable.")
            return
        balance = await ub.get_balance(player.id)
        if balance is None:
            await send_ephemeral(interaction, "❌ Could not fetch player balance.")
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await send_ephemeral(interaction, 
                f"❌ Player cannot afford ${total_cost:,} (has ${cash + bank:,}).")
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            player.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer add-item: {name} x{qty}"
        )
        if not ok_deduct:
            await send_ephemeral(interaction, "❌ Failed to deduct funds. Item not added.")
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
            "weapon_type": gun_class_val,
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
        await send_ephemeral(interaction, 
            f"⚠️ Added **{name}** ×{added}/{qty} to {player.display_name}'s inventory ({char_name}). "
            f"Payment was deducted but not all items were saved. Contact an admin for reconciliation.")
    else:
        await send_ephemeral(interaction, 
            f"Added **{name}** ×{added} to {player.display_name}'s inventory ({char_name}).")


class PlayerRemoveItemView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.selected_player: Optional[discord.Member] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
        self.selected_player = member
        await send_ephemeral(interaction, f"Player: **{member.display_name}** ✓")

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_player is None:
            await respond_ephemeral(interaction, "Please select a player first.")
            return
        await interaction.response.defer(ephemeral=True)
        player = self.selected_player
        items = await pi_get_by_owner(str(player.id))
        if not items:
            await send_ephemeral(interaction, 
                f"{player.display_name} has no items.")
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
        await send_ephemeral(interaction, 
            f"**{player.display_name}**'s inventory — select the item to remove:",
            view=step2)
        self.stop()


class RemoveItemPickerView(SafeView):
    def __init__(self, cog: "FixerHubCog", ctx: commands.Context,
                 player: discord.Member, grouped: dict[str, list[dict]]):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.player = player
        self.grouped = grouped
        self.selected_name: Optional[str] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(placeholder="Select item to remove…", row=0)
    async def item_dropdown(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_name = select.values[0]
        group = self.grouped.get(self.selected_name, [])
        count = len(group)
        if count > 1:
            await interaction.response.defer(ephemeral=True)
            await send_ephemeral(interaction, 
                f"**{self.selected_name}** — this player owns **{count}**. "
                f"How many to remove? (1-{count}, or type `cancel`):")
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
                return
            try:
                qty = int(text.strip())
            except ValueError:
                await send_ephemeral(interaction, "Invalid number.")
                return
            if qty < 1 or qty > count:
                await send_ephemeral(interaction, 
                    f"Quantity must be between 1 and {count}.")
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
        await send_ephemeral(interaction, 
            f"Removed **{self.selected_name}** {count_str} from {self.player.display_name}."
            if removed > 0
            else f"Failed to remove **{self.selected_name}**.")
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
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player…", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
        loa_cog = self.cog.bot.get_cog("LOA")
        if not loa_cog:
            await send_ephemeral(interaction, "LOA system unavailable.")
            return
        guild = self.ctx.guild
        if not guild:
            await send_ephemeral(interaction, "Must be used in server.")
            return
        loa_role = loa_cog.get_loa_role(guild)
        if loa_role is None:
            await send_ephemeral(interaction, "⚠️ LOA role is not configured.")
            return
        has_role = any(r.id == loa_role.id for r in member.roles)
        if self.action == "start":
            if has_role:
                await send_ephemeral(interaction, 
                    f"{member.display_name} is already on LOA.")
                return
            try:
                await member.add_roles(loa_role, reason=f"LOA start by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await send_ephemeral(interaction, f"❌ Could not assign LOA role: {e}")
                return
            log_ch = await _audit_channel(self.cog.bot)
            if log_ch:
                try:
                    await log_ch.send(
                        f"🏖️ **Fixer: Start LOA** — {interaction.user.display_name} put "
                        f"**{member.display_name}** ({member.id}) on LOA."
                    )
                except Exception:
                    pass
            await send_ephemeral(interaction, 
                f"✅ {member.display_name} is now on LOA.")
        else:
            if not has_role:
                await send_ephemeral(interaction, 
                    f"{member.display_name} is not currently on LOA.")
                return
            try:
                await member.remove_roles(loa_role, reason=f"LOA end by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await send_ephemeral(interaction, f"❌ Could not remove LOA role: {e}")
                return
            log_ch = await _audit_channel(self.cog.bot)
            if log_ch:
                try:
                    await log_ch.send(
                        f"🔚 **Fixer: End LOA** — {interaction.user.display_name} took "
                        f"**{member.display_name}** ({member.id}) off LOA."
                    )
                except Exception:
                    pass
            await send_ephemeral(interaction, 
                f"✅ {member.display_name}'s LOA has ended.")
        self.stop()


class FixerItemHistorySourceView(SafeView):
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
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        member = await _resolve_user_select(self.ctx, user)
        if not member:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
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
        view = FixerItemHistoryItemPickerView(self.cog, self.ctx, options, member.display_name)
        await interaction.edit_original_response(
            content=f"📜 **{member.display_name}** — Select an item to view history:",
            view=view,
        )


class FixerItemHistoryStorePickerView(SafeView):
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
        await interaction.response.defer(ephemeral=True)
        user = select.values[0] if select.values else None
        owner = await _resolve_user_select(self.ctx, user)
        if not owner:
            await send_ephemeral(interaction, "Could not resolve member.")
            return
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
                    gl = lot.get("gun_level", "")
                    gc = lot.get("gun_category", "")
                    tags = f"[{gl}]" if gl else ""
                    if gc:
                        tags = f"{tags} {gc}" if tags else gc
                    desc = f"${int(lot.get('unit_cost', 0)):,} {tags} — {iid[:8]}…"[:100]
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
            await send_ephemeral(interaction, 
                f"{owner.display_name}'s stores are empty.")
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
        history = await ih_get_history(item_id, limit=50)
        if not history:
            await send_ephemeral(interaction, f"No history for this item.")
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
        await send_ephemeral(interaction, embed=embed)


class StoreOwnerPickerView(SafeView):
    def __init__(self, cog, ctx, options: list, store_type: str):
        super().__init__(timeout=300)
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
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        owner_id = int(interaction.data["values"][0])
        guild = self.ctx.guild
        owner = guild.get_member(owner_id) if guild else None
        if not owner:
            await respond_ephemeral(interaction, "Could not resolve member.")
            return
        await interaction.response.defer(ephemeral=True)

        if self.store_type == "gun":
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await send_ephemeral(interaction, "Gun shop system unavailable.")
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
                from NightCityBot.utils.helpers import format_gun_lines_grouped
                lines = format_gun_lines_grouped(lots, qty_key="qty_remaining", max_items=25)
                embed = discord.Embed(
                    title=f"🔫 {store_title}",
                    description="\n".join(lines) if lines else "This store is currently empty.",
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
                await send_ephemeral(interaction, "Cyberware system unavailable.")
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
                from NightCityBot.utils.helpers import format_cw_lines_grouped
                groups = cw_cog._grouped_inventory(inventory)
                store_lots = []
                for g in groups:
                    sample = g["items"][0] if g.get("items") else {}
                    store_lots.append({
                        "item_name": g["name"],
                        "cwp": sample.get("cwp", ""),
                        "slot": sample.get("slot", ""),
                        "unit_cost": int(g.get("price_paid") or 0),
                        "qty_available": g["count"],
                    })
                lines = format_cw_lines_grouped(store_lots, max_items=30)
                embed = discord.Embed(
                    title=f"💉 {store_title}",
                    description="\n".join(lines) if lines else "Empty",
                    color=discord.Color.purple(),
                )
                embed.set_footer(text=f"{len(inventory)} item(s) total")
            else:
                embed = discord.Embed(
                    title=f"💉 {store_title}",
                    description="This store is currently empty.",
                    color=discord.Color.purple(),
                )

        action_view = StoreActionView(self.cog, self.ctx, owner, self.store_type)
        await send_ephemeral(interaction, embed=embed, view=action_view)


class StoreActionView(SafeView):
    def __init__(self, cog, ctx, owner: discord.Member, store_type: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner = owner
        self.store_type = store_type

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await respond_ephemeral(interaction, "This menu isn't for you.")
            return False
        return True

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if self.store_type == "gun":
            await send_ephemeral(interaction, 
                f"📝 **Add to {self.owner.display_name}'s Gun Store**\n"
                "Enter: `gun name, quantity, unit cost, restriction, power level, type`\n"
                "Example: `Militech Mk.31, 5, 5000, basic, medium, power`\n"
                "• **Restriction:** basic / controlled / restricted\n"
                "• **Power Level:** low / medium / high\n"
                "• **Type:** power / smart / tech\n"
                "Type `cancel` to abort.")
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
                return
            await _process_store_add_gun(self.cog, interaction, self.owner, text)
        else:
            await send_ephemeral(interaction, 
                f"📝 **Add to {self.owner.display_name}'s Ripperdoc Store**\n"
                "Enter: `cyberware name, quantity, unit cost, cwp, slot`\n"
                "Example: `Kiroshi Optics, 3, 8000, 14, ocular system`\n"
                "• **CWP:** Cyberware Power (integer)\n"
                "• **Slot:** " + ", ".join(sorted(VALID_CW_SLOTS)) + "\n"
                "Type `cancel` to abort.")
            text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if text is None:
                await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
                return
            await _process_store_add_cw(self.cog, interaction, self.owner, text)

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        if self.store_type == "gun":
            guns_cog = self.cog.bot.cogs.get("GunsShopCog")
            if not guns_cog:
                await send_ephemeral(interaction, "Gun shop system unavailable.")
                return
            state = await guns_cog._load_state()
            store_id = guns_cog._store_id(guild.id, self.owner.id)
            lots = [
                l for l in state.get("stores", {}).get(store_id, {}).get("lots", [])
                if l.get("qty_remaining", 0) > 0
            ]
            if not lots:
                await send_ephemeral(interaction, 
                    f"{self.owner.display_name}'s gun store is empty — nothing to remove.")
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
            await send_ephemeral(interaction, 
                f"🗑️ **Remove from {self.owner.display_name}'s Gun Store** — Select the lot:",
                view=view)
        else:
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if not cw_cog:
                await send_ephemeral(interaction, "Cyberware system unavailable.")
                return
            inventory = await cw_cog._load_inventory(self.owner.id)
            if not inventory:
                await send_ephemeral(interaction, 
                    f"{self.owner.display_name}'s Ripperdoc stock is empty — nothing to remove.")
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
            await send_ephemeral(interaction, 
                f"🗑️ **Remove from {self.owner.display_name}'s Ripperdoc Store** — Select the item:",
                view=view)


class StoreRemoveLotPickerView(SafeView):
    def __init__(self, cog, ctx, owner: discord.Member, options: list, store_type: str):
        super().__init__(timeout=300)
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
            await respond_ephemeral(interaction, "This menu isn't for you.")
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
        await send_ephemeral(interaction, "Must be used in server.")
        return
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await send_ephemeral(interaction, "Gun shop system unavailable.")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await send_ephemeral(interaction, "❌ Need at least: `gun name, quantity, unit cost`")
        return
    gun_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await send_ephemeral(interaction, "Quantity and cost must be numbers.")
        return
    if qty < 1 or cost < 0:
        await send_ephemeral(interaction, "Invalid quantity or cost.")
        return
    restriction = parts[3].strip().lower() if len(parts) > 3 else ""
    while restriction not in ("basic", "controlled", "restricted"):
        await send_ephemeral(interaction, 
            f"Got: **{gun_name}** ×{qty} at ${cost:,} — now enter the restriction level:\n"
            "`basic`, `controlled`, or `restricted`\n"
            "Type `cancel` to abort.")
        r_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if r_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        restriction = r_text.strip().lower()
        if restriction not in ("basic", "controlled", "restricted"):
            await send_ephemeral(interaction, 
                "❌ Invalid restriction. Must be `basic`, `controlled`, or `restricted`. Try again.")
    pl_map = {"low": "L", "medium": "M", "high": "H"}
    power_level = parts[4].strip().lower() if len(parts) > 4 else ""
    while power_level not in VALID_GUN_POWER_LEVELS:
        await send_ephemeral(interaction, 
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] — now enter the **power level**:\n"
            "`low`, `medium`, or `high`\n"
            "Type `cancel` to abort.")
        pl_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if pl_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        power_level = pl_text.strip().lower()
        if power_level not in VALID_GUN_POWER_LEVELS:
            await send_ephemeral(interaction, "❌ Invalid power level. Must be `low`, `medium`, or `high`. Try again.")
    gun_level = pl_map[power_level]
    gun_type = parts[5].strip().lower() if len(parts) > 5 else ""
    while gun_type not in VALID_GUN_TYPES:
        await send_ephemeral(interaction, 
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}] — now enter the **weapon type**:\n"
            "`power`, `smart`, or `tech`\n"
            "Type `cancel` to abort.")
        wt_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if wt_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        gun_type = wt_text.strip().lower()
        if gun_type not in VALID_GUN_TYPES:
            await send_ephemeral(interaction, "❌ Invalid weapon type. Must be `power`, `smart`, or `tech`. Try again.")
    gun_category = gun_type.title()
    gun_class = parts[6].strip().lower().replace(" ", "_") if len(parts) > 6 else ""
    gun_class_list = ", ".join(f"`{c}`" for c in sorted(VALID_GUN_CLASSES))
    while gun_class not in VALID_GUN_CLASSES:
        await send_ephemeral(interaction, 
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}/{gun_category}] — now enter the **gun class**:\n"
            f"{gun_class_list}\n"
            "Type `cancel` to abort.")
        gc_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if gc_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        gun_class = gc_text.strip().lower().replace(" ", "_")
        if gun_class not in VALID_GUN_CLASSES:
            await send_ephemeral(interaction, f"❌ Invalid gun class. Valid options: {gun_class_list}. Try again.")

    total_cost = cost * qty
    cash_deducted = 0
    bank_deducted = 0
    if cost > 0:
        confirm_view = _ConfirmItemView(target_user_id=owner.id)
        try:
            dm_msg = await owner.send(
                f"💰 A Fixer wants to add **{gun_name}** ×{qty} at **${total_cost:,}** total to your store. Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await send_ephemeral(interaction, 
                f"Cannot DM {owner.display_name}. They may have DMs disabled.")
            return
        await send_ephemeral(interaction, 
            f"Confirmation sent to {owner.display_name} via DM. Waiting…")
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await dm_msg.edit(content="⏰ Confirmation timed out — cancelled.", view=None)
            except Exception:
                pass
            await send_ephemeral(interaction, "⏰ Store owner did not respond. Item not added.")
            return
        if confirm_view.result is False:
            await send_ephemeral(interaction, "❌ Store owner declined. Item not added.")
            return
        try:
            await dm_msg.edit(view=None)
        except Exception:
            pass
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await send_ephemeral(interaction, "❌ Economy system unavailable.")
            return
        balance = await ub.get_balance(owner.id)
        if balance is None:
            await send_ephemeral(interaction, "❌ Could not fetch store owner's balance.")
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await send_ephemeral(interaction, 
                f"❌ Store owner cannot afford ${total_cost:,} (has ${cash + bank:,}).")
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            owner.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer store-add gun: {gun_name} x{qty}"
        )
        if not ok_deduct:
            await send_ephemeral(interaction, "❌ Failed to deduct funds. Item not added.")
            return

    async with guns_cog.lock:
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(guild.id, owner.id)
        store = state.setdefault("stores", {}).setdefault(store_id, {"lots": []})
        lot_id = f"fixer-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        store["lots"].append({
            "lot_id": lot_id,
            "gun_name": gun_name,
            "gun_level": gun_level,
            "gun_category": gun_category,
            "weapon_type": gun_class,
            "unit_cost": cost,
            "qty_remaining": qty,
            "restriction": restriction,
        })
        saved = await guns_cog._save_state(state)
    if not saved and cost > 0:
        ub = getattr(cog.bot, "unbelievaboat", None)
        refund_ok = False
        if ub:
            refund_ok = await ub.update_balance(
                owner.id, {"cash": cash_deducted, "bank": bank_deducted},
                reason=f"Fixer store-add gun refund: save failed for {gun_name} x{qty}"
            )
        if not refund_ok:
            logger.critical(
                "fixer store-add gun: refund ALSO failed — owner=%s amount=%s gun=%s",
                owner.id, cost * qty, gun_name,
            )
            await pt_create({
                "seller_id": str(interaction.user.id),
                "buyer_id": str(owner.id),
                "item_id": str(uuid.uuid4()),
                "amount": cost * qty,
                "reason": f"Fixer store-add gun refund failed: {gun_name} x{qty}",
            })
        await send_ephemeral(interaction, 
            f"❌ Failed to save store inventory. Funds have been refunded.")
        return
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        try:
            await log_ch.send(
                f"📥 **Fixer: Store Gun Added** — {interaction.user.display_name} added "
                f"**{gun_name}** ×{qty} at ${cost:,} [{restriction}] to {owner.display_name}'s store."
            )
        except Exception:
            pass
    await send_ephemeral(interaction, 
        f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}/{gun_category}] ({GUN_CLASS_DISPLAY_NAMES.get(gun_class, gun_class)}) to {owner.display_name}'s store.")


async def _process_store_add_cw(cog, interaction, owner, text):
    guild = interaction.guild
    if not guild:
        await send_ephemeral(interaction, "Must be used in server.")
        return
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await send_ephemeral(interaction, "Cyberware system unavailable.")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        await send_ephemeral(interaction, "❌ Need at least: `cyberware name, quantity, unit cost`")
        return
    item_name = parts[0]
    try:
        qty = int(parts[1])
        cost = int(parts[2])
    except ValueError:
        await send_ephemeral(interaction, "Quantity and cost must be numbers.")
        return
    if qty < 1 or cost < 0:
        await send_ephemeral(interaction, "Invalid quantity or cost.")
        return
    cwp_raw = parts[3].strip() if len(parts) > 3 else ""
    while True:
        if cwp_raw == "":
            await send_ephemeral(interaction, 
                f"Got: **{item_name}** ×{qty} at ${cost:,} — now enter the **CWP** (integer):\n"
                "Type `cancel` to abort.")
            cwp_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if cwp_text is None:
                await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
                return
            cwp_raw = cwp_text.strip()
        try:
            cwp = int(cwp_raw)
            break
        except ValueError:
            await send_ephemeral(interaction, "❌ CWP must be an integer. Try again.")
            cwp_raw = ""
    slot_raw = parts[4].strip().lower() if len(parts) > 4 else ""
    while slot_raw not in VALID_CW_SLOTS:
        slot_list = "\n".join(f"• {s}" for s in sorted(VALID_CW_SLOTS))
        await send_ephemeral(interaction, 
            f"Got: **{item_name}** ×{qty} at ${cost:,}, CWP:{cwp} — now enter the **slot**:\n"
            f"{slot_list}\n"
            "Type `cancel` to abort.")
        slot_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if slot_text is None:
            await send_ephemeral(interaction, "⏰ Timed out or cancelled.")
            return
        slot_raw = slot_text.strip().lower()
        if slot_raw not in VALID_CW_SLOTS:
            await send_ephemeral(interaction, "❌ Invalid slot. Try again.")

    total_cost = cost * qty
    cash_deducted = 0
    bank_deducted = 0
    if cost > 0:
        confirm_view = _ConfirmItemView(target_user_id=owner.id)
        try:
            dm_msg = await owner.send(
                f"💰 A Fixer wants to add **{item_name}** ×{qty} at **${total_cost:,}** total to your Ripperdoc store. Do you accept?",
                view=confirm_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await send_ephemeral(interaction, 
                f"Cannot DM {owner.display_name}. They may have DMs disabled.")
            return
        await send_ephemeral(interaction, 
            f"Confirmation sent to {owner.display_name} via DM. Waiting…")
        await confirm_view.wait()
        if confirm_view.result is None:
            try:
                await dm_msg.edit(content="⏰ Confirmation timed out — cancelled.", view=None)
            except Exception:
                pass
            await send_ephemeral(interaction, "⏰ Store owner did not respond. Item not added.")
            return
        if confirm_view.result is False:
            await send_ephemeral(interaction, "❌ Store owner declined. Item not added.")
            return
        try:
            await dm_msg.edit(view=None)
        except Exception:
            pass
        ub = getattr(cog.bot, "unbelievaboat", None)
        if ub is None:
            await send_ephemeral(interaction, "❌ Economy system unavailable.")
            return
        balance = await ub.get_balance(owner.id)
        if balance is None:
            await send_ephemeral(interaction, "❌ Could not fetch store owner's balance.")
            return
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        if cash + bank < total_cost:
            await send_ephemeral(interaction, 
                f"❌ Store owner cannot afford ${total_cost:,} (has ${cash + bank:,}).")
            return
        cash_deducted = min(max(cash, 0), total_cost)
        bank_deducted = max(0, total_cost - cash_deducted)
        ok_deduct = await ub.update_balance(
            owner.id, {"cash": -cash_deducted, "bank": -bank_deducted},
            reason=f"Fixer store-add cyberware: {item_name} x{qty}"
        )
        if not ok_deduct:
            await send_ephemeral(interaction, "❌ Failed to deduct funds. Item not added.")
            return

    async with cw_cog._locks.acquire(str(owner.id)):
        inventory = await cw_cog._load_inventory(owner.id)
        for _ in range(qty):
            inv_item = {
                "item_id": str(uuid.uuid4()),
                "name": item_name,
                "price_paid": cost,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            }
            if cwp:
                inv_item["cwp"] = cwp
            if slot_raw:
                inv_item["slot"] = slot_raw
            inventory.append(inv_item)
        saved = await cw_cog._save_inventory(owner.id, inventory)
    if not saved and cost > 0:
        ub = getattr(cog.bot, "unbelievaboat", None)
        refund_ok = False
        if ub:
            refund_ok = await ub.update_balance(
                owner.id, {"cash": cash_deducted, "bank": bank_deducted},
                reason=f"Fixer store-add CW refund: save failed for {item_name} x{qty}"
            )
        if not refund_ok:
            logger.critical(
                "fixer store-add CW: refund ALSO failed — owner=%s amount=%s item=%s",
                owner.id, cost * qty, item_name,
            )
            await pt_create({
                "seller_id": str(interaction.user.id),
                "buyer_id": str(owner.id),
                "item_id": str(uuid.uuid4()),
                "amount": cost * qty,
                "reason": f"Fixer store-add CW refund failed: {item_name} x{qty}",
            })
        await send_ephemeral(interaction, 
            f"❌ Failed to save clinic inventory. Funds have been refunded.")
        return
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        try:
            await log_ch.send(
                f"📥 **Fixer: Store Cyberware Added** — {interaction.user.display_name} added "
                f"**{item_name}** ×{qty} at ${cost:,} (CWP:{cwp}, {slot_raw}) to {owner.display_name}'s Ripperdoc store."
            )
        except Exception:
            pass
    await send_ephemeral(interaction, 
        f"Added **{item_name}** ×{qty} at ${cost:,} (CWP:{cwp}, {slot_raw}) to {owner.display_name}'s Ripperdoc store.")


async def _process_store_remove_gun(cog, interaction, owner, lot_id):
    guild = interaction.guild
    if not guild:
        await send_ephemeral(interaction, "Must be used in server.")
        return
    guns_cog = cog.bot.cogs.get("GunsShopCog")
    if not guns_cog:
        await send_ephemeral(interaction, "Gun shop system unavailable.")
        return
    async with guns_cog.lock:
        state = await guns_cog._load_state()
        store_id = guns_cog._store_id(guild.id, owner.id)
        store = state.get("stores", {}).get(store_id)
        if not store:
            await send_ephemeral(interaction, "Store not found.")
            return
        lot = next((l for l in store.get("lots", []) if l.get("lot_id") == lot_id), None)
        if not lot:
            await send_ephemeral(interaction, f"Lot not found in store.")
            return
        gun_name = lot.get("gun_name", "?")
        removed = int(lot.get("qty_remaining", 0))
        store["lots"].remove(lot)
        await guns_cog._save_state(state)
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        try:
            await log_ch.send(
                f"🗑️ **Fixer: Store Gun Removed** — {interaction.user.display_name} removed "
                f"**{gun_name}** ×{removed} from {owner.display_name}'s store."
            )
        except Exception:
            pass
    await send_ephemeral(interaction, 
        f"Removed **{gun_name}** ×{removed} from {owner.display_name}'s store.")


async def _process_store_remove_cw(cog, interaction, owner, item_id):
    cw_cog = cog.bot.cogs.get("CyberwareShop")
    if not cw_cog:
        await send_ephemeral(interaction, "Cyberware system unavailable.")
        return
    async with cw_cog._locks.acquire(str(owner.id)):
        inventory = await cw_cog._load_inventory(owner.id)
        item = next((i for i in inventory if i.get("item_id") == item_id), None)
        if not item:
            await send_ephemeral(interaction, "Item not found in store.")
            return
        item_name = item.get("name", "?")
        inventory.remove(item)
        await cw_cog._save_inventory(owner.id, inventory)
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        try:
            await log_ch.send(
                f"🗑️ **Fixer: Store Cyberware Removed** — {interaction.user.display_name} removed "
                f"**{item_name}** from {owner.display_name}'s Ripperdoc store."
            )
        except Exception:
            pass
    await send_ephemeral(interaction, 
        f"Removed **{item_name}** from {owner.display_name}'s Ripperdoc store.")


async def _process_wh_add_gun(cog, interaction, text, msg=None):
    async def _reply(content):
        if msg:
            await msg.edit(content=content)
        else:
            await send_ephemeral(interaction, content)

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
    pl_map = {"low": "L", "medium": "M", "high": "H"}
    power_level = parts[4].strip().lower() if len(parts) > 4 else ""
    while power_level not in VALID_GUN_POWER_LEVELS:
        await _reply(
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] — now enter the **power level**:\n"
            "`low`, `medium`, or `high`\n"
            "Type `cancel` to abort."
        )
        pl_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if pl_text is None:
            await _reply("⏰ Timed out or cancelled.")
            return
        power_level = pl_text.strip().lower()
        if power_level not in VALID_GUN_POWER_LEVELS:
            await _reply("❌ Invalid power level. Must be `low`, `medium`, or `high`. Try again.")
    gun_level = pl_map[power_level]
    gun_type = parts[5].strip().lower() if len(parts) > 5 else ""
    while gun_type not in VALID_GUN_TYPES:
        await _reply(
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}] — now enter the **weapon type**:\n"
            "`power`, `smart`, or `tech`\n"
            "Type `cancel` to abort."
        )
        wt_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if wt_text is None:
            await _reply("⏰ Timed out or cancelled.")
            return
        gun_type = wt_text.strip().lower()
        if gun_type not in VALID_GUN_TYPES:
            await _reply("❌ Invalid weapon type. Must be `power`, `smart`, or `tech`. Try again.")
    gun_category = gun_type.title()
    gun_class = parts[6].strip().lower().replace(" ", "_") if len(parts) > 6 else ""
    gun_class_list = ", ".join(f"`{c}`" for c in sorted(VALID_GUN_CLASSES))
    while gun_class not in VALID_GUN_CLASSES:
        await _reply(
            f"Got: **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}/{gun_category}] — now enter the **gun class**:\n"
            f"{gun_class_list}\n"
            "Type `cancel` to abort."
        )
        gc_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if gc_text is None:
            await _reply("⏰ Timed out or cancelled.")
            return
        gun_class = gc_text.strip().lower().replace(" ", "_")
        if gun_class not in VALID_GUN_CLASSES:
            await _reply(f"❌ Invalid gun class. Valid options: {gun_class_list}. Try again.")
    async with guns_cog.lock:
        state = await guns_cog._load_state()
        lots = state.setdefault("wholesale_lots", [])
        lot_id = f"fixer-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        lots.append({
            "lot_id": lot_id,
            "gun_name": gun_name,
            "gun_level": gun_level,
            "gun_category": gun_category,
            "weapon_type": gun_class,
            "unit_cost": cost,
            "qty_available": qty,
            "restriction": restriction,
        })
        await guns_cog._save_state(state)
    await _reply(f"Added **{gun_name}** ×{qty} at ${cost:,} [{restriction}] [{power_level}/{gun_category}] ({GUN_CLASS_DISPLAY_NAMES.get(gun_class, gun_class)}) to wholesale.")
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
            await send_ephemeral(interaction, content)

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
    cwp_raw = parts[3].strip() if len(parts) > 3 else ""
    while True:
        if cwp_raw == "":
            await _reply(
                f"Got: **{item_name}** ×{qty} at ${cost:,} — now enter the **CWP** (integer):\n"
                "Type `cancel` to abort."
            )
            cwp_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
            if cwp_text is None:
                await _reply("⏰ Timed out or cancelled.")
                return
            cwp_raw = cwp_text.strip()
        try:
            cwp = int(cwp_raw)
            break
        except ValueError:
            await _reply("❌ CWP must be an integer. Try again.")
            cwp_raw = ""
    slot_raw = parts[4].strip().lower() if len(parts) > 4 else ""
    while slot_raw not in VALID_CW_SLOTS:
        slot_list = "\n".join(f"• {s}" for s in sorted(VALID_CW_SLOTS))
        await _reply(
            f"Got: **{item_name}** ×{qty} at ${cost:,}, CWP:{cwp} — now enter the **slot**:\n"
            f"{slot_list}\n"
            "Type `cancel` to abort."
        )
        slot_text = await collect_text_input(interaction.client, interaction.channel_id, interaction.user.id)
        if slot_text is None:
            await _reply("⏰ Timed out or cancelled.")
            return
        slot_raw = slot_text.strip().lower()
        if slot_raw not in VALID_CW_SLOTS:
            await _reply("❌ Invalid slot. Try again.")
    async with cw_cog.lock:
        state = await cw_cog._load_state()
        lots = state.setdefault("cw_wholesale_lots", [])
        lot_id = f"fixer-cw-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        lots.append({
            "lot_id": lot_id,
            "item_name": item_name,
            "unit_cost": cost,
            "cwp": cwp,
            "slot": slot_raw,
            "qty_available": qty,
        })
        await cw_cog._save_state(state)
    await _reply(f"Added cyberware **{item_name}** ×{qty} at ${cost:,} (CWP:{cwp}, {slot_raw}) to wholesale.")
    log_ch = await _audit_channel(cog.bot)
    if log_ch:
        embed = discord.Embed(
            title="📥 Fixer: Cyberware Wholesale Restocked",
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Fixer", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="Item", value=item_name, inline=True)
        embed.add_field(name="Qty", value=str(qty), inline=True)
        embed.add_field(name="Cost", value=f"${cost:,}", inline=True)
        embed.set_footer(text="NightCityBot Audit Log")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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
                "**Store** — Gun store and Ripperdoc stock management"
            ),
            color=discord.Color.dark_gold(),
        )

    @staticmethod
    def _guide_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📘 Fixer Panel — How It Works",
            description=(
                "This panel is for Fixers to manage players and inspect player-owned stores. "
                "Pick a category below to open its sub-menu. "
                "All responses are private and **auto-delete after 5 minutes**."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="👤 Player",
            value=(
                "Manage any player's inventory and status:\n"
                "• **View Inventory** — look up a player's items\n"
                "• **Add / Remove Item** — grant or delete items\n"
                "• **Reassign Item** — transfer an item to a different owner or character\n"
                "• **Start / End LOA** — toggle a player's Leave of Absence"
            ),
            inline=False,
        )
        embed.add_field(
            name="🏪 Store",
            value=(
                "Inspect player-owned stores:\n"
                "• **View Gun Store** — browse a gun store's current stock\n"
                "• **View Ripperdoc Store** — browse a ripperdoc's current stock"
            ),
            inline=False,
        )
        embed.add_field(
            name="🏭 Wholesaler",
            value=(
                "Manage the catalogue overlay feeding gun + ripperdoc stores:\n"
                "• **View Stock** — see current gun + cyberware catalogue plus custom lots\n"
                "• **Add Gun / Add Cyberware** — add a custom lot that overlays the catalogue"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎯 Missions",
            value=(
                "Track and run player missions:\n"
                "• **🔎 Check Missions** — show a player's mission count, "
                "last date, recent titles + the fixer who ran each\n"
                "• **✅ Record Mission** — log today's mission for one or more "
                "players (`!mission_record … date=YYYY-MM-DD` for a custom date)\n"
                "• **🆕 Create Mission** — modal (name / pay / location / "
                "optional description) → date / time / duration / timezone "
                "dropdowns. Schedules a Discord *Actors Needed* event, "
                "credits attendees on sign-up, auto-pays each one (to bank) "
                "the midnight ET after start. Fixer is never paid. "
                "**📎 Attach Banner** for a custom cover.\n"
                "• **🎭 Actor Pay** — pick actor(s) + a recent mission, "
                "enter per-actor pay; pays via UnbelievaBoat\n"
                "• **🔎 Check Actor** — show acting count, dates, and which "
                "missions + fixer someone acted for\n"
                "• **✏️ Edit Mission** — change an active mission's "
                "date/time, attendees, or pay, or cancel it (reverses "
                "gig-log credits by default, title-matched so same-day "
                "duplicates stay safe)"
            ),
            inline=False,
        )
        return embed

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
        await channel.send(embed=self._guide_embed(), view=view)
        await ctx.send("✅ Fixer panel posted.", ephemeral=True)
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FixerHubCog(bot))
