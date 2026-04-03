"""Player inventory cog — unified item tracking helpers.

The legacy prefix commands (!my_inventory, !inv_give, !trade, !inv_add,
!inv_remove, !inv_reassign) have been removed.  All player-facing inventory
actions are now handled through the Player Hub (!player).

This cog is still loaded so that hub code can access the helper methods
(grouping, display building, channel routing, system-enabled check) via
``bot.cogs.get("PlayerInventory")``.
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
from NightCityBot.utils.player_inventory import (
    insert_player_item as pi_add_item,
    query_player_inventory as pi_get_by_owner,
    get_player_item as pi_get_item,
    delete_player_item as pi_delete_item,
    transfer_player_item as pi_update_owner,
    reassign_player_item as pi_update_character,
)
from NightCityBot.utils.db import pt_create, ih_record_event
from NightCityBot.utils.characters import ensure_character_active, get_character_by_name
from NightCityBot.utils.permissions import is_fixer

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 15


from NightCityBot.cogs.player_hub import TradeConfirmView  # noqa: F401 — re-export for backward compat


class PlayerInventoryCog(commands.Cog, name="PlayerInventory"):
    """Unified player inventory — helper methods for hub views."""

    def __init__(self, bot: commands.Bot, unbelievaboat) -> None:
        self.bot = bot
        self.unbelievaboat = unbelievaboat

    async def _get_channel(self, attr: str) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, attr, 0)
        if not ch_id:
            return None
        ch = self.bot.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(ch_id))
            except Exception:
                logger.warning("Could not fetch channel %s=%s", attr, ch_id, exc_info=True)
        return ch

    async def _nightcitybot_log_channel(self) -> Optional[discord.TextChannel]:
        return await self._get_channel("NIGHTCITYBOT_LOG_CHANNEL_ID")

    async def _gear_log_channel(self) -> Optional[discord.TextChannel]:
        return await self._get_channel("GEAR_MISC_LOG_CHANNEL_ID")

    async def _gun_log_channel(self) -> Optional[discord.TextChannel]:
        return await self._get_channel("GUN_LOG_CHANNEL_ID")

    async def _cyberware_log_channel(self) -> Optional[discord.TextChannel]:
        return await self._get_channel("CYBERWARE_LOG_CHANNEL_ID")

    async def _route_log_channel(self, item_type: str) -> Optional[discord.TextChannel]:
        if item_type == "gun":
            return await self._gun_log_channel()
        if item_type == "cyberware":
            return await self._cyberware_log_channel()
        return await self._gear_log_channel()

    @staticmethod
    def _group_items(items: list[dict]) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for item in items:
            name = item.get("name", "?")
            itype = item.get("item_type", "misc")
            price = item.get("price_paid")
            seller = item.get("seller_name", "")
            raw_date = item.get("acquired_at") or item.get("created_at") or ""
            date_str = str(raw_date)[:10]
            key = (name, itype, price, seller, date_str)
            if key not in groups:
                groups[key] = {
                    "name": name,
                    "item_type": itype,
                    "price_paid": price,
                    "seller_name": seller,
                    "acquired_date": date_str,
                    "items": [],
                }
            groups[key]["items"].append(item)
        for g in groups.values():
            g["items"].sort(
                key=lambda i: (
                    i.get("acquired_at") is None,
                    str(i.get("acquired_at") or i.get("created_at") or ""),
                )
            )
            g["count"] = len(g["items"])
        return sorted(groups.values(), key=lambda g: (g["name"], g["acquired_date"]))

    def _inv_system_enabled(self) -> bool:
        control = self.bot.get_cog("SystemControl")
        if control and not control.is_enabled("player_inventory"):
            return False
        return True

    VALID_RESTRICTIONS = ("basic", "controlled", "restricted")

    @staticmethod
    def _build_display(items: list[dict], char_filter: Optional[str] = None):
        char_order: list[str] = []
        char_groups: dict[str, list[dict]] = {}
        for item in items:
            char = item.get("character_name") or ""
            if char not in char_groups:
                char_order.append(char)
                char_groups[char] = []
            char_groups[char].append(item)

        char_filter_lower = char_filter.lower() if char_filter else None

        display = []
        row_num = 1
        all_groups: list[dict] = []
        for char in char_order:
            visible = (char_filter_lower is None) or (char.lower() == char_filter_lower)
            groups = PlayerInventoryCog._group_items(char_groups[char])
            if visible:
                display.append((None, f"\n**— {char or '(no character)'} —**"))
            for g in groups:
                count_str = f" ×{g['count']}" if g["count"] > 1 else ""
                type_tag = g["item_type"]
                meta_parts = []
                if g["price_paid"]:
                    meta_parts.append(f"💰 ${g['price_paid']:,}")
                if g["seller_name"]:
                    meta_parts.append(f"🏪 {g['seller_name']}")
                date_str = g.get("acquired_date") or ""
                if date_str:
                    meta_parts.append(f"📅 {date_str}")
                meta_line = " **·** ".join(meta_parts)
                line = f"`{row_num}.` **{g['name']}**{count_str} — `{type_tag}`"
                if meta_line:
                    line += f"\n> {meta_line}"
                if visible:
                    display.append((row_num, line))
                    all_groups.append(g)
                row_num += 1
        return display, all_groups


async def setup(bot: commands.Bot) -> None:
    raise NotImplementedError("PlayerInventoryCog requires unbelievaboat — load via bot.py")
