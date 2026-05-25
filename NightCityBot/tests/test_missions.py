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


class TestComputePayoutTs:
    """compute_payout_ts → next 00:00 America/New_York strictly after start_utc."""

    def test_late_evening_utc_rolls_to_next_et_midnight(self):
        from NightCityBot.cogs.missions import compute_payout_ts

        # 8pm UTC on May 22, 2026 = 4pm ET (EDT) May 22 → next midnight ET = May 23 04:00 UTC.
        start = datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc)
        out = compute_payout_ts(start)
        assert out == datetime(2026, 5, 23, 4, 0, tzinfo=timezone.utc)

    def test_winter_est_rollover(self):
        from NightCityBot.cogs.missions import compute_payout_ts

        # Jan 15, 2026 18:00 UTC = 13:00 EST (UTC-5) → next ET midnight = Jan 16 05:00 UTC.
        start = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
        out = compute_payout_ts(start)
        assert out == datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc)

    def test_naive_datetime_treated_as_utc(self):
        from NightCityBot.cogs.missions import compute_payout_ts

        naive = datetime(2026, 5, 22, 20, 0)  # no tzinfo
        aware = datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc)
        assert compute_payout_ts(naive) == compute_payout_ts(aware)

    def test_after_midnight_et_same_calendar_day(self):
        from NightCityBot.cogs.missions import compute_payout_ts

        # 02:00 UTC May 23 = 22:00 ET May 22 → next ET midnight is May 23 (same calendar UTC day +2hrs)
        start = datetime(2026, 5, 23, 2, 0, tzinfo=timezone.utc)
        out = compute_payout_ts(start)
        assert out == datetime(2026, 5, 23, 4, 0, tzinfo=timezone.utc)


class TestMissionStartParser:
    def test_parses_space_separated(self):
        from NightCityBot.cogs.fixer_hub import _parse_mission_start

        assert _parse_mission_start("2026-05-23 20:00") == datetime(2026, 5, 23, 20, 0, tzinfo=timezone.utc)

    def test_parses_t_separated(self):
        from NightCityBot.cogs.fixer_hub import _parse_mission_start

        assert _parse_mission_start("2026-05-23T20:00") == datetime(2026, 5, 23, 20, 0, tzinfo=timezone.utc)

    def test_rejects_garbage(self):
        from NightCityBot.cogs.fixer_hub import _parse_mission_start

        assert _parse_mission_start("nope") is None
        assert _parse_mission_start("2026/05/23 20:00") is None
        assert _parse_mission_start("") is None

    def test_rejects_invalid_calendar(self):
        from NightCityBot.cogs.fixer_hub import _parse_mission_start

        assert _parse_mission_start("2026-02-30 12:00") is None
        assert _parse_mission_start("2026-05-23 25:00") is None


class TestFormatCheckLineEvents:
    def test_no_row_no_events(self):
        from NightCityBot.cogs.missions import _format_check_line

        out = _format_check_line("alice", None, date(2026, 5, 22))
        assert "no mission record" in out

    def test_row_with_no_events_is_legacy_format(self):
        from NightCityBot.cogs.missions import _format_check_line

        row = {"username": "Alice", "mission_count": 3, "mission_dates": [date(2026, 5, 20)]}
        out = _format_check_line("alice", row, date(2026, 5, 22))
        assert "3 mission(s)" in out
        assert "2 days ago" in out
        # no enrichment section
        assert "by **" not in out

    def test_row_plus_events_enriched(self):
        from NightCityBot.cogs.missions import _format_check_line

        row = {"username": "Alice", "mission_count": 2, "mission_dates": [date(2026, 5, 20)]}
        events = [
            {
                "mission_name": "Heist Alpha",
                "creator_username": "FixerBob",
                "creator_id": "999",
                "start_ts": datetime(2026, 5, 18, 20, 0, tzinfo=timezone.utc),
            },
            {
                "mission_name": "Recon Bravo",
                "creator_username": "",  # forces creator_id fallback
                "creator_id": "1000",
                "start_ts": datetime(2026, 5, 10, 20, 0, tzinfo=timezone.utc),
            },
        ]
        out = _format_check_line("alice", row, date(2026, 5, 22), events)
        assert "Heist Alpha" in out
        assert "FixerBob" in out
        assert "Recon Bravo" in out
        assert "<@1000>" in out  # fallback creator
        assert "2026-05-18" in out

    def test_events_only_no_row(self):
        from NightCityBot.cogs.missions import _format_check_line

        events = [{
            "mission_name": "Solo Job",
            "creator_username": "FixerZ",
            "creator_id": "1",
            "start_ts": datetime(2026, 5, 18, 20, 0, tzinfo=timezone.utc),
        }]
        out = _format_check_line("ghost", None, date(2026, 5, 22), events)
        assert "Solo Job" in out
        assert "FixerZ" in out


