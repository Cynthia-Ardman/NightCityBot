import discord
from discord.ext import commands
from NightCityBot.utils import config_loader as _cfg
import config


class TraumaTeam(commands.Cog):
    """Commands related to Trauma Team assistance."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(aliases=["calltrauma", "trauma"])
    async def call_trauma(self, ctx: commands.Context) -> None:
        """Ping the Trauma Team role with the user's plan."""
        if not ctx.guild:
            await ctx.send("⚠️ This command can only be used in a server.")
            return
        trauma_channel = ctx.guild.get_channel(config.TRAUMA_NOTIFICATIONS_CHANNEL_ID)
        if not isinstance(trauma_channel, discord.TextChannel):
            await ctx.send("⚠️ Trauma Team channel not found.")
            return

        plan_role = next((r for r in ctx.author.roles if r.name in _cfg.get_trauma_role_costs()), None)
        if not plan_role:
            await ctx.send("⚠️ You don't have a Trauma Team plan role.")
            return

        mention = f"<@&{config.TRAUMA_TEAM_ROLE_ID}>"
        message = f"{mention} <@{ctx.author.id}> with **{plan_role.name}** is in need of assistance."

        await trauma_channel.send(message)

        await ctx.send("🚑 Trauma Team notified.")
