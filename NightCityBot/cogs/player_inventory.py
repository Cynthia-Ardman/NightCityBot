"""Player inventory cog — unified item tracking, trading, and admin management.

Commands
--------
!my_inventory [character_name|@player] [page]
    View your own (or another player's) item inventory.

!inv_give @target <row> "sender_char" ["receiver_char"]
    Give one of your items to another player (no payment).

!trade @buyer <row> <price> buyer_character
    Sell one of your items to a buyer with full payment handling.

!inv_add @player item_type "name" restriction "description" [price]
    Admin: add an item directly to a player's inventory.

!inv_remove @player <row>
    Admin: remove an item from a player's inventory (no payment).

!inv_reassign @player <row> new_character
    Admin: reassign an item to a different character on the same player.
"""

import logging
import uuid
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

ITEMS_PER_PAGE = 15


class PlayerInventoryCog(commands.Cog, name="PlayerInventory"):
    """Unified player inventory — view, trade, admin management."""

    def __init__(self, bot: commands.Bot, unbelievaboat) -> None:
        self.bot = bot
        self.unbelievaboat = unbelievaboat

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _nightcitybot_log_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                logger.warning("Could not fetch NIGHTCITYBOT_LOG_CHANNEL_ID", exc_info=True)
        return ch

    async def _gear_log_channel(self) -> Optional[discord.TextChannel]:
        ch_id = getattr(config, "GEAR_MISC_LOG_CHANNEL_ID", 0)
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                logger.warning("Could not fetch GEAR_MISC_LOG_CHANNEL_ID", exc_info=True)
        return ch

    @staticmethod
    def _format_item_line(i: int, item: dict) -> str:
        name = item.get("name", "?")
        char = item.get("character_name", "")
        itype = item.get("item_type", "")
        restriction = item.get("restriction", "basic")
        price = item.get("price_paid")
        line = f"`{i}.` **{name}**"
        if char:
            line += f" [{char}]"
        badges = []
        if itype:
            badges.append(itype)
        if restriction not in ("basic", "", None):
            badges.append(restriction.upper())
        if badges:
            line += f" _{'/'.join(badges)}_"
        if price:
            line += f" — ${price:,}"
        return line

    # ------------------------------------------------------------------
    # !my_inventory
    # ------------------------------------------------------------------

    @commands.command(name="my_inventory", aliases=["myinv"])
    async def my_inventory(
        self,
        ctx: commands.Context,
        target: Optional[discord.Member] = None,
        page: int = 1,
    ) -> None:
        """View your item inventory (or another player's if admin/fixer).

        Usage:
          !my_inventory [page]
          !my_inventory @player [page]
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

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

        if page < 1:
            page = 1
        total_pages = (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        if page > total_pages:
            page = total_pages

        start = (page - 1) * ITEMS_PER_PAGE
        page_items = items[start: start + ITEMS_PER_PAGE]
        lines = [self._format_item_line(start + i + 1, item) for i, item in enumerate(page_items)]

        whose_title = "Your Inventory" if owner == ctx.author else f"{owner.display_name}'s Inventory"
        embed = discord.Embed(
            title=f"📦 {whose_title} ({page}/{total_pages})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(items)} total item(s) | Use !trade <row> or !inv_give <row>")
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
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if target.id == ctx.author.id:
            await ctx.send("❌ You cannot give an item to yourself.")
            return

        sender_char = sender_char.strip().strip('"').strip("'")
        if not sender_char:
            await ctx.send("❌ Your character name is required.")
            return

        recv_char = (receiver_char or sender_char).strip().strip('"').strip("'")

        items = await pi_get_by_owner(str(ctx.author.id))
        if row < 1 or row > len(items):
            await ctx.send(
                f"❌ Invalid row **{row}**. You have {len(items)} item(s). "
                "Use `!my_inventory` to see the list."
            )
            return

        item = items[row - 1]
        item_name = item["name"]
        item_id = item["item_id"]
        item_char = item.get("character_name", "")
        if item_char and item_char.lower() != sender_char.lower():
            await ctx.send(
                f"❌ Row {row} (`{item_name}`) belongs to character **{item_char}**, "
                f"not **{sender_char}**. Check your row number."
            )
            return

        ok = await pi_update_owner(item_id, str(target.id), recv_char)
        if not ok:
            await ctx.send("❌ Failed to transfer item. Please try again or contact an admin.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🎁 Item Given",
                color=discord.Color.green(),
            )
            embed.add_field(name="From", value=f"{ctx.author.mention} ({sender_char})", inline=True)
            embed.add_field(name="To", value=f"{target.mention} ({recv_char})", inline=True)
            embed.add_field(name="Item", value=item_name, inline=False)
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
        Price 0 is allowed (gift with payment record).
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return
        if buyer.id == ctx.author.id:
            await ctx.send("❌ You cannot trade with yourself.")
            return
        if price < 0:
            await ctx.send("❌ Price cannot be negative.")
            return

        buyer_character = buyer_character.strip().strip('"').strip("'")
        if not buyer_character:
            await ctx.send("❌ Buyer character name is required.")
            return

        items = await pi_get_by_owner(str(ctx.author.id))
        if row < 1 or row > len(items):
            await ctx.send(
                f"❌ Invalid row **{row}**. You have {len(items)} item(s). "
                "Use `!my_inventory` to see the list."
            )
            return

        item = items[row - 1]
        item_name = item["name"]
        item_id = item["item_id"]
        restriction = item.get("restriction", "basic")

        if restriction in ("controlled", "restricted"):
            await ctx.send(
                f"❌ **{item_name}** is **{restriction}** — "
                "player-to-player trading of controlled/restricted items is not allowed. "
                "Contact a Fixer for assistance."
            )
            return

        if price > 0:
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
            if price > 0:
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

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(
                title="💱 Item Traded",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Seller", value=ctx.author.mention, inline=True)
            embed.add_field(name="Buyer", value=f"{buyer.mention} ({buyer_character})", inline=True)
            embed.add_field(name="Item", value=f"**{item_name}** ({restriction})", inline=False)
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
        item_type: str,
        name: str,
        restriction: str,
        description: str,
        price: Optional[int] = None,
    ) -> None:
        """Admin: add an item directly to a player's inventory.

        Usage: !inv_add @player <item_type> "name" <restriction> "description" [price]
        item_type: gun, cyberware, gear, misc
        restriction: basic, controlled, restricted
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        name = name.strip().strip('"').strip("'")
        description = description.strip().strip('"').strip("'")
        restriction = restriction.strip().lower()

        if not name:
            await ctx.send("❌ Item name is required.")
            return

        item_id = str(uuid.uuid4())
        ok = await pi_add_item({
            "item_id": item_id,
            "owner_id": str(player.id),
            "character_name": "",
            "item_type": item_type,
            "name": name,
            "restriction": restriction,
            "description": description,
            "price_paid": price,
            "seller_id": str(ctx.author.id),
            "seller_name": ctx.author.display_name,
        })
        if not ok:
            await ctx.send("❌ Failed to add item to inventory. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🔧 Admin: Item Added to Inventory",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Player", value=player.mention, inline=True)
            embed.add_field(name="Item", value=f"{name} ({item_type}/{restriction})", inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            if price is not None:
                embed.add_field(name="Price", value=f"${price:,}", inline=True)
            embed.add_field(name="Description", value=description or "—", inline=False)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Added **{name}** ({item_type}/{restriction}) to {player.display_name}'s inventory."
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
        row: int,
    ) -> None:
        """Admin: remove an item from a player's inventory.

        Usage: !inv_remove @player <row>
        The row number comes from !my_inventory @player.
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        items = await pi_get_by_owner(str(player.id))
        if row < 1 or row > len(items):
            await ctx.send(
                f"❌ Invalid row **{row}**. {player.display_name} has {len(items)} item(s). "
                "Use `!my_inventory @player` to see the list."
            )
            return

        item = items[row - 1]
        item_id = item["item_id"]
        item_name = item["name"]

        ok = await pi_delete_item(item_id)
        if not ok:
            await ctx.send("❌ Failed to remove item. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Admin: Item Removed from Inventory",
                color=discord.Color.red(),
            )
            embed.add_field(name="Player", value=player.mention, inline=True)
            embed.add_field(name="Item Removed", value=item_name, inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Removed **{item_name}** from {player.display_name}'s inventory."
        )

    # ------------------------------------------------------------------
    # !inv_reassign (admin)
    # ------------------------------------------------------------------

    @commands.command(name="inv_reassign")
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def inv_reassign(
        self,
        ctx: commands.Context,
        player: discord.Member,
        row: int,
        *,
        new_character: str,
    ) -> None:
        """Admin: reassign an item to a different character on the same player.

        Usage: !inv_reassign @player <row> new_character_name
        """
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in the server.")
            return

        new_character = new_character.strip().strip('"').strip("'")
        if not new_character:
            await ctx.send("❌ New character name is required.")
            return

        items = await pi_get_by_owner(str(player.id))
        if row < 1 or row > len(items):
            await ctx.send(
                f"❌ Invalid row **{row}**. {player.display_name} has {len(items)} item(s)."
            )
            return

        item = items[row - 1]
        item_id = item["item_id"]
        item_name = item["name"]
        old_char = item.get("character_name", "")

        ok = await pi_update_character(item_id, new_character)
        if not ok:
            await ctx.send("❌ Failed to reassign item. Please try again.")
            return

        log_ch = await self._gear_log_channel()
        if log_ch:
            embed = discord.Embed(
                title="✏️ Admin: Item Reassigned",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Player", value=player.mention, inline=True)
            embed.add_field(name="Item", value=item_name, inline=True)
            embed.add_field(name="Old Character", value=old_char or "—", inline=True)
            embed.add_field(name="New Character", value=new_character, inline=True)
            embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await ctx.send(
            f"✅ Reassigned **{item_name}** from **{old_char or '(none)'}** "
            f"to **{new_character}** for {player.display_name}."
        )


async def setup(bot: commands.Bot) -> None:
    raise NotImplementedError("PlayerInventoryCog requires unbelievaboat — load via bot.py")
