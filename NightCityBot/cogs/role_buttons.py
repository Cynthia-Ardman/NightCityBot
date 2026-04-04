"""UI buttons for self-assignable roles."""

import discord
from discord.ext import commands

import config
from NightCityBot.utils.interaction_safety import SafeView
from NightCityBot.utils.permissions import is_fixer


class NPCButtonView(SafeView):
    """View providing a button to assign the NPC role."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Get NPC Role",
        style=discord.ButtonStyle.primary,
        custom_id="npc_role_button",
    )
    async def assign_npc(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Grant the NPC role to the interacting member."""
        guild = interaction.guild or self.bot.get_guild(config.GUILD_ID)
        if not guild:
            await interaction.response.send_message(
                "⚠️ Guild not found.", ephemeral=True
            )
            return

        role = guild.get_role(config.NPC_ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "⚠️ NPC role is not configured.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except discord.NotFound:
                    member = None

        if member is None:
            await interaction.response.send_message(
                "⚠️ Could not find your member record.", ephemeral=True
            )
            return

        if any(r.id == role.id for r in getattr(member, "roles", [])):
            await interaction.response.send_message(
                "✅ You already have the NPC role.", ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="NPC role button")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.response.send_message(
                f"❌ Could not assign NPC role: {e}", ephemeral=True
            )
            return
        admin = self.bot.get_cog("Admin")
        if admin:
            await admin.log_audit(
                member, "✅ Self-assigned NPC role via button."
            )
        await interaction.response.send_message(
            "✅ NPC role granted.", ephemeral=True
        )


class RoleButtons(commands.Cog):
    """Cog registering buttons for self-assignable roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.view = NPCButtonView(bot)
        # Register the persistent view so button callbacks work after restarts.
        bot.add_view(self.view)

    @commands.command()
    @is_fixer()
    async def npc_button(self, ctx: commands.Context) -> None:
        """Send the NPC role assignment button in the current channel."""
        await ctx.send(
            "Click the button below to receive the NPC role.", view=self.view
        )
