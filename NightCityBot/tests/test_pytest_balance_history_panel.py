"""Tests for the admin Balance History panel helpers."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from NightCityBot.cogs.admin_shop import (
    _friendly_backup_label,
    _load_backup_history,
    _merge_history,
    _format_history_lines,
    _fit_lines_to_description,
)
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI


class TestFriendlyBackupLabel:
    def test_drops_before_anchor_entries(self):
        assert _friendly_backup_label("collect_housing_before") is None
        assert _friendly_backup_label("trauma_before") is None

    def test_maps_known_after_labels(self):
        assert _friendly_backup_label("collect_housing_after") == "Housing Rent collected"
        assert _friendly_backup_label("collect_business_after") == "Business Rent collected"
        assert _friendly_backup_label("collect_cyberware_after") == "Cyberware Rent collected"
        assert _friendly_backup_label("trauma_after") == "Trauma Team service"

    def test_manual_prefix(self):
        assert _friendly_backup_label("manual_20260101_120000.json") == "Admin manual backup/restore"

    def test_unknown_label_passthrough(self):
        assert _friendly_backup_label("custom_thing") == "custom_thing"

    def test_empty(self):
        assert _friendly_backup_label("") is None


class TestMergeHistory:
    def _row(self, ts, cash=0, bank=0, reason="r"):
        return {"id": None, "ts": ts, "cash_delta": cash, "bank_delta": bank, "reason": reason}

    def test_merges_and_sorts_desc(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now - timedelta(hours=1), cash=-1000, reason="Cyberware Rent (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2
        assert merged[0]["ts"] >= merged[1]["ts"]

    def test_drops_backup_dupe_within_window(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(seconds=30), cash=-500, reason="Housing Rent collected (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 1
        assert merged[0]["reason"] == "Housing Rent"

    def test_keeps_backup_when_amounts_differ(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(seconds=30), cash=-200, reason="Other event (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2

    def test_keeps_backup_when_outside_window(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(minutes=10), cash=-500, reason="Different rent (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2


class TestFormatHistoryLines:
    def test_renders_cash_and_bank_deltas(self):
        rows = [{
            "ts": datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
            "cash_delta": -500,
            "bank_delta": 200,
            "reason": "Some thing",
        }]
        lines, omitted = _format_history_lines(rows)
        assert omitted == 0
        assert len(lines) == 1
        assert "$-500 cash" in lines[0]
        assert "+$200 bank" in lines[0]
        assert "Some thing" in lines[0]

    def test_truncates_long_reasons(self):
        rows = [{
            "ts": datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
            "cash_delta": -1,
            "bank_delta": 0,
            "reason": "x" * 500,
        }]
        lines, _ = _format_history_lines(rows)
        assert lines[0].count("x") <= 140

    def test_caps_max_rows(self):
        ts = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        rows = [{"ts": ts, "cash_delta": -1, "bank_delta": 0, "reason": str(i)} for i in range(75)]
        lines, omitted = _format_history_lines(rows, max_rows=50)
        assert len(lines) == 50
        assert omitted == 25


class TestFitLines:
    def test_fits_within_cap(self):
        lines = ["short line"] * 5
        desc, dropped = _fit_lines_to_description(lines, cap=200)
        assert dropped == 0
        assert desc.count("\n") == 4

    def test_drops_overflow(self):
        lines = ["x" * 100 for _ in range(50)]
        desc, dropped = _fit_lines_to_description(lines, cap=300)
        assert dropped > 0
        assert len(desc) <= 300


def test_update_balance_records_history_on_success():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    with patch(
        "NightCityBot.utils.db.balance_history_record",
        new=AsyncMock(return_value=True),
    ) as mock_record:
        asyncio.run(api._record_history(
            12345, {"cash": -500, "bank": 0}, "Housing Rent"
        ))
        mock_record.assert_awaited_once_with("12345", -500, 0, "Housing Rent")


def test_record_history_swallows_db_failure():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    with patch(
        "NightCityBot.utils.db.balance_history_record",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        # Must not raise
        asyncio.run(api._record_history(1, {"cash": 0}, "x"))


def test_update_balance_invokes_record_history_after_200_patch():
    """End-to-end: a successful PATCH must trigger _record_history."""
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    api.api_token = "t"
    api.base_url = "https://example.invalid/g"
    api.headers = {"Authorization": "t"}
    api._semaphore = asyncio.Semaphore(1)
    api._record_history = AsyncMock()

    class _Resp:
        status = 200
        async def text(self):
            return ""
        async def json(self):
            return {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    api.session = MagicMock()
    api.session.patch = MagicMock(return_value=_Resp())

    ok = asyncio.run(api.update_balance(42, {"cash": -250, "bank": 0}, reason="Housing Rent"))
    assert ok is True
    api._record_history.assert_awaited_once_with(42, {"cash": -250, "bank": 0}, "Housing Rent")


def test_update_balance_does_not_record_when_patch_fails():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    api.api_token = "t"
    api.base_url = "https://example.invalid/g"
    api.headers = {"Authorization": "t"}
    api._semaphore = asyncio.Semaphore(1)
    api._record_history = AsyncMock()

    class _Resp:
        status = 500
        async def text(self):
            return "boom"
        async def json(self):
            return {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    api.session = MagicMock()
    api.session.patch = MagicMock(return_value=_Resp())

    with patch("asyncio.sleep", new=AsyncMock()):
        ok = asyncio.run(api.update_balance(7, {"cash": 1}, reason="x"))
    assert ok is False
    api._record_history.assert_not_called()


def test_load_backup_history_skips_malformed_change(tmp_path, monkeypatch):
    """A single bad ``change`` value must not nuke the entire backup file."""
    import config
    monkeypatch.setattr(config, "BALANCE_BACKUP_DIR", str(tmp_path))
    user_id = 999
    now = datetime.now(timezone.utc)
    entries = [
        {  # bad: change is non-numeric
            "timestamp": now.isoformat(),
            "label": "collect_housing_after",
            "change": "not-a-number",
        },
        {  # good
            "timestamp": now.isoformat(),
            "label": "trauma_after",
            "change": -1500,
        },
    ]
    (tmp_path / f"balance_backup_{user_id}.json").write_text(json.dumps(entries))
    rows = asyncio.run(_load_backup_history(user_id, now - timedelta(days=30)))
    assert len(rows) == 1
    assert rows[0]["cash_delta"] == -1500
    assert "Trauma Team service" in rows[0]["reason"]
