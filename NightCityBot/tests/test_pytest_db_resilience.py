"""Tests for db.py retry/resilience helpers (Task #3).

These tests run fully offline — no real DB connection required.
"""
import asyncio
import pytest

import asyncpg


# ---------------------------------------------------------------------------
# Helper: run a coroutine synchronously in tests
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _with_retry tests
# ---------------------------------------------------------------------------

from NightCityBot.utils import db as _db


def test_with_retry_success_first_attempt():
    """Coroutine that succeeds immediately is called once and result returned."""
    calls = []

    async def coro():
        calls.append(1)
        return 42

    result = _run(_db._with_retry(coro, label="test"))
    assert result == 42
    assert calls == [1]


def test_with_retry_retries_on_transient_error():
    """Transient errors cause retries up to the configured count."""
    calls = []

    async def coro():
        calls.append(1)
        if len(calls) < 3:
            raise asyncpg.InterfaceError("transient")
        return "ok"

    result = _run(_db._with_retry(coro, label="test", retries=2, delay=0))
    assert result == "ok"
    assert len(calls) == 3


def test_with_retry_increments_failure_counter_on_exhaustion():
    """When retries are exhausted the failure counter is incremented."""
    import NightCityBot.utils.db as db_mod

    before = db_mod._db_failures

    async def coro():
        raise asyncpg.InterfaceError("always fails")

    with pytest.raises(asyncpg.InterfaceError):
        _run(db_mod._with_retry(coro, label="test", retries=1, delay=0))

    assert db_mod._db_failures == before + 1


def test_with_retry_non_transient_error_propagates_immediately():
    """Non-transient exceptions bypass the retry loop and propagate right away."""
    calls = []

    async def coro():
        calls.append(1)
        raise ValueError("logic error")

    with pytest.raises(ValueError):
        _run(_db._with_retry(coro, label="test", retries=2, delay=0))

    assert len(calls) == 1


def test_get_failure_count_returns_int():
    """get_failure_count() always returns an int."""
    count = _db.get_failure_count()
    assert isinstance(count, int)
    assert count >= 0


# ---------------------------------------------------------------------------
# warn_db_failure tests (offline — no real bot object)
# ---------------------------------------------------------------------------

def test_warn_db_failure_handles_missing_channel():
    """warn_db_failure should not raise even if the bot/channel is unavailable."""

    class FakeBot:
        def get_channel(self, ch_id):
            return None

    _run(_db.warn_db_failure(FakeBot(), "test_op", "some detail"))


def test_warn_db_failure_increments_counter_and_sets_timestamp():
    """warn_db_failure increments _db_failures and records _last_failure_at."""
    import NightCityBot.utils.db as db_mod
    from datetime import datetime

    class FakeBot:
        def get_channel(self, ch_id):
            return None

    before = db_mod._db_failures
    before_ts = db_mod._last_failure_at

    _run(db_mod.warn_db_failure(FakeBot(), "test_warn_op", "detail text"))

    assert db_mod._db_failures == before + 1
    assert db_mod._last_failure_at is not None
    assert isinstance(db_mod._last_failure_at, datetime)
    if before_ts is not None:
        assert db_mod._last_failure_at >= before_ts


def test_get_last_failure_at_returns_none_or_datetime():
    """get_last_failure_at() returns None or a datetime after warn_db_failure calls."""
    from datetime import datetime

    result = _db.get_last_failure_at()
    assert result is None or isinstance(result, datetime)


def test_retry_exhaustion_sets_last_failure_at():
    """_with_retry exhaustion records _last_failure_at as well as incrementing counter."""
    import NightCityBot.utils.db as db_mod
    from datetime import datetime

    async def coro():
        raise asyncpg.InterfaceError("always fails")

    with pytest.raises(asyncpg.InterfaceError):
        _run(db_mod._with_retry(coro, label="timestamp_test", retries=1, delay=0))

    assert db_mod._last_failure_at is not None
    assert isinstance(db_mod._last_failure_at, datetime)


# ---------------------------------------------------------------------------
# pi_add_item SQL parameter alignment test
# ---------------------------------------------------------------------------

def test_pi_add_item_sql_parameter_alignment():
    """Verify pi_add_item passes parameters in the correct column order.

    Regression test: character_id was accidentally inserted at position $4
    (item_type column), shifting all subsequent parameters by one.
    """
    from unittest.mock import AsyncMock, patch, MagicMock
    from NightCityBot.utils.db import pi_add_item

    captured = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return "INSERT 0 1"

    mock_pool = MagicMock()
    mock_pool.execute = fake_execute

    async def fake_retry(fn, **kw):
        return await fn()

    item = {
        "item_id": "test-uuid-001",
        "owner_id": "owner-123",
        "character_name": "V",
        "character_id": "char-uuid-456",
        "item_type": "weapon",
        "name": "Malorian Arms 3516",
        "restriction": "restricted",
        "description": "Johnny's gun",
        "price_paid": 50000,
        "seller_id": "seller-789",
        "seller_name": "Wilson",
    }

    async def run():
        with patch("NightCityBot.utils.db.get_pool", AsyncMock(return_value=mock_pool)):
            with patch("NightCityBot.utils.db._with_retry", side_effect=fake_retry):
                return await pi_add_item(item)

    result = _run(run())
    assert result is True

    args = captured["args"]
    assert args[0] == "test-uuid-001", f"$1 (item_id) = {args[0]}"
    assert args[1] == "owner-123", f"$2 (owner_id) = {args[1]}"
    assert args[2] == "V", f"$3 (character_name) = {args[2]}"
    assert args[3] == "weapon", f"$4 (item_type) = {args[3]}"
    assert args[4] == "Malorian Arms 3516", f"$5 (name) = {args[4]}"
    assert args[5] == "restricted", f"$6 (restriction) = {args[5]}"
    assert args[6] == "Johnny's gun", f"$7 (description) = {args[6]}"
    assert args[7] == 50000, f"$8 (price_paid) = {args[7]}"
    assert args[8] == "seller-789", f"$9 (seller_id) = {args[8]}"
    assert args[9] == "Wilson", f"$10 (seller_name) = {args[9]}"
    assert args[11] == "char-uuid-456", f"$12 (character_id) = {args[11]}"
    assert len(args) == 12, f"Expected 12 args for 12 placeholders, got {len(args)}"
