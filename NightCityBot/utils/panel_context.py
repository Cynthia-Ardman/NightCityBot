import discord


class PanelContext:
    __slots__ = ("author", "guild", "bot", "channel")

    def __init__(self, interaction: discord.Interaction):
        self.author = interaction.user
        self.guild = interaction.guild
        self.bot = interaction.client
        self.channel = interaction.channel

    async def send(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        return await self.channel.send(*args, **kwargs)