class TestPickMissionBanner:
    def test_returns_none_when_no_files(self, tmp_path, monkeypatch):
        from NightCityBot.cogs import fixer_hub

        monkeypatch.setattr(
            fixer_hub, "MISSION_EVENT_IMAGE_CANDIDATES",
            [tmp_path / "missing1.png", tmp_path / "missing2.png"],
        )
        monkeypatch.setattr(fixer_hub, "_LEGACY_BANNER", tmp_path / "legacy.png")
        assert fixer_hub._pick_mission_banner_bytes() is None

    def test_returns_bytes_of_an_existing_file(self, tmp_path, monkeypatch):
        from NightCityBot.cogs import fixer_hub

        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        p1.write_bytes(b"AAA")
        p2.write_bytes(b"BBB")
        monkeypatch.setattr(fixer_hub, "MISSION_EVENT_IMAGE_CANDIDATES", [p1, p2])
        monkeypatch.setattr(fixer_hub, "_LEGACY_BANNER", tmp_path / "legacy.png")
        out = fixer_hub._pick_mission_banner_bytes()
        assert out in (b"AAA", b"BBB")

    def test_skips_oversize_file(self, tmp_path, monkeypatch):
        from NightCityBot.cogs import fixer_hub

        big = tmp_path / "big.png"
        small = tmp_path / "small.png"
        big.write_bytes(b"X" * (8 * 1024 * 1024 + 1))
        small.write_bytes(b"OK")
        monkeypatch.setattr(fixer_hub, "MISSION_EVENT_IMAGE_CANDIDATES", [big, small])
        monkeypatch.setattr(fixer_hub, "_LEGACY_BANNER", tmp_path / "legacy.png")
        # With both available, the small one must be the only acceptable choice
        # if the big one is rolled first. Run a few times to cover shuffles.
        for _ in range(20):
            assert fixer_hub._pick_mission_banner_bytes() == b"OK"


class TestParseIntAmount:
    def test_plain_integer(self):
        from NightCityBot.cogs.fixer_hub import _parse_int_amount

        assert _parse_int_amount("5000") == 5000

    def test_strips_commas_and_currency(self):
        from NightCityBot.cogs.fixer_hub import _parse_int_amount

        assert _parse_int_amount("¥5,000") == 5000
        assert _parse_int_amount("$1,234") == 1234

    def test_rejects_empty_or_garbage(self):
        from NightCityBot.cogs.fixer_hub import _parse_int_amount

        assert _parse_int_amount("") is None
        assert _parse_int_amount("   ") is None
        assert _parse_int_amount("abc") is None


