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

    storage = {}

    async def fake_load(*_, **__):
        return storage.get("data", {})

    async def fake_save(_, data):
        storage["data"] = data

    ctx.send = AsyncMock()
    sunday = datetime(2025, 6, 15)
    with (
        patch("NightCityBot.utils.helpers.get_tz_now", return_value=sunday),
        patch("NightCityBot.cogs.economy.load_json_file", new=fake_load),
        patch("NightCityBot.cogs.economy.save_json_file", new=fake_save),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock()),
    ):
        await asyncio.gather(
            economy.open_shop(ctx),
            economy.open_shop(ctx),
        )

    entries = storage.get("data", {}).get(str(mock_author.id), [])
    msgs = [c.args[0] for c in ctx.send.call_args_list]
    if len(entries) == 1 and any("already" in m for m in msgs):
        logs.append("✅ concurrent open_shop calls serialized")
    else:
        logs.append("❌ concurrency issue in open_shop")
    ctx.channel = original_channel
    ctx.author = original_author
    return logs
