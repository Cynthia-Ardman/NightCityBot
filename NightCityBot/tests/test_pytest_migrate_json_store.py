"""Offline tests for migrate_json_store_blobs() and its per-key handlers (Task #4).

All tests use a lightweight fake pool/connection — no real database required.
"""
import asyncio
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


from NightCityBot.utils import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeConn:
    """Minimal fake asyncpg connection that records executed SQL and returns a result."""

    def __init__(self, insert_result="INSERT 0 1"):
        self._insert_result = insert_result
        self.executed: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append((sql.strip(), args))
        if "INSERT" in sql.upper():
            return self._insert_result
        return "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _FakePool:
    """Minimal fake asyncpg pool backed by a single _FakeConn."""

    def __init__(self, rows=None, insert_result="INSERT 0 1"):
        self._rows = rows or []
        self._conn = _FakeConn(insert_result)

    def acquire(self):
        return self._conn

    async def fetch(self, sql, *args):
        return self._rows

    async def execute(self, sql, *args):
        return self._conn.execute.__wrapped__(self._conn, sql, *args) if hasattr(self._conn.execute, "__wrapped__") else await self._conn.execute(sql, *args)


# ---------------------------------------------------------------------------
# _count_inserted
# ---------------------------------------------------------------------------

def test_count_inserted_normal():
    assert _db._count_inserted("INSERT 0 1") == 1


def test_count_inserted_zero():
    assert _db._count_inserted("INSERT 0 0") == 0


def test_count_inserted_garbage():
    assert _db._count_inserted("") == 0


# ---------------------------------------------------------------------------
# _mig_result
# ---------------------------------------------------------------------------

def test_mig_result_basic():
    r = _db._mig_result("some_table", 10, 7, 1)
    assert r["target"] == "some_table"
    assert r["found"] == 10
    assert r["inserted"] == 7
    assert r["errors"] == 1
    assert r["skipped"] == 2  # 10 - 7 - 1


def test_mig_result_no_negatives():
    r = _db._mig_result("t", 0, 0, 0)
    assert r["skipped"] == 0


# ---------------------------------------------------------------------------
# _mig_attendance
# ---------------------------------------------------------------------------

def test_mig_attendance_inserts_rows():
    data = {
        "111": ["2024-01-01T10:00:00", "2024-02-01T10:00:00"],
        "222": ["2024-03-01T10:00:00"],
    }
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_attendance(pool, data))
    assert result["found"] == 3
    assert result["inserted"] == 3
    assert result["errors"] == 0
    assert result["target"] == "attendance_log"


