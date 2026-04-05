import asyncio
from typing import Optional

import discord

from NightCityBot.utils.interaction_safety import SafeView, send_ephemeral, respond_ephemeral


async def collect_text_input(bot, channel_id: int, author_id: int, *, timeout: int = 60) -> Optional[str]:
    def check(m: discord.Message) -> bool:
        return m.author.id == author_id and m.channel.id == channel_id

    try:
        msg = await bot.wait_for('message', check=check, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    text = msg.content.strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if text.lower() == 'cancel':
        return None
    return text


class QtySelectView(SafeView):
    def __init__(self, author_id: int, max_qty: int = 10):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.result: Optional[int] = None
        cap = min(max_qty, 25)
        if cap < 1:
            cap = 1
        options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(1, cap + 1)]
        select = discord.ui.Select(placeholder="Choose quantity…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def _on_select(self, interaction: discord.Interaction):
        self.result = int(interaction.data["values"][0])
        await interaction.response.edit_message(content=f"Quantity: **{self.result}** ✓", view=None)
        self.stop()


class PriceSelectView(SafeView):
    def __init__(self, author_id: int, bot, channel_id: int, *, allow_zero: bool = True):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.bot = bot
        self.channel_id = channel_id
        self.result: Optional[int] = None

        prices = []
        if allow_zero:
            prices.append(("Free ($0)", "0"))
        for p in [500, 1000, 2500, 5000, 10000, 25000, 50000]:
            prices.append((f"${p:,}", str(p)))
        prices.append(("Custom amount…", "custom"))

        options = [discord.SelectOption(label=lbl, value=val) for lbl, val in prices]
        select = discord.ui.Select(placeholder="Choose a price…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def _on_select(self, interaction: discord.Interaction):
        val = interaction.data["values"][0]
        if val == "custom":
            await respond_ephemeral(interaction,
                "📝 Type the price amount (number only), or `cancel` to abort:"
            )
            text = await collect_text_input(self.bot, self.channel_id, self.author_id)
            if text is None:
                self.stop()
                return
            try:
                self.result = int(text.replace(",", "").replace("$", ""))
            except ValueError:
                await send_ephemeral(interaction, "❌ Invalid price.")
                self.stop()
                return
        else:
            self.result = int(val)
            await interaction.response.edit_message(
                content=f"Price: **${self.result:,}** ✓", view=None
            )
        self.stop()
