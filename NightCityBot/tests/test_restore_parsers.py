"""
Tests for NightCityBot/scripts/restore_from_discord.py parser classes.

Covers the edge cases identified during code review:
  - Interleaved commands from multiple users
  - Failed/non-success bot reply between command and ACK (pair discarded)
  - Unrelated bot messages between command and ACK (pair discarded)
  - Page-boundary cross-page pair
  - State resume from checkpoint (to_state / __init__ round-trip)
"""

import pytest
from datetime import datetime, timezone

# Import parsers directly from the script module
from NightCityBot.scripts.restore_from_discord import (
    AttendanceParser,
    OpenShopParser,
    RentParser,
    CyberwareParser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(uid: str, content: str, ts: str) -> dict:
    return {"author": {"id": uid, "bot": False}, "content": content, "timestamp": ts}


def _bot(content: str, ts: str) -> dict:
    return {"author": {"id": "BOT", "bot": True}, "content": content, "timestamp": ts}


def _ts(n: int) -> str:
    """Return a fake but valid ISO timestamp string ordered by n."""
    return f"2025-01-01T00:{n:02d}:00+00:00"


# ---------------------------------------------------------------------------
# AttendanceParser
# ---------------------------------------------------------------------------

class TestAttendanceParser:
    """All pages supplied in newest-to-oldest (fetch) order."""

    def test_single_clean_pair(self):
        """Simple !attend → Attendance logged pair."""
        page = [
            _bot("Attendance logged for you.", _ts(2)),   # newer
            _user("U1", "!attend", _ts(1)),               # older
        ]
        p = AttendanceParser()
        p.process_page(page)
        results = p.get_results()
        assert len(results) == 1
        uid, ts = results[0]
        assert uid == "U1"

    def test_interleaved_two_users(self):
        """
        Two users attend in sequence; both pairs must be resolved correctly.
        Chronological order: U1!attend → ACK1 → U2!attend → ACK2
        Newest-first order:  ACK2 → U2 → ACK1 → U1
        """
        page = [
            _bot("Attendance logged.", _ts(4)),   # ACK for U2
            _user("U2", "!attend", _ts(3)),
            _bot("Attendance logged.", _ts(2)),   # ACK for U1
            _user("U1", "!attend", _ts(1)),
        ]
        p = AttendanceParser()
        p.process_page(page)
        results = p.get_results()
        assert len(results) == 2
        uids = [r[0] for r in results]
        assert "U1" in uids
        assert "U2" in uids

    def test_unrelated_bot_message_between_command_and_ack(self):
        """
        An unrelated bot message appears between the command and the ACK in
        chronological order.  In reverse scan: ACK → unrelated_bot → command.
        The pair must be discarded (pending cleared by unrelated_bot).
        """
        page = [
            _bot("Attendance logged.", _ts(3)),   # ACK
            _bot("Some unrelated bot message.", _ts(2)),  # intervening
            _user("U1", "!attend", _ts(1)),               # command
        ]
        p = AttendanceParser()
        p.process_page(page)
        assert p.get_results() == []

    def test_user_message_between_command_and_ack(self):
        """
        An unrelated user message appears between command and ACK (chrono).
        In reverse: ACK → unrelated_user → command → pair must be discarded.
        """
        page = [
            _bot("Attendance logged.", _ts(3)),
            _user("U2", "hello", _ts(2)),         # unrelated user message
            _user("U1", "!attend", _ts(1)),
        ]
        p = AttendanceParser()
        p.process_page(page)
        assert p.get_results() == []

    def test_failed_command_no_ack(self):
        """A !attend with no following bot ACK is silently discarded."""
        page = [
            _user("U1", "!attend", _ts(1)),
        ]
        p = AttendanceParser()
        p.process_page(page)
        assert p.get_results() == []

    def test_cross_page_pair(self):
        """
        A pair split across two pages.
        Chrono: U1!attend (older page) → Attendance logged (newer page).
        Page 1 (newer, fetched first): [ACK]
        Page 2 (older, fetched second): [U1!attend]
        State must carry pending_bot_ts from page 1 into page 2.
        """
        page1 = [_bot("Attendance logged.", _ts(2))]
        page2 = [_user("U1", "!attend", _ts(1))]
        p = AttendanceParser()
        p.process_page(page1)
        # Simulate checkpoint round-trip
        p = AttendanceParser(p.to_state())
        p.process_page(page2)
        results = p.get_results()
        assert len(results) == 1
        assert results[0][0] == "U1"

    def test_intervening_message_breaks_cross_page_pair(self):
        """
        An intervening message in page 2 breaks the cross-page pair.
        Page 1: [ACK]
        Page 2: [unrelated_msg, command]  — unrelated appears before command (newest-first)
        """
        page1 = [_bot("Attendance logged.", _ts(3))]
        page2 = [
            _user("U2", "hey", _ts(2)),       # intervening, clears pending
            _user("U1", "!attend", _ts(1)),
        ]
        p = AttendanceParser()
        p.process_page(page1)
        p = AttendanceParser(p.to_state())
        p.process_page(page2)
        assert p.get_results() == []

    def test_state_round_trip(self):
        """to_state / __init__ preserves all fields for checkpoint resume."""
        p = AttendanceParser()
        p.process_page([_bot("Attendance logged.", _ts(5))])
        state = p.to_state()
        assert state["pending_bot_ts"] is not None
        p2 = AttendanceParser(state)
        assert p2._pending_bot_ts == state["pending_bot_ts"]
        assert p2._records == state["records"]


# ---------------------------------------------------------------------------
# OpenShopParser
# ---------------------------------------------------------------------------

class TestOpenShopParser:
    """Mirrors AttendanceParser tests for the open_shop channel."""

    def test_clean_pair_os_alias(self):
        """!os alias works."""
        page = [
            _bot("Business opening logged.", _ts(2)),
            _user("U1", "!os", _ts(1)),
        ]
        p = OpenShopParser()
        p.process_page(page)
        assert len(p.get_results()) == 1

    def test_clean_pair_openshop_alias(self):
        page = [
            _bot("Business opening logged.", _ts(2)),
            _user("U1", "!openshop", _ts(1)),
        ]
        p = OpenShopParser()
        p.process_page(page)
        assert len(p.get_results()) == 1

    def test_intervening_message_discards_pair(self):
        page = [
            _bot("Business opening logged.", _ts(3)),
            _user("U2", "not a command", _ts(2)),
            _user("U1", "!open_shop", _ts(1)),
        ]
        p = OpenShopParser()
        p.process_page(page)
        assert p.get_results() == []

    def test_cross_page_pair(self):
        page1 = [_bot("Business opening logged.", _ts(2))]
        page2 = [_user("U1", "!open_shop", _ts(1))]
        p = OpenShopParser()
        p.process_page(page1)
        p = OpenShopParser(p.to_state())
        p.process_page(page2)
        assert len(p.get_results()) == 1


# ---------------------------------------------------------------------------
# RentParser
# ---------------------------------------------------------------------------

class TestRentParser:
    def test_single_run(self):
        """Two rent payments within 5 minutes form one run."""
        page = [
            _bot("Housing Rent paid for <@111>.", _ts(2)),
            _bot("Housing Rent paid for <@222>.", _ts(1)),
        ]
        p = RentParser()
        p.process_page(page)
        runs, lp, lp_ts = p.get_results()
        assert len(runs) == 1
        assert "111" in lp and "222" in lp

    def test_two_separate_runs(self):
        """Events >5 minutes apart must produce two separate runs."""
        def _ts_m(m: int) -> str:
            return f"2025-01-01T{m:02d}:00:00+00:00"

        page = [
            _bot("Housing Rent paid for <@111>.", _ts_m(10)),
            _bot("Housing Rent paid for <@222>.", _ts_m(0)),
        ]
        p = RentParser()
        p.process_page(page)
        runs, lp, lp_ts = p.get_results()
        assert len(runs) == 2

    def test_last_payment_most_recent(self):
        """last_payment holds the most recent entry per user across pages."""
        def _ts_m(m: int) -> str:
            return f"2025-01-01T{m:02d}:00:00+00:00"

        page1 = [_bot("Housing Rent paid for <@111>.", _ts_m(10))]
        page2 = [_bot("Housing Rent paid for <@111>.", _ts_m(0))]
        p = RentParser()
        p.process_page(page1)
        p.process_page(page2)
        _runs, lp, lp_ts = p.get_results()
        # Most recent is ts_m(10)
        assert lp_ts["111"] == _ts_m(10)


# ---------------------------------------------------------------------------
# CyberwareParser
# ---------------------------------------------------------------------------

class TestCyberwareParser:
    def test_paid_old_format(self):
        page = [_bot("Deducted $50 for cyberware meds from <@111> (week 3)", _ts(1))]
        p = CyberwareParser()
        p.process_page(page)
        runs, status = p.get_results()
        assert len(runs) == 1
        assert runs[0]["paid"][0]["user_id"] == "111"
        assert runs[0]["paid"][0]["weeks"] == 3
        assert status["111"][0] == 3

    def test_unpaid_old_format(self):
        page = [_bot("<@111> cannot pay $50 for immunosuppressants and is at risk.", _ts(1))]
        p = CyberwareParser()
        p.process_page(page)
        runs, status = p.get_results()
        assert len(runs) == 1
        assert "111" in runs[0]["unpaid"]

    def test_unpaid_new_format(self):
        page = [_bot("Could not deduct $50 from <@111> for cyberware meds", _ts(1))]
        p = CyberwareParser()
        p.process_page(page)
        runs, status = p.get_results()
        assert "111" in runs[0]["unpaid"]

    def test_two_runs_separated_by_gap(self):
        def _ts_h(h: int) -> str:
            return f"2025-01-01T{h:02d}:00:00+00:00"

        page = [
            _bot("Deducted $50 from <@111> for cyberware meds", _ts_h(2)),
            _bot("Deducted $50 from <@222> for cyberware meds", _ts_h(0)),
        ]
        p = CyberwareParser()
        p.process_page(page)
        runs, status = p.get_results()
        assert len(runs) == 2


# ---------------------------------------------------------------------------
# Orchestration smoke-tests
# Verify that the attributes referenced in main() logging actually exist,
# so runtime AttributeErrors are caught here before the CLI is invoked.
# ---------------------------------------------------------------------------

class TestOrchestrationAttributes:
    """
    Smoke-tests ensuring the parser attributes referenced by main()'s logging
    statements are present and of the expected types.  These catch regressions
    where a parser refactor breaks the main() log lines.
    """

    def _run_attendance(self, page):
        p = AttendanceParser()
        p.process_page(page)
        return p

    def _run_open_shop(self, page):
        p = OpenShopParser()
        p.process_page(page)
        return p

    def test_attendance_parser_has_records_attr(self):
        """main() logs `len(attendance_parser._records)` — must not AttributeError."""
        page = [
            _bot("Attendance logged.", _ts(2)),
            _user("111", "!attend", _ts(1)),
        ]
        p = self._run_attendance(page)
        # Must be subscriptable and len-able (list)
        assert isinstance(p._records, list)
        _ = len(p._records)   # must not raise

    def test_open_shop_parser_has_records_attr(self):
        """main() logs `len(open_shop_parser._records)` — must not AttributeError."""
        page = [
            _bot("Business opening logged.", _ts(2)),
            _user("111", "!open_shop", _ts(1)),
        ]
        p = self._run_open_shop(page)
        assert isinstance(p._records, list)
        _ = len(p._records)

    def test_attendance_parser_no_bot_acks_attr(self):
        """The old _bot_acks attribute must no longer exist (guard against revert)."""
        p = AttendanceParser()
        assert not hasattr(p, "_bot_acks"), (
            "_bot_acks must not exist; main() must use _records"
        )

    def test_attendance_parser_no_cmds_attr(self):
        """The old _cmds attribute must no longer exist (guard against revert)."""
        p = AttendanceParser()
        assert not hasattr(p, "_cmds"), (
            "_cmds must not exist; main() must use _records"
        )

    def test_open_shop_parser_no_bot_acks_attr(self):
        p = OpenShopParser()
        assert not hasattr(p, "_bot_acks")

    def test_open_shop_parser_no_cmds_attr(self):
        p = OpenShopParser()
        assert not hasattr(p, "_cmds")

    def test_attendance_full_pipeline(self):
        """
        Simulate two pages of fetch then get_results() — same flow as main().
        Verify no crash and correct record count.
        """
        page1 = [
            _bot("Attendance logged.", _ts(4)),
            _user("222", "!attend", _ts(3)),
        ]
        page2 = [
            _bot("Attendance logged.", _ts(2)),
            _user("111", "!attend", _ts(1)),
        ]
        p = AttendanceParser()
        p.process_page(page1)
        # Simulate checkpoint round-trip (as done in run_channel_section)
        p = AttendanceParser(p.to_state())
        p.process_page(page2)
        # main() logging line
        count = len(p._records)     # must not raise
        assert count == 2
        results = p.get_results()   # must not raise
        assert len(results) == 2
