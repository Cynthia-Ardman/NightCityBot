"""Player inventory cog — unified item tracking, trading, and admin management.

Commands
--------
!my_inventory [character_name|@player] [page]
    View your own (or another player's) item inventory, grouped by character.

!inv_give @target <row> "sender_char" ["receiver_char"]
    Give one of your items (no payment). If cyberware and target is a
    Ripperdoc, the item goes into their CW stock instead.

!trade @buyer <row> <price> buyer_character
    Sell one of your items with full payment handling. Self-trade (price=0)
    is allowed and is how players move items between their own characters.

!inv_add @player "name" <qty> "character_name" [item_type=misc] [description=] [price=]
    Admin: add qty items (each with a unique UUID) to a player's inventory.

!inv_remove @player <item_id>
    Admin: remove a specific item by UUID.

!inv_reassign <item_id> @player "character_name"
    Admin: reassign an item to a different character.
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from NightCityBot.utils.db import (
    pi_add_item,
    pi_get_by_owner,
    pi_get_item,
    pi_delete_item,
    pi_update_owner,
    pi_update_character,
    pt_create,
)
from NightCityBot.utils.permissions import is_fixer

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 15


class PlayerInventoryCog(commands.Cog, name="PlayerInventory"):
    """Unified player inventory — view, trade, admin management."""

    def __init__(self, bot: commands.Bot, unbelievaboat) -> None:
        self.bot = bot
        self.unbelievaboat = unbelievaboat

    # ------------------------------------------------------------------
    # Channel helpers
    # ------------------------------------------------------------------

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
        """Return the appropriate audit channel based on item type."""
        if item_type == "gun":
            return await self._gun_log_channel()
        if item_type == "cyberware":
            return await self._cyberware_log_channel()
        return await self._gear_log_channel()

    # ------------------------------------------------------------------
    # Grouping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_items(items: list[dict]) -> list[dict]:
        """Group a flat item list by (name, item_type, price_paid, seller_name).

        All copies of the same item from the same seller at the same price are
        collapsed into one group regardless of acquisition date. Items within
        each group are sorted FIFO by acquired_at so the oldest is used first.

        Returns a list of group dicts sorted alphabetically by name:
          {name, item_type, price_paid, seller_name, count, items (FIFO)}
        """
        groups: dict[tuple, dict] = {}
        for item in items:
            name = item.get("name", "?")
            itype = item.get("item_type", "misc")
            price = item.get("price_paid")
            seller = item.get("seller_name", "")
            key = (name, itype, price, seller)
            if key not in groups:
                groups[key] = {
                    "name": name,
                    "item_type": itype,
                    "price_paid": price,
                    "seller_name": seller,
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
        return sorted(groups.values(), key=lambda g: g["name"])

    @staticmethod
    def _build_display(items: list[dict], char_filter: Optional[str] = None):
        """Build the display structure for !my_inventory.

        Returns a list of (row_number_or_None, line) tuples where row_number
        is None for character headers and an int for item group rows.
        Flat row numbers are consecutive across all characters.
        """
        char_order: list[str] = []
        char_groups: dict[str, list[dict]] = {}
        for item in items:
            char = item.get("character_name") or ""
            if char not in char_groups:
                char_order.append(char)
                char_groups[char] = []
            char_groups[char].append(item)

        # Filter by character if requested
        if char_filter:
            char_filter_lower = char_filter.lower()
            char_order = [c for c in char_order if c.lower() == char_filter_lower]

        display = []
        row_num = 1
        all_groups: list[dict] = []
        for char in char_order:
            display.append((None, f"— **{char or '(no character)'}** —"))
            groups = PlayerInventoryCog._group_items(char_groups[char])
            for g in groups:
                price_str = f"${g['price_paid']:,}" if g["price_paid"] else "—"
                seller_str = g["seller_name"] or "—"
                earliest = g["items"][0] if g["items"] else {}
                raw_date = earliest.get("acquired_at") or earliest.get("created_at")
                date_str = str(raw_date)[:10] if raw_date else "—"
                count_str = f" ×{g['count']}" if g["count"] > 1 else ""
                line = (
                    f"`{row_num}.` **{g['name']}**{count_str}"
                    f" | {g['item_type']} | {price_str} | {seller_str} | {date_str}"
                )
                display.append((row_num, line))
                all_groups.append(g)
                row_num += 1
        return display, all_groups

    # ------------------------------------------------------------------
    # !my_inventory
    # ------------------------------------------------------------------

    @commands.command(name="my_inventory", aliases=["myinv"])
    async def my_inventory(self, ctx: commands.Context, *, query: str = "") -> None:
        """View your item inventory, grouped by character.

        Usage:
          !my_inventory
          !my_inventory "character_name"
          !my_inventory @player
          !my_inventory @player 2
          !my_inventory "V" 2
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        target: Optional[discord.Member] = None
        char_filter: Optional[str] = None
        page: int = 1

        tokens = query.strip().split()
        if tokens:
            # Try last token as page number
            if tokens[-1].isdigit():
                page = int(tokens[-1])
                tokens = tokens[:-1]

            if tokens:
                remainder = " ".join(tokens)
                mention_match = re.match(r"<@!?(\d+)>", remainder.strip())
                if mention_match:
                    member_id = int(mention_match.group(1))
                    target = ctx.guild.get_member(member_id)
                    if target is None:
                        try:
                            target = await ctx.guild.fetch_member(member_id)
                        except Exception:
                            await ctx.send("❌ Could not find that member.")
                            return
                else:
                    char_filter = remainder.strip().strip('"').strip("'")

        if target and target != ctx.author:
            author_roles = getattr(ctx.author, "roles", [])
            is_privileged = (
                any(r.id == config.FIXER_ROLE_ID for r in author_roles)
                or (isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator)
            )
            if not is_privileged:
                await ctx.send("❌ Only Fixers or admins can view another player's inventory.")
                return

        owner = target or ctx.author
        items = await pi_get_by_owner(str(owner.id))

        if not items:
            whose = "Your" if owner == ctx.author else f"{owner.display_name}'s"
            await ctx.send(f"📦 {whose} inventory is empty.")
            return

        display_lines, all_groups = self._build_display(items, char_filter)

        if not display_lines:
            char_label = f" for character **{char_filter}**" if char_filter else ""
            await ctx.send(f"📦 No items found{char_label}.")
            return

        # Paginate: count only item-group lines (not headers)
        item_lines = [(rn, ln) for rn, ln in display_lines if rn is not None]
        total_groups = len(item_lines)
        if page < 1:
            page = 1
        total_pages = max(1, (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
        if page > total_pages:
            page = total_pages

        # Collect which row numbers are on this page
        start_group = (page - 1) * GROUPS_PER_PAGE + 1
        end_group = page * GROUPS_PER_PAGE
        page_rows = {rn for rn, _ in item_lines if start_group <= rn <= end_group}

        # Rebuild display lines for this page (include headers if they have items on this page)
        page_lines: list[str] = []
        last_was_header = False
        pending_header = None
        for rn, ln in display_lines:
            if rn is None:
                pending_header = ln
                last_was_header = True
            else:
                if rn in page_rows:
                    if pending_header is not None:
                        page_lines.append(pending_header)
                        pending_header = None
                    page_lines.append(ln)
                    last_was_header = False

        whose_title = "Your Inventory" if owner == ctx.author else f"{owner.display_name}'s Inventory"
        if char_filter:
            whose_title += f" — {char_filter}"

        embed = discord.Embed(
            title=f"📦 {whose_title} ({page}/{total_pages})",
            description="\n".join(page_lines) if page_lines else "No items.",
            color=discord.Color.blue(),
        )
        hint = f"Use `!my_inventory page {page + 1}`" if page < total_pages else ""
        embed.set_footer(
            text=f"{len(items)} total item(s) | Use !trade <row> or !inv_give <row>"
            + (f" | {hint}" if hint else "")
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !inv_give
    # ------------------------------------------------------------------

    @commands.command(name="inv_give")
    async def inv_give(
        self,
        ctx: commands.Context,
        target: discord.Member,
        row: int,
        sender_char: str,
        receiver_char: Optional[str] = None,
    ) -> None:
        """Give one of your items to another player (no payment).

        Usage: !inv_give @target <row> "sender_char" ["receiver_char"]
        The row number comes from !my_inventory.

        If the item is cyberware and target is a Ripperdoc, the item goes into
        the ripperdoc's CW stock (receiver_char may be omitted in that case).
        Self-give (same Discord user, different characters) is allowed.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        sender_char = sender_char.strip().strip('"').strip("'")
        if not sender_char:
            await ctx.send("❌ Your character name is required.")
            return

        items = await pi_get_by_owner(str(ctx.author.id))
        _, all_groups = self._build_display(items)

        if row < 1 or row > len(all_groups):
            await ctx.send(
                f"❌ Invalid row **{row}**. You have {len(all_groups)} item group(s). "
                "Use `!my_inventory` to see the list."
            )
            return

        group = all_groups[row - 1]
        selected_item = group["items"][0]
        item_name = selected_item["name"]
        item_id = selected_item["item_id"]
        item_type = selected_item.get("item_type", "misc")
        item_char = selected_item.get("character_name", "")
        if item_char and item_char.lower() != sender_char.lower():
            await ctx.send(
                f"❌ Row {row} (`{item_name}`) belongs to character **{item_char}**, "
                f"not **{sender_char}**. Check your row number."
            )
            return

        # Check if target is a ripperdoc and item is cyberware → route to CW stock
        target_roles = getattr(target, "roles", [])
        is_ripperdoc_target = any(
            getattr(r, "id", None) == getattr(config, "RIPPERDOC_ROLE_ID", None)
            for r in target_roles
        )

        if item_type == "cyberware" and is_ripperdoc_target:
            # Transfer cyberware into ripperdoc's CW stock file
            cw_cog = self.bot.cogs.get("CyberwareShop")
            if cw_cog is None:
                await ctx.send("❌ CyberwareShop cog unavailable. Contact an admin.")
                return

            ok_del = await pi_delete_item(item_id)
            if not ok_del:
                await ctx.send("❌ Failed to remove item from your inventory. Please try again.")
                return

            rd_inventory = await cw_cog._load_inventory(target.id)
            rd_inventory.append({
                "item_id": item_id,
                "name": item_name,
                "price_paid": selected_item.get("price_paid"),
                "purchased_at": selected_item.get("acquired_at") or selected_item.get("created_at"),
            })
            await cw_cog._save_inventory(target.id, rd_inventory)

            log_ch = await self._cyberware_log_channel()
            if log_ch:
                embed = discord.Embed(title="💉 Cyberware Returned to Ripperdoc Stock", color=discord.Color.teal())
                embed.add_field(name="From", value=f"{ctx.author.mention} ({sender_char})", inline=True)
                embed.add_field(name="Ripperdoc", value=target.mention, inline=True)
                embed.add_field(name="Item", value=item_name, inline=False)
                await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

            await ctx.send(
                f"✅ **{item_name}** transferred from **{sender_char}** to "
                f"{target.display_name}'s ripperdoc stock."
            )
            return

        # Regular player-to-player give (self-give allowed)
        recv_char = (receiver_char or sender_char).strip().strip('"').strip("'")
        if not recv_char:
            await ctx.send("❌ Receiver character name is required.")
            return

        ok = await pi_update_owner(item_id, str(target.id), recv_char)
        if not ok:
            await ctx.send("❌ Failed to transfer item. Please try again or contact an admin.")
            return

        log_ch = await self._route_log_channel(item_type)
        if log_ch:
            embed = discord.Embed(title="🎁 Item Given", color=discord.Color.green())
            embed.add_field(name="From", value=f"{ctx.author.mention} ({sender_char})", inline=True)
            embed.add_field(name="To", value=f"{target.mention} ({recv_char})", inline=True)
            embed.add_field(name="Item", value=f"**{item_name}** ({item_type})", inline=False)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Transferred **{item_name}** from **{sender_char}** to "
            f"**{recv_char}** ({target.display_name})."
        )

    # ------------------------------------------------------------------
    # !trade
    # ------------------------------------------------------------------

    @commands.command(name="trade")
    async def trade(
        self,
        ctx: commands.Context,
        buyer: discord.Member,
        row: int,
        price: int,
        *,
        buyer_character: str,
    ) -> None:
        """Sell one of your items to a buyer with full payment.

        Usage: !trade @buyer <row> <price> buyer_character_name
        The row number comes from !my_inventory.
        Controlled/restricted items cannot be traded via this command.
        Price 0 is allowed — use it to move items between your own characters.
        Self-trade (!trade @yourself <row> 0 "other_char") is explicitly allowed.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if price < 0:
            await ctx.send("❌ Price cannot be negative.")
            return

        buyer_character = buyer_character.strip().strip('"').strip("'")
        if not buyer_character:
            await ctx.send("❌ Buyer character name is required.")
            return

        items = await pi_get_by_owner(str(ctx.author.id))
        _, all_groups = self._build_display(items)

        if row < 1 or row > len(all_groups):
            await ctx.send(
                f"❌ Invalid row **{row}**. You have {len(all_groups)} item group(s). "
                "Use `!my_inventory` to see the list."
            )
            return

        group = all_groups[row - 1]
        selected_item = group["items"][0]
        item_name = selected_item["name"]
        item_id = selected_item["item_id"]
        item_type = selected_item.get("item_type", "misc")
        restriction = selected_item.get("restriction", "basic")

        if restriction in ("controlled", "restricted"):
            await ctx.send(
                f"❌ **{item_name}** is **{restriction}** — "
                "player-to-player trading of controlled/restricted items is not allowed. "
                "Contact a Fixer for assistance."
            )
            return

        b_cash_deduct = 0
        b_bank_deduct = 0

        if price > 0 and buyer.id != ctx.author.id:
            buyer_balance = await self.unbelievaboat.get_balance(buyer.id)
            if buyer_balance is None:
                await ctx.send("❌ Could not fetch buyer's balance. Please try again.")
                return

            b_cash = int(buyer_balance.get("cash", 0))
            b_bank = int(buyer_balance.get("bank", 0))
            if b_cash + b_bank < price:
                await ctx.send(
                    f"❌ {buyer.display_name} cannot afford **${price:,}** "
                    f"(they have **${b_cash + b_bank:,}**)."
                )
                return

            b_cash_deduct = min(max(b_cash, 0), price)
            b_bank_deduct = max(0, price - b_cash_deduct)

            ok_buyer = await self.unbelievaboat.update_balance(
                buyer.id,
                {"cash": -b_cash_deduct, "bank": -b_bank_deduct},
                reason=f"Trade purchase: {item_name} from {ctx.author.display_name}",
            )
            if not ok_buyer:
                await ctx.send("❌ Failed to deduct from buyer's balance. Aborting.")
                return

            ok_seller = await self.unbelievaboat.update_balance(
                ctx.author.id,
                {"cash": price},
                reason=f"Trade sale: {item_name} to {buyer.display_name}",
            )
            if not ok_seller:
                logger.error(
                    "trade: buyer debited but seller credit failed — "
                    "recording pending transfer for seller=%s buyer=%s item=%s",
                    ctx.author.id, buyer.id, item_id,
                )
                pt_id = str(uuid.uuid4())
                await pt_create({
                    "transfer_id": pt_id,
                    "from_id": str(buyer.id),
                    "to_id": str(ctx.author.id),
                    "item_id": item_id,
                    "amount": price,
                    "status": "pending",
                    "error_detail": "seller credit failed after buyer debit",
                })
                alert_ch = await self._nightcitybot_log_channel()
                if alert_ch:
                    await alert_ch.send(
                        f"🚨 **PENDING TRADE** — seller credit failed!\n"
                        f"Transfer ID: `{pt_id}`\n"
                        f"Seller: {ctx.author.mention} | Buyer: {buyer.mention}\n"
                        f"Item: **{item_name}** | Amount: **${price:,}**\n"
                        "Buyer has been debited. Please resolve manually.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                await ctx.send(
                    "⚠️ Buyer was charged but seller payout failed. "
                    "This has been flagged for admin review. "
                    "Item ownership has NOT been transferred yet."
                )
                return

        # Transfer ownership in DB
        ok_transfer = await pi_update_owner(item_id, str(buyer.id), buyer_character)
        if not ok_transfer:
            if price > 0 and buyer.id != ctx.author.id:
                await self.unbelievaboat.update_balance(
                    buyer.id,
                    {"cash": b_cash_deduct, "bank": b_bank_deduct},
                    reason=f"Trade refund (DB failure): {item_name}",
                )
                await self.unbelievaboat.update_balance(
                    ctx.author.id,
                    {"cash": -price},
                    reason=f"Trade refund (DB failure): {item_name}",
                )
            await ctx.send(
                "❌ Failed to transfer item ownership in database. "
                "All payments have been refunded. Please try again."
            )
            return

        log_ch = await self._route_log_channel(item_type)
        if log_ch:
            embed = discord.Embed(title="💱 Item Traded", color=discord.Color.gold())
            embed.add_field(name="Seller", value=ctx.author.mention, inline=True)
            embed.add_field(name="Buyer", value=f"{buyer.mention} ({buyer_character})", inline=True)
            embed.add_field(name="Item", value=f"**{item_name}** ({item_type}/{restriction})", inline=False)
            embed.add_field(name="Price", value=f"${price:,}" if price else "Free", inline=True)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        price_str = f"for **${price:,}**" if price else "for free"
        await ctx.send(
            f"✅ Traded **{item_name}** to **{buyer_character}** ({buyer.display_name}) {price_str}."
        )

    # ------------------------------------------------------------------
    # !inv_add (admin)
    # ------------------------------------------------------------------

    @commands.command(name="inv_add")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def inv_add(
        self,
        ctx: commands.Context,
        player: discord.Member,
        name: str,
        qty: int,
        character_name: str,
        item_type: str = "misc",
        description: str = "",
        price: Optional[int] = None,
    ) -> None:
        """Admin: add qty items to a player's inventory.

        Usage: !inv_add @player "name" <qty> "character_name" [item_type=misc] ["description"] [price]
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        name = name.strip().strip('"').strip("'")
        character_name = character_name.strip().strip('"').strip("'")
        description = description.strip().strip('"').strip("'")
        item_type = item_type.strip().lower()

        if not name:
            await ctx.send("❌ Item name is required.")
            return
        if qty < 1:
            await ctx.send("❌ qty must be at least 1.")
            return
        if not character_name:
            await ctx.send("❌ Character name is required.")
            return

        now = datetime.now(timezone.utc).isoformat()
        added = 0
        for _ in range(qty):
            ok = await pi_add_item({
                "item_id": str(uuid.uuid4()),
                "owner_id": str(player.id),
                "character_name": character_name,
                "item_type": item_type,
                "name": name,
                "restriction": "basic",
                "description": description,
                "price_paid": price,
                "seller_id": str(ctx.author.id),
                "seller_name": ctx.author.display_name,
                "acquired_at": now,
            })
            if ok:
                added += 1

        if added == 0:
            await ctx.send("❌ Failed to add item to inventory. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(title="🔧 Admin: Item(s) Added to Inventory", color=discord.Color.orange())
            embed.add_field(name="Player", value=f"{player.mention} ({character_name})", inline=True)
            embed.add_field(name="Item", value=f"{name} ({item_type})", inline=True)
            embed.add_field(name="Qty Added", value=str(added), inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            if price is not None:
                embed.add_field(name="Price", value=f"${price:,}", inline=True)
            if description:
                embed.add_field(name="Description", value=description, inline=False)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        partial = f" (only {added} of {qty})" if added < qty else ""
        await ctx.send(
            f"✅ Added **{name}** × {added}{partial} ({item_type}) to "
            f"**{character_name}** ({player.display_name}'s) inventory."
        )

    # ------------------------------------------------------------------
    # !inv_remove (admin)
    # ------------------------------------------------------------------

    @commands.command(name="inv_remove")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def inv_remove(
        self,
        ctx: commands.Context,
        player: discord.Member,
        item_id: str,
    ) -> None:
        """Admin: remove a specific item from a player's inventory by UUID.

        Usage: !inv_remove @player <item_id>
        The item_id comes from !my_inventory (shown on the item detail).
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        item_id = item_id.strip()
        item = await pi_get_item(item_id)
        if item is None:
            await ctx.send(f"❌ Item `{item_id}` not found.")
            return
        if item.get("owner_id") != str(player.id):
            await ctx.send(
                f"❌ Item `{item_id}` does not belong to {player.display_name}."
            )
            return

        item_name = item.get("name", "?")
        ok = await pi_delete_item(item_id)
        if not ok:
            await ctx.send("❌ Failed to remove item. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(title="🗑️ Admin: Item Removed from Inventory", color=discord.Color.red())
            embed.add_field(name="Player", value=player.mention, inline=True)
            embed.add_field(name="Item Removed", value=item_name, inline=True)
            embed.add_field(name="Item ID", value=f"`{item_id}`", inline=False)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Removed **{item_name}** (`{item_id}`) from {player.display_name}'s inventory."
        )

    # ------------------------------------------------------------------
    # !inv_reassign (admin)
    # ------------------------------------------------------------------

    @commands.command(name="inv_reassign")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def inv_reassign(
        self,
        ctx: commands.Context,
        item_id: str,
        player: discord.Member,
        *,
        new_character: str,
    ) -> None:
        """Admin: reassign an item to a different character.

        Usage: !inv_reassign <item_id> @player "character_name"
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        new_character = new_character.strip().strip('"').strip("'")
        if not new_character:
            await ctx.send("❌ New character name is required.")
            return

        item_id = item_id.strip()
        item = await pi_get_item(item_id)
        if item is None:
            await ctx.send(f"❌ Item `{item_id}` not found.")
            return
        if item.get("owner_id") != str(player.id):
            await ctx.send(
                f"❌ Item `{item_id}` does not belong to {player.display_name}."
            )
            return

        item_name = item.get("name", "?")
        old_char = item.get("character_name", "")

        ok = await pi_update_character(item_id, new_character)
        if not ok:
            await ctx.send("❌ Failed to reassign item. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(title="✏️ Admin: Item Reassigned", color=discord.Color.blurple())
            embed.add_field(name="Player", value=player.mention, inline=True)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Old Character", value=old_char or "—", inline=True)
            embed.add_field(name="New Character", value=new_character, inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Reassigned **{item_name}** (`{item_id}`) from **{old_char or '(none)'}** "
            f"to **{new_character}** for {player.display_name}."
        )


async def setup(bot: commands.Bot) -> None:
    raise NotImplementedError("PlayerInventoryCog requires unbelievaboat — load via bot.py")
