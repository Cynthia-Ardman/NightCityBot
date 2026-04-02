import logging
import random
import re

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

MAX_DICE = 100
MAX_SIDES = 1_000_000
MAX_MOD = 1_000_000


class RollSystem(commands.Cog):
    """Cog providing dice rolling utilities."""

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the roll system."""
        self.bot = bot

    @commands.command()
    async def roll(self, ctx, *, dice: str):
        original_sender = getattr(ctx, "original_author", None)
        skip_log = getattr(ctx, "skip_dm_log", False)

        # Extract user mention if present
        match = re.search(r"(?:<@!?)?([0-9]{15,20})>?", dice)
        mentioned_user = None
        if match:
            user_id = int(match.group(1))
            mentioned_user = ctx.guild.get_member(user_id)
            if mentioned_user:
                dice = re.sub(r"(?:<@!?)?[0-9]{17,20}(?:>)?", "", dice).strip()

        roller = mentioned_user or ctx.author

        if original_sender:
            try:
                await ctx.message.delete()
                admin = self.bot.get_cog("Admin")
                if admin:
                    await admin.log_audit(
                        ctx.author, f"🗑️ Deleted message: {ctx.message.content}"
                    )
            except Exception as e:
                logger.warning("Couldn't delete relayed !roll command: %s", e)
            await self.loggable_roll(
                roller,
                ctx.channel,
                dice,
                original_sender=original_sender,
                log_user=ctx.author,
                skip_log=skip_log,
            )
        else:
            await self.loggable_roll(
                roller,
                ctx.channel,
                dice,
                log_user=ctx.author,
                skip_log=skip_log,
            )

    async def loggable_roll(
        self,
        author,
        channel,
        dice: str,
        *,
        original_sender=None,
        log_user=None,
        skip_log=False,
    ):
        pattern = r"(?:(\d*)d)?(\d+)([+-]\d+)?"
        m = re.fullmatch(pattern, dice.replace(" ", ""))
        if not m:
            await channel.send(
                "🎲 Invalid format. Use: `!roll XdY+Z` (e.g. `!roll 2d6+3`)"
            )
            return

        n_dice, sides, mod = m.groups()
        n_dice = int(n_dice) if n_dice else 1
        sides = int(sides)
        mod = int(mod) if mod else 0

        if n_dice > MAX_DICE:
            await channel.send(f"❌ Too many dice (max {MAX_DICE}).")
            return
        if sides > MAX_SIDES:
            await channel.send(f"❌ Too many sides (max {MAX_SIDES:,}).")
            return
        if abs(mod) > MAX_MOD:
            await channel.send(f"❌ Modifier too large (max ±{MAX_MOD:,}).")
            return

        rolls = [random.randint(1, sides) for _ in range(n_dice)]
        total = sum(rolls) + mod

        name = author.display_name
        header = f'🎲 {name} rolled: {n_dice}d{sides}{f"+{mod}" if mod else ""}\n'
        body = f'**Results:** {", ".join(map(str, rolls))}\n**Total:** {total}'
        result = header + body

        await channel.send(result)

        log_target = log_user or author

        if skip_log:
            return
        dm_handler = self.bot.get_cog("DMHandler")
        if isinstance(channel, discord.DMChannel) and not original_sender:
            if dm_handler:
                thread = await dm_handler.get_or_create_dm_thread(log_target)
                if isinstance(thread, discord.abc.Messageable):
                    await thread.send(
                        f"📥 **{log_target.display_name} used:** `!roll {dice}`\n\n{result}"
                    )
        elif original_sender:
            if dm_handler:
                thread = await dm_handler.get_or_create_dm_thread(log_target)
                if isinstance(thread, discord.abc.Messageable):
                    await thread.send(
                        f"📤 **{original_sender.display_name} rolled as {author.display_name}** → `!roll {dice}`\n\n{result}"
                    )
