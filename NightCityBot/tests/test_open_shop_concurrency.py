from typing import List
import asyncio
import discord
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
import config

async def run(suite, ctx) -> List[str]:
    logs = []
    control = suite.bot.get_cog('SystemControl')
    if control:
        await control.set_status('open_shop', True)
    economy = suite.bot.get_cog('Economy')
    original_channel = ctx.channel
    original_author = ctx.author
    ctx.channel = ctx.guild.get_channel(config.BUSINESS_ACTIVITY_CHANNEL_ID)
    if not ctx.channel:
        ctx.channel = MagicMock(id=config.BUSINESS_ACTIVITY_CHANNEL_ID)

    biz_role = MagicMock(spec=discord.Role)
    biz_role.name = "Business Tier 1"
    biz_role.id = 9999
    mock_author = MagicMock(spec=discord.Member)
    mock_author.id = original_author.id
    mock_author.display_name = original_author.display_name
    mock_author.roles = [biz_role]
    ctx.author = mock_author

    opened = set()
    add_count = 0

    async def fake_exists_today(uid):
        return uid in opened

    async def fake_count_month(uid, year, month):
        return len(opened)

    async def fake_add(uid, ts):
        nonlocal add_count
        opened.add(uid)
        add_count += 1
        return True

    ctx.send = AsyncMock()
    sunday = datetime(2025, 6, 15)
    with (
        patch("NightCityBot.utils.helpers.get_tz_now", return_value=sunday),
        patch("NightCityBot.cogs.economy.open_log_exists_today", new=fake_exists_today),
        patch("NightCityBot.cogs.economy.open_log_count_month", new=fake_count_month),
        patch("NightCityBot.cogs.economy.open_log_add", new=fake_add),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock()),
    ):
        await asyncio.gather(
            economy.open_shop(ctx),
            economy.open_shop(ctx),
        )

    msgs = [c.args[0] for c in ctx.send.call_args_list]
    if add_count == 1 and any("already" in m for m in msgs):
        logs.append("✅ concurrent open_shop calls serialized")
    else:
        logs.append(f"❌ concurrency issue in open_shop (add_count={add_count})")
    ctx.channel = original_channel
    ctx.author = original_author
    return logs
