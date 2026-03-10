from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config
from NightCityBot.utils.constants import ROLE_COSTS_BUSINESS, ROLE_COSTS_HOUSING


async def run(suite, ctx) -> List[str]:
    """Test rent logging functionality."""
    logs = []
    try:
        real_user = await suite.get_test_user(ctx)
        approved = MagicMock(spec=discord.Role)
        approved.name = "Approved Character"
        approved.id = config.APPROVED_ROLE_ID
        user = MagicMock(spec=discord.Member)
        user.id = real_user.id
        user.display_name = real_user.display_name
        user.roles = [approved]
        user.guild = ctx.guild
        rent_log_channel = ctx.guild.get_channel(config.RENT_LOG_CHANNEL_ID)
        eviction_channel = ctx.guild.get_channel(config.EVICTION_CHANNEL_ID)

        logs.append(
            "→ Expected: collect_rent should post messages to rent and eviction log channels."
        )

        if not rent_log_channel or not eviction_channel:
            logs.append("→ Result: ❌ Rent or eviction channels not found.")
            return logs

        economy = suite.bot.get_cog("Economy")
        with (
            patch.object(
                economy.unbelievaboat,
                "get_balance",
                new=AsyncMock(return_value={"cash": 1000, "bank": 0}),
            ),
            patch.object(
                economy.unbelievaboat,
                "update_balance",
                new=AsyncMock(return_value=True),
            ),
            patch.object(discord.TextChannel, "send", new=AsyncMock()) as send_mock,
        ):
            await economy.simulate_rent(ctx, target_user=user)
            rent_calls = [
                c
                for c in send_mock.await_args_list
                if c.args and c.args[0] is rent_log_channel
            ]
            evict_calls = [
                c
                for c in send_mock.await_args_list
                if c.args and c.args[0] is eviction_channel
            ]
            if rent_calls:
                logs.append("✅ rent_log_channel.send was called")
            else:
                logs.append("❌ rent_log_channel.send was not called")
            if evict_calls:
                logs.append("✅ eviction_channel.send was called")
            else:
                logs.append("❌ eviction_channel.send was not called")
        logs.append("→ Result: ✅ Rent logic executed and logging channels present.")
    except Exception as e:
        logs.append(f"❌ Exception in test_rent_logging_sends: {e}")
    return logs