class TestMissionPayoutLoop:
    """End-to-end-ish test for _process_mission_payout."""

    def _build_cog(self, ub_returns=True):
        bot = MagicMock()
        bot.get_guild.return_value = None  # skip channel post for simplicity
        async def _fetch(uid):
            u = MagicMock()
            u.display_name = f"User{uid}"
            u.name = f"User{uid}"
            return u
        bot.fetch_user.side_effect = _fetch
        ub = MagicMock()
        async def _update(uid, amount, reason=""):
            return ub_returns
        ub.update_balance.side_effect = _update
        cog = MissionsCog(bot=bot, unbelievaboat=ub)
        return cog, ub

    def test_processes_payout_and_marks_paid(self):
        cog, ub = self._build_cog(ub_returns=True)
        row = {
            "mission_id": "m1",
            "mission_name": "Test Op",
            "pay_per_player": 5000,
            "attendee_ids": ["111", "222"],
            "guild_id": "9",
            "channel_id": "8",
            "start_ts": datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc),
        }
        claimed = []

        async def fake_claim(mid):
            claimed.append(mid)
            return dict(row)

        async def fake_get(mid):
            return dict(row)

        with patch(
            "NightCityBot.cogs.missions.mission_event_claim_for_payout",
            side_effect=fake_claim,
        ), patch("NightCityBot.cogs.missions.mission_event_get", side_effect=fake_get):
            asyncio.run(cog._process_mission_payout(row))

        assert ub.update_balance.call_count == 2
        # Each call should have bank=5000, cash=0.
        for call in ub.update_balance.call_args_list:
            args, kwargs = call
            assert args[1] == {"cash": 0, "bank": 5000}
        assert claimed == ["m1"]

    def test_claim_returns_none_skips_payout(self):
        """If the row was already paid/canceled in a race, claim returns
        None and we must skip the entire UB loop."""
        cog, ub = self._build_cog(ub_returns=True)
        row = {
            "mission_id": "m3",
            "mission_name": "Already Paid",
            "pay_per_player": 5000,
            "attendee_ids": ["111"],
            "guild_id": "9",
            "channel_id": "8",
            "start_ts": datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc),
        }

        async def fake_claim(mid):
            return None

        async def fake_get(mid):
            return dict(row)

        with patch(
            "NightCityBot.cogs.missions.mission_event_claim_for_payout",
            side_effect=fake_claim,
        ), patch("NightCityBot.cogs.missions.mission_event_get", side_effect=fake_get):
            asyncio.run(cog._process_mission_payout(row))

        ub.update_balance.assert_not_called()

    def test_skips_ub_when_pay_zero(self):
        cog, ub = self._build_cog(ub_returns=True)
        row = {
            "mission_id": "m2",
            "mission_name": "Freebie",
            "pay_per_player": 0,
            "attendee_ids": ["111"],
            "guild_id": "9",
            "channel_id": None,
            "start_ts": datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc),
        }
        async def fake_claim(mid):
            return dict(row)
        async def fake_get(mid):
            return dict(row)
        with patch(
            "NightCityBot.cogs.missions.mission_event_claim_for_payout",
            side_effect=fake_claim,
        ), patch("NightCityBot.cogs.missions.mission_event_get", side_effect=fake_get):
            asyncio.run(cog._process_mission_payout(row))

        ub.update_balance.assert_not_called()


class TestMissionReconcile:
    """Cover the Discord-reconcile branches added for completed-event purges
    and >24h-past reschedules."""

    def _build_cog_with_guild(self, fetch_result):
        bot = MagicMock()
        guild = MagicMock()
        async def _fetch_event(eid):
            if isinstance(fetch_result, Exception):
                raise fetch_result
            return fetch_result
        guild.fetch_scheduled_event = _fetch_event
        bot.get_guild.return_value = guild
        bot.get_channel.return_value = None
        cog = MissionsCog(bot=bot, unbelievaboat=MagicMock())
        return cog

    def test_notfound_past_start_pays_anyway(self):
        """Completed events get purged by Discord; if start_ts is in the
        past, treat NotFound as completion and return the row to be paid."""
        import discord as _d
        cog = self._build_cog_with_guild(
            _d.NotFound(MagicMock(status=404), "gone")
        )
        past = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
        row = {
            "mission_id": "m_past",
            "mission_name": "Done Op",
            "guild_id": "9",
            "event_id": "1234",
            "start_ts": past,
        }
        cancelled = []
        async def fake_cancel(mid):
            cancelled.append(mid)
            return True
        with patch(
            "NightCityBot.cogs.missions.mission_event_cancel",
            side_effect=fake_cancel,
        ):
            result = asyncio.run(cog._reconcile_mission_with_discord(row))
        assert result is not None
        assert result.get("mission_id") == "m_past"
        assert cancelled == []

    def test_notfound_future_start_cancels(self):
        """Fixer deleted the event before it ran → cancel and skip payout."""
        import discord as _d
        cog = self._build_cog_with_guild(
            _d.NotFound(MagicMock(status=404), "gone")
        )
        future = datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc)
        row = {
            "mission_id": "m_future",
            "mission_name": "Upcoming Op",
            "guild_id": "9",
            "channel_id": None,
            "event_id": "1234",
            "start_ts": future,
        }
        cancelled = []
        async def fake_cancel(mid):
            cancelled.append(mid)
            return True
        with patch(
            "NightCityBot.cogs.missions.mission_event_cancel",
            side_effect=fake_cancel,
        ):
            result = asyncio.run(cog._reconcile_mission_with_discord(row))
        assert result is None
        assert cancelled == ["m_future"]

    def test_rewound_more_than_24h_cancels_no_pay(self):
        """If a fixer rewinds an event >24h into the past, refuse to pay
        and mark the row canceled."""
        import discord as _d
        from datetime import timedelta as _td
        ev = MagicMock()
        ev.name = "Actors Needed: Test Op"
        ev.status = _d.EventStatus.scheduled
        ev.start_time = datetime.now(timezone.utc) - _td(hours=48)
        ev.end_time = ev.start_time + _td(hours=4)
        cog = self._build_cog_with_guild(ev)
        row = {
            "mission_id": "m_rewind",
            "mission_name": "Rewound Op",
            "guild_id": "9",
            "channel_id": None,
            "event_id": "1234",
            "start_ts": datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc),
            "end_ts": datetime(2099, 1, 1, 23, 0, tzinfo=timezone.utc),
        }
        cancelled = []
        async def fake_cancel(mid):
            cancelled.append(mid)
            return True
        with patch(
            "NightCityBot.cogs.missions.mission_event_cancel",
            side_effect=fake_cancel,
        ):
            result = asyncio.run(cog._reconcile_mission_with_discord(row))
        assert result is None
        assert cancelled == ["m_rewind"]


