"""Tests for the missions cog (parsing, helpers, and command flow)."""
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openpyxl import Workbook

from NightCityBot.cogs.missions import (
    MissionsCog,
    _format_check_line,
    _parse_date_token,
    _parse_sheet_dates,
    _resolve_target,
    parse_mission_sheet,
)


class TestParseDateToken:
    def test_iso(self):
        assert _parse_date_token("2026-05-09") == date(2026, 5, 9)

    def test_slash_two_digit_year(self):
        assert _parse_date_token("5/9/26") == date(2026, 5, 9)

    def test_slash_four_digit_year(self):
        assert _parse_date_token("05/09/2026") == date(2026, 5, 9)

    def test_garbage(self):
        assert _parse_date_token("not a date") is None
        assert _parse_date_token("") is None
        assert _parse_date_token("13/40/2026") is None


class TestParseSheetDates:
    def test_empty(self):
        assert _parse_sheet_dates(None) == []
        assert _parse_sheet_dates("") == []

    def test_comma_separated(self):
        out = _parse_sheet_dates("1/2/26, 3/4/26 , 5/6/2026")
        assert out == [date(2026, 1, 2), date(2026, 3, 4), date(2026, 5, 6)]

    def test_native_date_cell(self):
        assert _parse_sheet_dates(date(2026, 5, 9)) == [date(2026, 5, 9)]
        assert _parse_sheet_dates(datetime(2026, 5, 9, 12, 0)) == [date(2026, 5, 9)]

    def test_skips_garbage(self):
        out = _parse_sheet_dates("1/2/26, bogus, 5/6/2026")
        assert out == [date(2026, 1, 2), date(2026, 5, 6)]