def test_mig_attendance_skips_on_conflict():
    data = {"111": ["2024-01-01T10:00:00"]}
    pool = _FakePool(insert_result="INSERT 0 0")
    result = _run(_db._mig_attendance(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 0
    assert result["skipped"] == 1


def test_mig_attendance_bad_data():
    result = _run(_db._mig_attendance(_FakePool(), "not a dict"))
    assert result["errors"] == 1


# ---------------------------------------------------------------------------
# _mig_open_log
# ---------------------------------------------------------------------------

def test_mig_open_log_inserts():
    data = {"333": ["2024-04-01T12:00:00"]}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_open_log(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "business_open_log"


# ---------------------------------------------------------------------------
# _mig_last_payment
# ---------------------------------------------------------------------------

def test_mig_last_payment_inserts():
    data = {"444": "Paid $500 rent on 2024-01-01"}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_last_payment(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "last_payment"


# ---------------------------------------------------------------------------
# _mig_rent_run
# ---------------------------------------------------------------------------

def test_mig_rent_run_dict_format():
    data = {"last_run": "2024-05-01T00:00:00"}

    class SimplePool:
        async def execute(self, sql, *args):
            return "INSERT 0 1"

    result = _run(_db._mig_rent_run(SimplePool(), data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "rent_runs"


def test_mig_rent_run_empty():
    result = _run(_db._mig_rent_run(_FakePool(), {}))
    assert result["found"] == 0
    assert result["inserted"] == 0


# ---------------------------------------------------------------------------
# _mig_cyberware_status
# ---------------------------------------------------------------------------

def test_mig_cyberware_status_dict_entry():
    data = {
        "555": {"weeks": 3, "last": "2024-06-01T00:00:00"},
        "_last_run": "2024-06-01T00:00:00",  # should be skipped
    }
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_cyberware_status(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "cyberware_status"


def test_mig_cyberware_status_int_entry():
    data = {"666": 5}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_cyberware_status(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1


# ---------------------------------------------------------------------------
# _mig_cyberware_last_run
# ---------------------------------------------------------------------------

def test_mig_cyberware_last_run_string():
    class SimplePool:
        async def execute(self, sql, *args):
            return "INSERT 0 1"

    result = _run(_db._mig_cyberware_last_run(SimplePool(), "2024-07-01T00:00:00"))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "cyberware_meta"


# ---------------------------------------------------------------------------
# _mig_cyberware_weekly
# ---------------------------------------------------------------------------

def test_mig_cyberware_weekly_inserts():
    data = [
        {"timestamp": "2024-08-01T00:00:00", "checkup": ["1"], "paid": ["2"], "unpaid": []},
    ]
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_cyberware_weekly(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "cyberware_weekly_runs"


def test_mig_cyberware_weekly_bad_data():
    result = _run(_db._mig_cyberware_weekly(_FakePool(), "not a list"))
    assert result["errors"] == 1


# ---------------------------------------------------------------------------
# _mig_system_status
# ---------------------------------------------------------------------------

def test_mig_system_status_inserts():
    data = {"rent": True, "cyberware": False}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_system_status(pool, data))
    assert result["found"] == 2
    assert result["inserted"] == 2
    assert result["target"] == "system_settings"


# ---------------------------------------------------------------------------
# _mig_wholesaler_lots / stores / shops / settings
# ---------------------------------------------------------------------------

def test_mig_wholesaler_lots_inserts():
    data = [{"lot_id": "L1", "gun_name": "Pistol", "gun_level": "H", "unit_cost": 500, "qty_available": 3}]
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_wholesaler_lots(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "wholesale_lots"


def test_mig_wholesaler_stores_inserts():
    data = {"store1": {"owner_id": "777", "inventory": []}}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_wholesaler_stores(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "wholesaler_stores"


def test_mig_wholesaler_shops_inserts():
    data = {"shop_alias": {"channel_id": 123}}
    pool = _FakePool(insert_result="INSERT 0 1")
    result = _run(_db._mig_wholesaler_shops(pool, data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "wholesaler_shops"


def test_mig_wholesaler_settings_inserts():
    class SimplePool:
        async def execute(self, sql, *args):
            return "INSERT 0 1"

    data = {"auto_refresh": True, "lot_count": 5}
    result = _run(_db._mig_wholesaler_settings(SimplePool(), data))
    assert result["found"] == 1
    assert result["inserted"] == 1
    assert result["target"] == "wholesaler_settings"


# ---------------------------------------------------------------------------
# migrate_json_store_blobs — integration (offline fake pool)
# ---------------------------------------------------------------------------

def test_migrate_json_store_blobs_empty_store():
    """Empty json_store → empty summary."""
    pool = _FakePool(rows=[])
    result = _run(_db.migrate_json_store_blobs(pool))
    assert result == {}


def test_migrate_json_store_blobs_unknown_key():
    """Unknown key is logged and appears with target=None."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: "unknown_blob_key" if k == "key" else json.dumps({"x": 1})
    pool = _FakePool(rows=[row])
    result = _run(_db.migrate_json_store_blobs(pool))
    assert "unknown_blob_key" in result
    assert result["unknown_blob_key"]["target"] is None


def test_migrate_json_store_blobs_known_key():
    """Known key is dispatched and returns stats."""
    attendance_data = {"100": ["2024-01-01T10:00:00"]}

    class FakeRow:
        def __getitem__(self, k):
            if k == "key":
                return "attendance"
            return attendance_data

    pool = _FakePool(rows=[FakeRow()], insert_result="INSERT 0 1")
    result = _run(_db.migrate_json_store_blobs(pool))
    assert "attendance" in result
    assert result["attendance"]["target"] == "attendance_log"
    assert result["attendance"]["found"] == 1
    assert result["attendance"]["inserted"] == 1