class TestCreateMissionScheduleHelpers:
    def test_time_label_formatting(self):
        from NightCityBot.cogs.fixer_hub import _format_time_option_label

        assert _format_time_option_label(0) == "12:00 AM (00:00)"
        assert _format_time_option_label(11) == "11:00 AM (11:00)"
        assert _format_time_option_label(12) == "12:00 PM (12:00)"
        assert _format_time_option_label(13) == "1:00 PM (13:00)"
        assert _format_time_option_label(20) == "8:00 PM (20:00)"
        assert _format_time_option_label(23) == "11:00 PM (23:00)"

    def test_build_date_options_count_and_labels(self):
        from zoneinfo import ZoneInfo
        from NightCityBot.cogs.fixer_hub import _build_date_options

        opts = _build_date_options(ZoneInfo("America/New_York"), count=14)
        assert len(opts) == 14
        assert opts[0].label.startswith("Today")
        assert opts[1].label.startswith("Tomorrow")
        # Values are ISO YYYY-MM-DD and strictly increasing by 1 day.
        from datetime import date as _date
        prev = None
        for opt in opts:
            d = _date.fromisoformat(opt.value)
            if prev is not None:
                assert (d - prev).days == 1
            prev = d

    def test_build_tz_options_default(self):
        from NightCityBot.cogs.fixer_hub import _build_tz_options, MISSION_TZ_OPTIONS

        opts = _build_tz_options("America/Chicago")
        assert len(opts) == len(MISSION_TZ_OPTIONS)
        defaults = [o for o in opts if o.default]
        assert len(defaults) == 1
        assert defaults[0].value == "America/Chicago"

    def test_fixer_tz_pref_roundtrip(self):
        from unittest.mock import AsyncMock, patch
        from NightCityBot.cogs import fixer_hub as fh

        store: dict[str, str] = {}

        async def fake_get(key, default=None):
            return store.get(key, default)

        async def fake_set(key, value, description=""):
            store[key] = value
            return True

        with patch.object(fh, "bot_config_get", side_effect=fake_get), \
             patch.object(fh, "bot_config_set", side_effect=fake_set):
            # First call falls back to default (US Eastern).
            tz = asyncio.run(fh.get_fixer_tz_pref(12345))
            assert tz == "America/New_York"
            # Persist a new pick.
            ok = asyncio.run(fh.set_fixer_tz_pref(12345, "America/Los_Angeles"))
            assert ok is True
            assert store["fixer_tz:12345"] == "America/Los_Angeles"
            # Now it round-trips.
            tz2 = asyncio.run(fh.get_fixer_tz_pref(12345))
            assert tz2 == "America/Los_Angeles"
            # Invalid tz is silently rejected on write, and stale data falls back on read.
            ok_bad = asyncio.run(fh.set_fixer_tz_pref(12345, "Mars/Olympus"))
            assert ok_bad is False
            store["fixer_tz:99"] = "Not/Real"
            assert asyncio.run(fh.get_fixer_tz_pref(99)) == "America/New_York"

    def test_local_to_utc_conversion_via_schedule_view(self):
        """Picking '8 PM US Eastern' on a given date must store the right UTC moment."""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        # 8 PM Eastern on a winter date (EST, UTC-5).
        local = _dt(2026, 1, 15, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        assert local.astimezone(timezone.utc) == _dt(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        # 8 PM Pacific on a summer date (PDT, UTC-7).
        local2 = _dt(2026, 7, 4, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert local2.astimezone(timezone.utc) == _dt(2026, 7, 5, 3, 0, tzinfo=timezone.utc)
