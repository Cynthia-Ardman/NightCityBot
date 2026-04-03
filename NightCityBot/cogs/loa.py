import logging
import discord
from discord.ext import commands
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def get_loa_role(guild: discord.Guild) -> Optional[discord.abc.Snowflake]:
    """Return the LOA role from ``guild`` if it exists."""
    logger.debug(
        "Fetching LOA role %s from guild %s", config.LOA_ROLE_ID, guild
    )
    role = discord.Guild.get_role(guild, config.LOA_ROLE_ID)
    if role is None:
        logger.debug(
            "LOA role %s not found in guild %s", config.LOA_ROLE_ID, guild
        )
    return role


class LOA(commands.Cog):
    """Manage Leave of Absence status."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def get_loa_role(self, guild: discord.Guild) -> Optional[discord.abc.Snowflake]:
        """Wrapper around :func:`get_loa_role` for backwards compatibility."""
        return get_loa_role(guild)

    @commands.command(
        aliases=["startloa", "loa_start", "loastart"]
    )
    async def start_loa(self, ctx, member: Optional[discord.Member] = None):
        """Start a leave of absence. Fixers may specify a member."""
        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('loa'):
            logger.debug("start_loa blocked: system disabled")
            await ctx.send("⚠️ The LOA system is currently disabled.")
            return
        if not ctx.guild:
            await ctx.send("⚠️ This command can only be used in a server.")
            return
        guild = ctx.guild
        logger.debug("start_loa invoked by %s for %s", ctx.author, member or ctx.author)
        loa_role = self.get_loa_role(guild)
        if loa_role is None:
            logger.debug("start_loa aborted: no LOA role")
            await ctx.send("⚠️ LOA role is not configured.")
            return

        target = member or ctx.author
        if member and not any(r.id == config.FIXER_ROLE_ID for r in ctx.author.roles):
            logger.debug("start_loa denied: %s lacks fixer role", ctx.author)
            await ctx.send("❌ Permission denied.")
            return

        # Compare by ID to avoid issues with mocked Role equality
        if any(r.id == loa_role.id for r in target.roles):
            logger.debug("%s already has LOA role", target)
            await ctx.send(f"{target.display_name} is already on LOA.")
            return

        await target.add_roles(loa_role, reason="LOA start")
        logger.debug("LOA role added to %s", target)
        if target == ctx.author:
            await ctx.send("✅ You are now on LOA.")
        else:
            await ctx.send(f"✅ {target.display_name} is now on LOA.")

    @commands.command(
        aliases=["endloa", "loa_end", "loaend"]
    )
    async def end_loa(self, ctx, member: Optional[discord.Member] = None):
        """End a leave of absence. Fixers may specify a member."""
        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('loa'):
            await ctx.send("⚠️ The LOA system is currently disabled.")
            return
        if not ctx.guild:
            await ctx.send("⚠️ This command can only be used in a server.")
            return
        guild = ctx.guild
        logger.debug("end_loa invoked by %s for %s", ctx.author, member or ctx.author)
        loa_role = self.get_loa_role(guild)
        if loa_role is None:
            logger.debug("end_loa aborted: no LOA role")
            await ctx.send("⚠️ LOA role is not configured.")
            return

        target = member or ctx.author
        if member and not any(r.id == config.FIXER_ROLE_ID for r in ctx.author.roles):
            logger.debug("end_loa denied: %s lacks fixer role", ctx.author)
            await ctx.send("❌ Permission denied.")
            return

        # Compare by ID to avoid issues with mocked Role equality
        if not any(r.id == loa_role.id for r in target.roles):
            logger.debug("%s is not on LOA", target)
            await ctx.send(f"{target.display_name} is not currently on LOA.")
            return

        await target.remove_roles(loa_role, reason="LOA end")
        logger.debug("LOA role removed from %s", target)
        if target == ctx.author:
            await ctx.send("✅ Your LOA has ended.")
        else:
            await ctx.send(f"✅ {target.display_name}'s LOA has ended.")