class TestParseMissionSheet:
    def test_round_trip(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.append(["Discord ID", "Username", "Count", "Dates"])  # header
        ws.append(["123456789012345678", "Alice", 3, "1/2/26, 3/4/26, 5/6/26"])
        ws.append(["234567890123456789", "Bob", 1, "5/1/2026"])
        ws.append(["", "stray", "", ""])  # skip
        ws.append(["999999999999999999", "Carol", None, ""])  # count=0, no dates
        p = tmp_path / "roster.xlsx"
        wb.save(p)

        rows = parse_mission_sheet(p)
        assert len(rows) == 3
        alice = rows[0]
        assert alice["user_id"] == "123456789012345678"
        assert alice["username"] == "Alice"
        assert alice["mission_count"] == 3
        assert alice["mission_dates"] == [date(2026, 1, 2), date(2026, 3, 4), date(2026, 5, 6)]

        carol = rows[2]
        assert carol["mission_count"] == 0
        assert carol["mission_dates"] == []

    def test_infers_count_from_dates_when_blank(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.append(["345678901234567890", "Dave", "", "1/1/26, 2/2/26"])
        p = tmp_path / "roster2.xlsx"
        wb.save(p)
        rows = parse_mission_sheet(p)
        assert rows[0]["mission_count"] == 2


class TestFormatCheckLine:
    def test_no_record(self):
        line = _format_check_line("Alice", None, date(2026, 5, 9))
        assert "no mission record" in line

    def test_today(self):
        row = {"username": "Alice", "mission_count": 4, "mission_dates": [date(2026, 5, 9)]}
        line = _format_check_line("Alice", row, date(2026, 5, 9))
        assert "today" in line
        assert "4 mission(s)" in line

    def test_days_ago(self):
        row = {"username": "Bob", "mission_count": 2, "mission_dates": [date(2026, 4, 25), date(2026, 5, 1)]}
        line = _format_check_line("Bob", row, date(2026, 5, 9))
        # Most recent is May 1, 8 days ago
        assert "8 days ago" in line
        assert "2 mission(s)" in line

    def test_dates_missing(self):
        row = {"username": "X", "mission_count": 5, "mission_dates": []}
        line = _format_check_line("X", row, date(2026, 5, 9))
        assert "no dates on file" in line


class TestResolveTarget:
    def _ctx(self, members=None):
        ctx = MagicMock()
        guild = MagicMock()
        guild.members = members or []
        guild.get_member = MagicMock(return_value=None)
        ctx.guild = guild
        ctx.bot = MagicMock()
        ctx.bot.fetch_user = AsyncMock(side_effect=Exception("nope"))
        return ctx

    def test_resolves_mention(self):
        member = MagicMock(id=111111111111111111, display_name="Alice", name="alice")
        ctx = self._ctx()
        ctx.guild.get_member = MagicMock(return_value=member)
        uid, uname, obj = asyncio.run(_resolve_target(ctx, "<@111111111111111111>"))
        assert uid == "111111111111111111"
        assert uname == "Alice"
        assert obj is member

    def test_resolves_raw_id(self):
        ctx = self._ctx()
        uid, uname, obj = asyncio.run(_resolve_target(ctx, "222222222222222222"))
        assert uid == "222222222222222222"
        assert obj is None  # fetch_user raised — id-only fallback
        assert uname == "222222222222222222"

    def test_resolves_username_from_guild(self):
        member = MagicMock(id=333333333333333333, name="bob", display_name="Bob", global_name="bob")
        ctx = self._ctx(members=[member])
        uid, uname, obj = asyncio.run(_resolve_target(ctx, "Bob"))
        assert uid == "333333333333333333"
        assert uname == "Bob"
        assert obj is member

    def test_unknown_token_falls_through(self):
        ctx = self._ctx()
        uid, uname, obj = asyncio.run(_resolve_target(ctx, "nobody-here"))
        assert uid == "nobody-here"
        assert uname == "nobody-here"
        assert obj is None


class TestMissionCheckCommand:
    def test_check_no_args(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = MagicMock()
        ctx.reply = AsyncMock()
        asyncio.run(cog.mission_check.callback(cog, ctx))
        ctx.reply.assert_awaited()
        msg = ctx.reply.await_args.args[0]
        assert "Usage" in msg

    def test_check_reports_each_user(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = MagicMock()
        guild = MagicMock()
        member = MagicMock(id=444444444444444444, display_name="Alice", name="alice")
        guild.get_member = MagicMock(return_value=member)
        guild.members = [member]
        ctx.guild = guild
        ctx.bot = MagicMock()
        ctx.reply = AsyncMock()

        async def fake_get(uid):
            return {
                "username": "Alice",
                "mission_count": 3,
                "mission_dates": [date(2026, 5, 1)],
            }

        with patch("NightCityBot.cogs.missions.mission_log_get", side_effect=fake_get):
            asyncio.run(cog.mission_check.callback(cog, ctx, "<@444444444444444444>"))

        ctx.reply.assert_awaited()
        msg = ctx.reply.await_args.args[0]
        assert "Alice" in msg
        assert "3 mission(s)" in msg


class TestMissionRecordCommand:
    def _ctx(self):
        ctx = MagicMock()
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=None)
        guild.members = []
        ctx.guild = guild
        ctx.bot = MagicMock()
        ctx.bot.fetch_user = AsyncMock(side_effect=Exception("nope"))
        ctx.reply = AsyncMock()
        return ctx

    def test_no_args(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = self._ctx()
        asyncio.run(cog.mission_record.callback(cog, ctx))
        ctx.reply.assert_awaited()
        assert "Usage" in ctx.reply.await_args.args[0]

    def test_records_with_default_date(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = self._ctx()
        captured = {}

        async def fake_record(uid, uname, mdate):
            captured["uid"] = uid
            captured["uname"] = uname
            captured["mdate"] = mdate
            return {"username": uname, "mission_count": 1, "mission_dates": [mdate]}

        today = datetime.now(timezone.utc).date()
        with patch("NightCityBot.cogs.missions.mission_log_record", side_effect=fake_record):
            asyncio.run(cog.mission_record.callback(cog, ctx, "555555555555555555"))

        assert captured["uid"] == "555555555555555555"
        assert captured["mdate"] == today
        msg = ctx.reply.await_args.args[0]
        assert "today" in msg
        assert "total 1 mission(s)" in msg

    def test_records_with_explicit_date(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = self._ctx()
        captured = {}

        async def fake_record(uid, uname, mdate):
            captured["mdate"] = mdate
            return {"username": uname, "mission_count": 7, "mission_dates": [mdate]}

        with patch("NightCityBot.cogs.missions.mission_log_record", side_effect=fake_record):
            asyncio.run(
                cog.mission_record.callback(cog, ctx, "555555555555555555", "date=2026-04-01")
            )

        assert captured["mdate"] == date(2026, 4, 1)
        msg = ctx.reply.await_args.args[0]
        assert "2026-04-01" in msg
        assert "today" not in msg

    def test_rejects_bad_date(self):
        cog = MissionsCog(bot=MagicMock())
        ctx = self._ctx()
        asyncio.run(
            cog.mission_record.callback(cog, ctx, "555555555555555555", "date=nonsense")
        )
        msg = ctx.reply.await_args.args[0]
        assert "parse" in msg.lower()
