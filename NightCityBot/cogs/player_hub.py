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
from NightCityBot.utils.player_inventory import (
    query_player_inventory as pi_get_by_owner,
    get_player_item as pi_get_item,
    delete_player_item as pi_delete_item,
    transfer_player_item as pi_update_owner,
    insert_player_item as pi_add_item,
)
from NightCityBot.utils.db import ih_record_event, pt_create
from NightCityBot.utils.characters import (
    create_character,
    get_active_characters,
    get_inactive_characters,
    deactivate_character,
    reactivate_character,
    character_name_exists,
)

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

    def _inv_system_enabled(self) -> bool:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("player_inventory"):
            return False
        return True

    @commands.command(name="player")
    async def player_cmd(self, ctx: commands.Context):
        """Open the player hub — view inventory, trade items, give items."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        embed = discord.Embed(
            title="🎒 Player Hub",
            description="Manage your inventory, trade with other players, give items, or sell guns to a store.",
            color=discord.Color.blue(),
        )
        view = PlayerHubView(self, ctx)
        msg = await ctx.send(embed=embed, view=view, delete_after=120)
        view.message = msg
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
                "`!player` — open the interactive player panel.\n\n"
                "From the panel you can:\n"
                "• **View Inventory** — see all your items grouped by character\n"
                "• **Trade Item** — sell an item to another player (with payment)\n"
                "• **Give Item** — transfer an item for free\n"
                "• **Sell to Store** — sell any gun to a gunstore owner\n"
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
        filtered = [i for i in items if i.get("character_name") == char_filter]
        label = f"{display_name}'s Inventory — {char_filter}"
    else:
        filtered = items
        label = f"{display_name}'s Inventory"
    if not filtered:
        return discord.Embed(title=f"📦 {label}", description="No items.", color=discord.Color.blue())
    display_lines, _ = inv_cog._build_display(filtered)
    item_lines = [(rn, ln) for rn, ln in display_lines if rn is not None]
    total_groups = len(item_lines)
    total_pages = max(1, (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page_rows = {rn for rn, _ in item_lines[:GROUPS_PER_PAGE]}
    page_lines: list[str] = []
    pending_header = None
    for rn, ln in display_lines:
        if rn is None:
            pending_header = ln
        else:
            if rn in page_rows:
                if pending_header is not None:
                    page_lines.append(pending_header)
                    pending_header = None
                page_lines.append(ln)
    embed = discord.Embed(
        title=f"📦 {label} (1/{total_pages})",
        description="\n".join(page_lines) if page_lines else "No items.",
        color=discord.Color.blue(),
    )
    hint = f"Use `!my_inventory 2` to see page 2." if total_pages > 1 else ""
    embed.set_footer(
        text=f"{len(filtered)} total item(s) | Row numbers are used for Trade and Give."
        + (f" | {hint}" if hint else "")
    )
    return embed


class InventoryCharFilterView(discord.ui.View):
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


class PlayerHubView(discord.ui.View):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.primary, emoji="📦", row=0)
    async def view_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        char_names = sorted({item.get("character_name", "") for item in items if item.get("character_name")})
        if len(char_names) > 1:
            view = InventoryCharFilterView(self.cog, self.ctx, items, inv_cog, char_names)
            await interaction.followup.send(
                "🔎 **Filter inventory by character** (or select **All Characters**):",
                view=view,
                ephemeral=True,
            )
        else:
            embed = _build_inventory_embed(interaction.user.display_name, items, inv_cog)
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Trade Item", style=discord.ButtonStyle.success, emoji="💱", row=0)
    async def trade_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to trade.", ephemeral=True)
            return
        view = TradeSetupView(self.cog, self.ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the buyer and the item to trade:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Sell to Store", style=discord.ButtonStyle.primary, emoji="🏪", row=1)
    async def sell_to_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
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
        gun_items = [i for i in items if i.get("item_type") == "gun"]
        if not gun_items:
            await interaction.followup.send("📦 You have no guns to sell to a store.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(gun_items)
        if not all_groups:
            await interaction.followup.send("📦 You have no guns to sell to a store.", ephemeral=True)
            return
        view = SellToStoreSetupView(self.cog, self.ctx, all_groups, seller_chars)
        await interaction.followup.send(
            "**Step 1** — Select the store owner, your character, and the gun to sell:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Give Item", style=discord.ButtonStyle.secondary, emoji="🎁", row=1)
    async def give_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return
        items = await pi_get_by_owner(str(interaction.user.id))
        if not items:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
        if not inv_cog:
            await interaction.followup.send("Inventory system unavailable.", ephemeral=True)
            return
        _, all_groups = inv_cog._build_display(items)
        if not all_groups:
            await interaction.followup.send("📦 Your inventory is empty — nothing to give.", ephemeral=True)
            return
        view = GiveSetupView(self.cog, self.ctx, all_groups)
        await interaction.followup.send(
            "**Step 1** — Select the recipient and the item to give:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Create Character", style=discord.ButtonStyle.success, emoji="🧑", row=2)
    async def create_char(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "🧑 **Create Character** — Please type your new character's name below (max 64 characters).\n"
            "You have 60 seconds to reply.",
            ephemeral=True,
        )

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await self.cog.bot.wait_for("message", check=check, timeout=60)
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

        log_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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

    @discord.ui.button(label="Manage Characters", style=discord.ButtonStyle.secondary, emoji="📋", row=2)
    async def manage_chars(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        view = ManageCharactersView(self.cog, self.ctx)
        await interaction.followup.send(
            "📋 **Manage Characters** — Choose an action:",
            view=view,
            ephemeral=True,
        )


class ManageCharactersView(discord.ui.View):
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


class DeactivateCharacterView(discord.ui.View):
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


class ReactivateCharacterView(discord.ui.View):
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


class TradeConfirmView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.accepted: Optional[bool] = None

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


class TradeSetupView(discord.ui.View):
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
            row=1,
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
            row=2,
        )
        char_select.callback = self._on_buyer_char_select
        self._buyer_char_select = char_select
        self.add_item(char_select)

        await interaction.response.send_message(
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
        modal = TradeDetailsModal(self.cog, self.selected_buyer, group, self.selected_buyer_char_name)
        await interaction.response.send_modal(modal)
        self.stop()


class TradeDetailsModal(discord.ui.Modal, title="Trade — Finalize Details"):
    price_input = discord.ui.TextInput(
        label="Price ($)",
        placeholder="e.g. 5000 (0 for free)",
    )

    def __init__(self, cog: PlayerHubCog, buyer: discord.Member, group: dict, buyer_character: str = ""):
        super().__init__()
        self.cog = cog
        self.buyer = buyer
        self.group = group
        self.buyer_character = buyer_character

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return

        buyer = self.buyer
        try:
            price = int(self.price_input.value.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Price must be a number.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("❌ Price cannot be negative.", ephemeral=True)
            return
        if buyer.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You cannot trade items to yourself.",
                ephemeral=True,
            )
            return

        buyer_character = self.buyer_character
        if not buyer_character:
            await interaction.followup.send("❌ Buyer character name is required.", ephemeral=True)
            return

        selected_item = self.group["items"][0]
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

        inv_cog = self.cog.bot.cogs.get("PlayerInventory")

        if buyer.id != interaction.user.id:
            price_str = f"**${price:,}**" if price > 0 else "**free**"
            confirm_view = TradeConfirmView(timeout=60)
            try:
                dm_msg = await buyer.send(
                    f"**{interaction.user.display_name}** wants to trade you **{item_name}** "
                    f"for {price_str} (character: **{buyer_character}**).\n"
                    "Do you accept?",
                    view=confirm_view,
                )
            except discord.Forbidden:
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
                alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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

        ok_transfer = await pi_update_owner(
            item_id, str(buyer.id), buyer_character, str(interaction.user.id)
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
                alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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
                        {"cash": -price},
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

        log_ch = await _route_log_channel(self.cog.bot, item_type)
        if log_ch:
            seller_char = selected_item.get("character_name") or "—"
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
                "seller_character": selected_item.get("character_name", ""),
            },
        )

        price_str = f"for **${price:,}**" if price else "for free"
        await interaction.followup.send(
            f"✅ Traded **{item_name}** to **{buyer_character}** ({buyer.display_name}) {price_str}.",
            ephemeral=True,
        )


class GiveSetupView(discord.ui.View):
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

        await interaction.response.send_message(
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
        modal = GiveDetailsModal(self.cog, self.selected_recipient, group, self.selected_recipient_char_name or "")
        await interaction.response.send_modal(modal)
        self.stop()


class GiveDetailsModal(discord.ui.Modal, title="Give — Finalize Details"):
    sender_char_input = discord.ui.TextInput(
        label="Your Character Name",
        placeholder="Character giving the item",
    )

    def __init__(self, cog: PlayerHubCog, recipient: discord.Member, group: dict, receiver_character: str = ""):
        super().__init__()
        self.cog = cog
        self.recipient = recipient
        self.group = group
        self.receiver_character = receiver_character

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return

        target = self.recipient
        sender_char = self.sender_char_input.value.strip().strip('"').strip("'")
        if not sender_char:
            await interaction.followup.send("❌ Your character name is required.", ephemeral=True)
            return

        selected_item = self.group["items"][0]
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
            cw_cog = self.cog.bot.cogs.get("CyberwareShop")
            if cw_cog is None:
                await interaction.followup.send("❌ CyberwareShop cog unavailable. Contact an admin.", ephemeral=True)
                return

            ok_del = await pi_delete_item(item_id)
            if not ok_del:
                await interaction.followup.send("❌ Failed to remove item from your inventory.", ephemeral=True)
                return

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

            log_ch = await _log_channel(self.cog.bot, "CYBERWARE_LOG_CHANNEL_ID")
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
            return

        receiver_char = self.receiver_character
        if not receiver_char:
            await interaction.followup.send(
                "❌ Recipient's character name is required for player-to-player gives.",
                ephemeral=True,
            )
            return

        ok = await pi_update_owner(item_id, str(target.id), receiver_char, str(interaction.user.id))
        if not ok:
            await interaction.followup.send("❌ Failed to transfer item. Please try again.", ephemeral=True)
            return

        log_ch = await _route_log_channel(self.cog.bot, item_type)
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


class SellToStoreSetupView(discord.ui.View):
    def __init__(self, cog: PlayerHubCog, ctx: commands.Context, all_groups: list, seller_chars: list | None = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.all_groups = all_groups
        self.selected_store_owner: Optional[discord.Member] = None
        self.selected_group_idx: Optional[int] = None
        self.selected_seller_char_name: Optional[str] = None
        self.seller_chars = seller_chars

        options = []
        for i, g in enumerate(all_groups[:25]):
            item = g["items"][0]
            char = item.get("character_name", "")
            restriction = item.get("restriction", "basic")
            r_tag = f" [{restriction}]" if restriction != "basic" else ""
            count = g.get("count", 1)
            count_str = f" ×{count}" if count > 1 else ""
            char_str = f" ({char})" if char else ""
            label = f"{g['name']}{count_str}{r_tag}{char_str}"
            options.append(discord.SelectOption(label=label[:100], value=str(i)))

        item_select = discord.ui.Select(
            placeholder="Choose a gun to sell…",
            options=options,
            row=1,
        )
        item_select.callback = self._on_item_select
        self.add_item(item_select)

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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the store owner…", row=0)
    async def owner_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0] if select.values else None
        if user is None:
            await interaction.response.send_message("Please select a server member.", ephemeral=True)
            return
        if isinstance(user, discord.Member):
            member = user
        else:
            guild = self.ctx.guild
            if guild:
                member = guild.get_member(user.id)
                if not member:
                    await interaction.response.send_message(
                        "That user doesn't appear to be in this server.", ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message("Could not resolve server member.", ephemeral=True)
                return
        raw_role = getattr(config, "WHOLESALER_STORE_ROLE_IDS", None)
        if raw_role is None:
            await interaction.response.send_message(
                "❌ Gun store owner role is not configured.", ephemeral=True
            )
            return
        if isinstance(raw_role, (list, tuple, set, frozenset)):
            allowed_ids = {int(r) for r in raw_role}
        elif isinstance(raw_role, str):
            allowed_ids = {int(raw_role)}
        else:
            allowed_ids = {int(raw_role)}
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        if not member_role_ids & allowed_ids:
            await interaction.response.send_message(
                f"❌ **{member.display_name}** is not a gunstore owner.", ephemeral=True
            )
            return
        self.selected_store_owner = member
        await interaction.response.send_message(
            f"Store Owner: **{member.display_name}** ✓", ephemeral=True
        )

    async def _on_seller_char_select(self, interaction: discord.Interaction):
        self.selected_seller_char_name = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Selling character: **{self.selected_seller_char_name}** ✓", ephemeral=True
        )

    async def _on_item_select(self, interaction: discord.Interaction):
        self.selected_group_idx = int(interaction.data["values"][0])
        g = self.all_groups[self.selected_group_idx]
        await interaction.response.send_message(
            f"Gun: **{g['name']}** ✓", ephemeral=True
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_store_owner is None:
            await interaction.response.send_message("Please select a store owner first.", ephemeral=True)
            return
        if self.selected_group_idx is None:
            await interaction.response.send_message("Please select a gun first.", ephemeral=True)
            return
        if self.seller_chars and not self.selected_seller_char_name:
            await interaction.response.send_message("Please select a selling character first.", ephemeral=True)
            return
        if self.selected_store_owner.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot sell guns to yourself.", ephemeral=True
            )
            return
        group = self.all_groups[self.selected_group_idx]
        modal = SellToStoreDetailsModal(
            self.cog, self.selected_store_owner, group,
            seller_character=self.selected_seller_char_name or "",
        )
        await interaction.response.send_modal(modal)
        self.stop()


class StoreBuyConfirmView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.accepted: Optional[bool] = None

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


class SellToStoreDetailsModal(discord.ui.Modal, title="Sell to Store — Finalize"):
    price_input = discord.ui.TextInput(
        label="Asking Price ($)",
        placeholder="e.g. 5000",
    )

    def __init__(self, cog: PlayerHubCog, store_owner: discord.Member, group: dict, *, seller_character: str = ""):
        super().__init__()
        self.cog = cog
        self.store_owner = store_owner
        self.group = group
        self.seller_character = seller_character

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Must be used in server.", ephemeral=True)
            return
        if not self.cog._inv_system_enabled():
            await interaction.followup.send("⚠️ The player inventory system is currently offline.", ephemeral=True)
            return

        store_owner = self.store_owner
        try:
            price = int(self.price_input.value.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Price must be a number.", ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send("❌ Price cannot be negative.", ephemeral=True)
            return
        if store_owner.id == interaction.user.id:
            await interaction.followup.send("❌ You cannot sell guns to yourself.", ephemeral=True)
            return

        selected_item = self.group["items"][0]
        item_name = selected_item["name"]
        item_id = selected_item["item_id"]
        item_type = selected_item.get("item_type", "gun")
        restriction = selected_item.get("restriction", "basic")
        character_name = self.seller_character or selected_item.get("character_name", "")

        live_item = await pi_get_item(item_id)
        if live_item is None or str(live_item.get("owner_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ **{item_name}** is no longer in your inventory.",
                ephemeral=True,
            )
            return

        guns_cog = self.cog.bot.cogs.get("GunsShopCog")
        if not guns_cog:
            await interaction.followup.send("❌ Gun shop system unavailable.", ephemeral=True)
            return

        store_id = guns_cog._store_id(guild.id, store_owner.id)

        price_str = f"**${price:,}**" if price > 0 else "**free**"
        confirm_view = StoreBuyConfirmView(timeout=60)
        try:
            dm_msg = await store_owner.send(
                f"**{interaction.user.display_name}** wants to sell you **{item_name}** "
                f"for {price_str}.\n"
                f"Restriction: **{restriction}**\n"
                "Do you want to buy it for your store?",
                view=confirm_view,
            )
        except discord.Forbidden:
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

        inv_cog = self.cog.bot.cogs.get("PlayerInventory")
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
                alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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

        ok_delete = await pi_delete_item(item_id)
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
                        {"cash": -price},
                        reason=f"Store buy refund (DB failure): {item_name}",
                    )
            await interaction.followup.send(
                "❌ Failed to remove item from your inventory. Refunds attempted. Please contact an admin.",
                ephemeral=True,
            )
            return

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
            alert_ch = await _log_channel(self.cog.bot, "NIGHTCITYBOT_LOG_CHANNEL_ID")
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

        log_ch = await _route_log_channel(self.cog.bot, "gun")
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
