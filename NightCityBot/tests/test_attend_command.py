from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config
from NightCityBot.utils.constants import ROLE_COSTS_BUSINESS, ROLE_COSTS_HOUSING

async def run(suite, ctx) -> List[str]:
    """Test the attend reward command and its restrictions."""
    control = suite.bot.get_cog('SystemControl')
    if control:
        await control.set_status('attend', True)
    logs = []
    economy = suite.bot.get_cog('Economy')
    original_author = ctx.author
    mock_author = MagicMock(spec=discord.Member)
    mock_author.id = original_author.id
    mock_author.roles = [discord.Object(id=config.VERIFIED_ROLE_ID)]
    ctx.author = mock_author
    ctx.send = AsyncMock()
    original_channel = ctx.channel

    tz = ZoneInfo(getattr(config, "TIMEZONE", "America/Los_Angeles"))

    # Wrong channel should be rejected
    ctx.channel = MagicMock(id=9999)
    sunday_local = datetime(2025, 6, 15, 15, 30, tzinfo=tz)
    sunday_utc = sunday_local.astimezone(ZoneInfo("UTC"))
    with patch("NightCityBot.utils.helpers.get_tz_now", return_value=sunday_utc):
        await economy.attend(ctx)
        msg = ctx.send.await_args[0][0]
        if "Please use" in msg:
            logs.append("✅ attend rejected in wrong channel")
        else:
            logs.append("❌ attend allowed in wrong channel")
    ctx.send.reset_mock()
    ctx.channel = original_channel
    ctx.channel.id = config.ATTENDANCE_CHANNEL_ID

    # Non-Sunday should be rejected
    monday_local = datetime(2025, 6, 16, 15, 0, tzinfo=tz)
    monday_utc = monday_local.astimezone(ZoneInfo("UTC"))
    with (
        patch("NightCityBot.utils.helpers.get_tz_now", return_value=monday_utc),
        patch("NightCityBot.cogs.economy.attendance_get_user", new=AsyncMock(return_value=[])),
        patch("NightCityBot.cogs.economy.attendance_append", new=AsyncMock(return_value=True)),
    ):
        await economy.attend(ctx)
        msg = ctx.send.await_args[0][0]
        if "only allowed during Sunday events" in msg:
            logs.append("✅ attend rejected on non-Sunday")
        else:
            logs.append("❌ attend did not reject non-Sunday")
    ctx.send.reset_mock()

    # Already attended this event should be rejected
    sunday_event_local = datetime(2025, 6, 15, 16, 0, tzinfo=tz)
    sunday_event_utc = sunday_event_local.astimezone(ZoneInfo("UTC"))
    event_start_local = sunday_event_local.replace(hour=14, minute=0, second=0, microsecond=0)
    prev_attend = (event_start_local + timedelta(minutes=30)).astimezone(ZoneInfo("UTC"))
    with (
        patch("NightCityBot.utils.helpers.get_tz_now", return_value=sunday_event_utc),
        patch(
            "NightCityBot.cogs.economy.attendance_get_user",
            new=AsyncMock(return_value=[prev_attend.isoformat()]),
        ),
        patch("NightCityBot.cogs.economy.attendance_append", new=AsyncMock(return_value=True)),
    ):
        await economy.attend(ctx)
        msg = ctx.send.await_args[0][0]
        if "already logged attendance for this event" in msg:
            logs.append("✅ attend rejected when used twice in same event")
        else:
            logs.append("❌ attend did not enforce event limit")
    ctx.send.reset_mock()

    # Success on new event when last log was previous week
    prev_week = (sunday_event_local - timedelta(days=7)).astimezone(ZoneInfo("UTC"))
    with (
        patch("NightCityBot.utils.helpers.get_tz_now", return_value=sunday_event_utc),
        patch(
            "NightCityBot.cogs.economy.attendance_get_user",
            new=AsyncMock(return_value=[prev_week.isoformat()]),
        ),
        patch("NightCityBot.cogs.economy.attendance_append", new=AsyncMock(return_value=True)),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock()),
    ):
        await economy.attend(ctx)
        msg = ctx.send.await_args[0][0]
        if "Attendance logged" in msg:
            logs.append("✅ attend succeeded after cooldown")
        else:
            logs.append("❌ attend did not succeed after cooldown")

    ctx.author = original_author
    return logs
