"""Async PostgreSQL connection pool and per-entity DB helpers for NightCityBot.

All persistent state lives in typed tables — json_store is kept for backwards
compatibility / emergency fallback but new code must use the helper functions
below, not raw db_load / db_save.
"""
import asyncio
import asyncpg
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None

# ---------------------------------------------------------------------------
# Retry / resilience helpers
# ---------------------------------------------------------------------------

_db_failures: int = 0          # cumulative count — incremented by both _with_retry exhaustion and warn_db_failure
_last_failure_at: datetime | None = None  # UTC timestamp of the most recent recorded failure

_TRANSIENT_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.TooManyConnectionsError,
)


async def _with_retry(coro_factory, *, label: str = "", retries: int = 2, delay: float = 0.5):
    """Call coro_factory() up to retries+1 times on transient DB errors.

    Non-transient exceptions (constraint violations, etc.) propagate immediately.
    On final failure the module-level _db_failures counter is incremented and
    _last_failure_at is set to the current UTC time.
    """
    global _db_failures, _last_failure_at
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except _TRANSIENT_ERRORS as exc:
            if attempt < retries:
                logger.warning(
                    "Transient DB error on '%s' (attempt %d/%d): %s",
                    label, attempt + 1, retries + 1, exc,
                )
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                _db_failures += 1
                _last_failure_at = datetime.utcnow()
                logger.error(
                    "DB operation '%s' failed after %d attempt(s)",
                    label, retries + 1, exc_info=True,
                )
                raise
        except Exception:
            raise


def get_failure_count() -> int:
    """Return the cumulative number of write-path failures since startup."""
    return _db_failures


def get_last_failure_at() -> "datetime | None":
    """Return the UTC datetime of the most recent recorded write failure, or None."""
    return _last_failure_at


async def db_ping() -> float | None:
    """Run SELECT 1 and return latency in ms, or None on error."""
    import time
    try:
        pool = await get_pool()
        t0 = time.monotonic()
        await pool.fetchval("SELECT 1")
        return (time.monotonic() - t0) * 1000
    except Exception:
        logger.error("db_ping failed", exc_info=True)
        return None


async def warn_db_failure(bot, operation: str, detail: str) -> None:
    """Increment the failure counter, record the failure timestamp, and alert the audit channel.

    Call this from cog-level code when a write operation returns False/None.
    Also increments _db_failures and sets _last_failure_at so !db_health reflects
    failures that are surfaced through the cog layer, not just retry exhaustions.
    """
    global _db_failures, _last_failure_at
    _db_failures += 1
    _last_failure_at = datetime.utcnow()
    logger.error("DB failure alert: %s — %s", operation, detail)
    try:
        import config as _config
        ch_id = getattr(_config, "AUDIT_LOG_CHANNEL_ID", 0)
        channel = bot.get_channel(ch_id)
        if channel:
            await channel.send(
                f"\U0001f534 **DB failure** in `{operation}`: {detail[:400]}"
            )
    except Exception:
        logger.warning("warn_db_failure: could not send Discord alert", exc_info=True)


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

class _DBLoadFailed:
    """Returned by db_load when a database error occurs (falsy, but distinguishable)."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "DB_LOAD_FAILED"


DB_LOAD_FAILED = _DBLoadFailed()


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool
    async with _get_pool_lock():
        if _pool is not None:
            return _pool
        # PROD_DATABASE_URL takes priority when set (production deployment uses
        # a separate database from the dev Replit PostgreSQL instance).
        dsn = os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "Neither PROD_DATABASE_URL nor DATABASE_URL environment variable is set."
            )
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        logger.info("PostgreSQL connection pool created.")
        await _ensure_schema(_pool)
    return _pool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

async def _ensure_schema(pool: asyncpg.Pool) -> None:
    """Create all required tables if they do not exist."""

    statements = [
        # Legacy KV blob store — kept for migration seeding only
        """
        CREATE TABLE IF NOT EXISTS json_store (
            key         TEXT PRIMARY KEY,
            value       JSONB NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        # Already-normalized tables from previous migration
        """
        CREATE TABLE IF NOT EXISTS attendance_log (
            user_id     TEXT NOT NULL,
            logged_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, logged_at)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ticket_index (
            message_id  TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            ts          TIMESTAMPTZ NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            body        TEXT NOT NULL DEFAULT ''
        )
        """,
        # ── Economy ──────────────────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS business_open_log (
            user_id     TEXT NOT NULL,
            opened_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, opened_at)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS last_payment (
            user_id     TEXT PRIMARY KEY,
            summary     TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rent_runs (
            id              SERIAL PRIMARY KEY,
            run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            initiated_by    TEXT
        )
        """,
        # ── System control ───────────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            system_name TEXT PRIMARY KEY,
            enabled     BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        # ── Cyberware ────────────────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS cyberware_status (
            user_id         TEXT PRIMARY KEY,
            weeks           INT NOT NULL DEFAULT 0,
            last_processed  TIMESTAMPTZ,
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cyberware_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cyberware_weekly_runs (
            id          SERIAL PRIMARY KEY,
            run_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checkup_ids TEXT[] NOT NULL DEFAULT '{}',
            paid_ids    TEXT[] NOT NULL DEFAULT '{}',
            unpaid_ids  TEXT[] NOT NULL DEFAULT '{}'
        )
        """,
        # ── DM handler ───────────────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS dm_threads (
            user_id     TEXT PRIMARY KEY,
            thread_id   BIGINT NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        # ── Wholesaler ───────────────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS wholesale_lots (
            lot_id          TEXT PRIMARY KEY,
            gun_name        TEXT NOT NULL DEFAULT '',
            gun_level       TEXT NOT NULL DEFAULT '',
            unit_cost       INT NOT NULL DEFAULT 0,
            qty_available   INT NOT NULL DEFAULT 0,
            data            JSONB NOT NULL DEFAULT '{}',
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wholesaler_stores (
            store_id    TEXT PRIMARY KEY,
            owner_id    TEXT,
            data        JSONB NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wholesaler_shops (
            shop_key    TEXT PRIMARY KEY,
            data        JSONB NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wholesaler_pending_payouts (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            amount      INT NOT NULL,
            reason      TEXT DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wholesaler_settings (
            key         TEXT PRIMARY KEY,
            value       JSONB NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wholesaler_transactions (
            tx_id       TEXT PRIMARY KEY,
            tx_type     TEXT NOT NULL DEFAULT '',
            ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status      TEXT NOT NULL DEFAULT 'SUCCESS',
            actor_id    TEXT,
            lot_id      TEXT,
            data        JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        # ── Bot configuration (editable monetary constants) ───────────────
        """
        CREATE TABLE IF NOT EXISTS bot_config (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """,
    ]

    async with pool.acquire() as conn:
        for stmt in statements:
            await conn.execute(stmt)

    logger.info("DB schema verified — all tables ready.")

    # One-time migrations from legacy json_store blobs
    await _migrate_all(pool)


async def _migrate_all(pool: asyncpg.Pool) -> None:
    """Run all one-time migrations from json_store blobs to typed tables."""
    await _seed_attendance_log_from_file(pool)
    await _migrate_business_open_log(pool)
    await _migrate_last_payment(pool)
    await _migrate_rent_runs(pool)
    await _migrate_system_settings(pool)
    await _migrate_cyberware_status(pool)
    await _migrate_cyberware_weekly(pool)
    await _migrate_dm_threads(pool)
    await _migrate_wholesaler(pool)


# ---------------------------------------------------------------------------
# Legacy json_store helpers (kept for migration seeding; do not use in new code)
# ---------------------------------------------------------------------------

async def db_load(key: str, default=None, seed_path: Path | str | None = None):
    """Load a JSONB blob from json_store. Use typed helpers for new code."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = $1", key)
        if row is not None:
            raw = row["value"]
            return json.loads(raw) if isinstance(raw, str) else raw
        if seed_path is not None:
            path = Path(seed_path)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8").strip()
                    if content:
                        data = json.loads(content)
                        await db_save(key, data)
                        logger.info("Seeded DB key '%s' from %s", key, path)
                        return data
                except Exception:
                    logger.error("Failed to seed DB key '%s' from %s", key, path, exc_info=True)
    except Exception:
        logger.error("db_load failed for key '%s'", key, exc_info=True)
        return DB_LOAD_FAILED
    return default


async def db_save(key: str, value) -> bool:
    """Persist a JSON-serialisable value under key in json_store."""
    try:
        pool = await get_pool()
        serialized = json.dumps(value)
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO json_store (key, value, updated_at)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
                key,
                serialized,
            ),
            label="db_save",
        )
        return True
    except Exception:
        logger.error("db_save failed for key '%s'", key, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Attendance (already normalized — helpers unchanged)
# ---------------------------------------------------------------------------

async def attendance_get_user(user_id: str) -> list[str]:
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT logged_at FROM attendance_log WHERE user_id = $1 ORDER BY logged_at",
            user_id,
        )
        return [row["logged_at"].isoformat() for row in rows]
    except Exception:
        logger.error("attendance_get_user failed for user '%s'", user_id, exc_info=True)
        return []


async def attendance_append(user_id: str, logged_at_iso: str) -> bool:
    try:
        pool = await get_pool()
        dt = datetime.fromisoformat(logged_at_iso)
        await _with_retry(
            lambda: pool.execute(
                "INSERT INTO attendance_log (user_id, logged_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, dt,
            ),
            label="attendance_append",
        )
        return True
    except Exception:
        logger.error("attendance_append failed for user '%s'", user_id, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

async def _seed_attendance_log_from_file(pool: asyncpg.Pool) -> None:
    try:
        row_count = await pool.fetchval("SELECT COUNT(*) FROM attendance_log")
        if row_count and row_count > 0:
            return
        try:
            import config as _config
            attend_file = Path(getattr(_config, "ATTEND_LOG_FILE", ""))
        except Exception:
            return
        if not attend_file.exists():
            return
        content = attend_file.read_text(encoding="utf-8").strip()
        if not content:
            return
        attend_data = json.loads(content)
        if not isinstance(attend_data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, timestamps in attend_data.items():
                    if not isinstance(timestamps, list):
                        continue
                    for ts in timestamps:
                        if not isinstance(ts, str):
                            continue
                        try:
                            dt = datetime.fromisoformat(ts)
                            await conn.execute(
                                "INSERT INTO attendance_log (user_id, logged_at)"
                                " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                str(user_id), dt,
                            )
                            inserted += 1
                        except Exception:
                            logger.warning("Failed to seed attendance row", exc_info=True)
        if inserted:
            logger.info("Seeded attendance_log with %d rows", inserted)
    except Exception:
        logger.error("_seed_attendance_log_from_file failed", exc_info=True)


async def _migrate_business_open_log(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM business_open_log")
        if count and count > 0:
            return
        # Load from json_store (which may itself have been seeded from a file)
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'open_log'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "OPEN_LOG_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, timestamps in data.items():
                    if not isinstance(timestamps, list):
                        continue
                    for ts in timestamps:
                        if not isinstance(ts, str):
                            continue
                        try:
                            dt = datetime.fromisoformat(ts)
                            await conn.execute(
                                "INSERT INTO business_open_log (user_id, opened_at)"
                                " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                str(user_id), dt,
                            )
                            inserted += 1
                        except Exception:
                            logger.warning("Failed to seed business_open_log row", exc_info=True)
        if inserted:
            logger.info("Migrated %d rows into business_open_log", inserted)
    except Exception:
        logger.error("_migrate_business_open_log failed", exc_info=True)


async def _migrate_last_payment(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM last_payment")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'last_payment'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "LAST_PAYMENT_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, summary in data.items():
                    if not isinstance(summary, str):
                        summary = str(summary)
                    await conn.execute(
                        """
                        INSERT INTO last_payment (user_id, summary)
                        VALUES ($1, $2) ON CONFLICT DO NOTHING
                        """,
                        str(user_id), summary,
                    )
                    inserted += 1
        if inserted:
            logger.info("Migrated %d rows into last_payment", inserted)
    except Exception:
        logger.error("_migrate_last_payment failed", exc_info=True)


async def _migrate_rent_runs(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM rent_runs")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'last_rent'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "LAST_RENT_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict) or "last_run" not in data:
            return
        dt = datetime.fromisoformat(data["last_run"])
        pool2 = await get_pool()
        await pool2.execute(
            "INSERT INTO rent_runs (run_at, initiated_by) VALUES ($1, $2)",
            dt, "migrated",
        )
        logger.info("Migrated last_rent into rent_runs")
    except Exception:
        logger.error("_migrate_rent_runs failed", exc_info=True)


async def _migrate_system_settings(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM system_settings")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'system_status'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "SYSTEM_STATUS_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for system_name, enabled in data.items():
                    await conn.execute(
                        """
                        INSERT INTO system_settings (system_name, enabled)
                        VALUES ($1, $2) ON CONFLICT DO NOTHING
                        """,
                        str(system_name), bool(enabled),
                    )
                    inserted += 1
        if inserted:
            logger.info("Migrated %d rows into system_settings", inserted)
    except Exception:
        logger.error("_migrate_system_settings failed", exc_info=True)


async def _migrate_cyberware_status(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM cyberware_status")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'cyberware_log'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "CYBERWARE_LOG_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, v in data.items():
                    if user_id == "_last_run":
                        # Migrate into cyberware_meta
                        await conn.execute(
                            """
                            INSERT INTO cyberware_meta (key, value)
                            VALUES ('last_full_run', $1)
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                            """,
                            str(v),
                        )
                        continue
                    if isinstance(v, dict):
                        weeks = int(v.get("weeks", 0))
                        last_str = v.get("last")
                    else:
                        weeks = int(v)
                        last_str = None
                    last_dt = None
                    if last_str:
                        try:
                            last_dt = datetime.fromisoformat(last_str)
                        except Exception:
                            pass
                    await conn.execute(
                        """
                        INSERT INTO cyberware_status (user_id, weeks, last_processed)
                        VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                        """,
                        str(user_id), weeks, last_dt,
                    )
                    inserted += 1
        if inserted:
            logger.info("Migrated %d rows into cyberware_status", inserted)
    except Exception:
        logger.error("_migrate_cyberware_status failed", exc_info=True)


async def _migrate_cyberware_weekly(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM cyberware_weekly_runs")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'cyberware_weekly'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "CYBERWARE_WEEKLY_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, list):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    ts_str = entry.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        run_at = datetime.fromisoformat(ts_str)
                    except Exception:
                        continue
                    checkup = [str(x) for x in entry.get("checkup", [])]
                    paid = [str(x) for x in entry.get("paid", [])]
                    unpaid = [str(x) for x in entry.get("unpaid", [])]
                    await conn.execute(
                        """
                        INSERT INTO cyberware_weekly_runs (run_at, checkup_ids, paid_ids, unpaid_ids)
                        VALUES ($1, $2, $3, $4)
                        """,
                        run_at, checkup, paid, unpaid,
                    )
                    inserted += 1
        if inserted:
            logger.info("Migrated %d rows into cyberware_weekly_runs", inserted)
    except Exception:
        logger.error("_migrate_cyberware_weekly failed", exc_info=True)


async def _migrate_dm_threads(pool: asyncpg.Pool) -> None:
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM dm_threads")
        if count and count > 0:
            return
        row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'thread_map'")
        if not row:
            try:
                import config as _config
                f = Path(getattr(_config, "THREAD_MAP_FILE", ""))
                if f.exists():
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    return
            except Exception:
                return
        else:
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return
        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, thread_id in data.items():
                    try:
                        await conn.execute(
                            "INSERT INTO dm_threads (user_id, thread_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                            str(user_id), int(thread_id),
                        )
                        inserted += 1
                    except Exception:
                        logger.warning("Failed to migrate dm_thread user=%s", user_id, exc_info=True)
        if inserted:
            logger.info("Migrated %d rows into dm_threads", inserted)
    except Exception:
        logger.error("_migrate_dm_threads failed", exc_info=True)


async def _migrate_wholesaler(pool: asyncpg.Pool) -> None:
    try:
        lots_count = await pool.fetchval("SELECT COUNT(*) FROM wholesale_lots")
        stores_count = await pool.fetchval("SELECT COUNT(*) FROM wholesaler_stores")
        shops_count = await pool.fetchval("SELECT COUNT(*) FROM wholesaler_shops")
        tx_count = await pool.fetchval("SELECT COUNT(*) FROM wholesaler_transactions")
        has_data = (lots_count or 0) + (stores_count or 0) + (shops_count or 0) + (tx_count or 0)
        if has_data > 0:
            return

        # Migrate wholesaler_state blob
        state_row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'wholesaler_state'")
        if state_row:
            raw = state_row["value"]
            state = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(state, dict):
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        # wholesale_lots
                        for lot in state.get("wholesale_lots", []):
                            if not isinstance(lot, dict):
                                continue
                            lot_id = lot.get("lot_id")
                            if not lot_id:
                                continue
                            extra = {k: v for k, v in lot.items()
                                     if k not in ("lot_id", "gun_name", "gun_level", "unit_cost", "qty_available")}
                            await conn.execute(
                                """
                                INSERT INTO wholesale_lots (lot_id, gun_name, gun_level, unit_cost, qty_available, data)
                                VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT DO NOTHING
                                """,
                                lot_id,
                                str(lot.get("gun_name", "")),
                                str(lot.get("gun_level", "")),
                                int(lot.get("unit_cost", 0)),
                                int(lot.get("qty_available", 0)),
                                json.dumps(extra),
                            )
                        # stores
                        for store_id, store in state.get("stores", {}).items():
                            if not isinstance(store, dict):
                                continue
                            owner_id = store.get("owner_id")
                            extra = {k: v for k, v in store.items() if k != "owner_id"}
                            await conn.execute(
                                """
                                INSERT INTO wholesaler_stores (store_id, owner_id, data)
                                VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING
                                """,
                                str(store_id),
                                str(owner_id) if owner_id is not None else None,
                                json.dumps(extra),
                            )
                        # shop_registry
                        for shop_key, shop_data in state.get("shop_registry", {}).items():
                            await conn.execute(
                                """
                                INSERT INTO wholesaler_shops (shop_key, data)
                                VALUES ($1, $2::jsonb) ON CONFLICT DO NOTHING
                                """,
                                str(shop_key), json.dumps(shop_data),
                            )
                        # pending_payouts
                        for payout in state.get("pending_payouts", []):
                            if not isinstance(payout, dict):
                                continue
                            await conn.execute(
                                """
                                INSERT INTO wholesaler_pending_payouts (user_id, amount, reason)
                                VALUES ($1, $2, $3)
                                """,
                                str(payout.get("seller_id", payout.get("user_id", ""))),
                                int(payout.get("amount", 0)),
                                str(payout.get("reason", "")),
                            )
                        # settings
                        settings = state.get("settings", {})
                        if settings:
                            await conn.execute(
                                """
                                INSERT INTO wholesaler_settings (key, value)
                                VALUES ('main', $1::jsonb) ON CONFLICT DO NOTHING
                                """,
                                json.dumps(settings),
                            )
                logger.info("Migrated wholesaler_state into typed tables")

        # Migrate wholesaler_tx blob
        tx_row = await pool.fetchrow("SELECT value FROM json_store WHERE key = 'wholesaler_tx'")
        if tx_row:
            raw = tx_row["value"]
            tx_list = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(tx_list, list):
                imported = 0
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for tx in tx_list:
                            if not isinstance(tx, dict):
                                continue
                            tx_id = tx.get("tx_id")
                            if not tx_id:
                                continue
                            ts_str = tx.get("timestamp")
                            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.utcnow()
                            await conn.execute(
                                """
                                INSERT INTO wholesaler_transactions
                                    (tx_id, tx_type, ts, status, actor_id, lot_id, data)
                                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                                ON CONFLICT DO NOTHING
                                """,
                                tx_id,
                                str(tx.get("type", "")),
                                ts,
                                str(tx.get("status", "SUCCESS")),
                                str(tx.get("seller_id", tx.get("buyer_id", tx.get("actor_id", "")))) or None,
                                tx.get("lot_id"),
                                json.dumps(tx),
                            )
                            imported += 1
                if imported:
                    logger.info("Migrated %d rows into wholesaler_transactions", imported)
    except Exception:
        logger.error("_migrate_wholesaler failed", exc_info=True)


# ---------------------------------------------------------------------------
# Business open log helpers
# ---------------------------------------------------------------------------

async def open_log_exists_today(user_id: str) -> bool:
    """Return True if the user already logged a business opening today (UTC)."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT 1 FROM business_open_log WHERE user_id = $1 AND DATE(opened_at) = CURRENT_DATE",
            user_id,
        )
        return row is not None
    except Exception:
        logger.error("open_log_exists_today failed for user '%s'", user_id, exc_info=True)
        return False


async def open_log_count_month(user_id: str, year: int, month: int) -> int:
    """Count how many times the user opened this month."""
    try:
        pool = await get_pool()
        val = await pool.fetchval(
            """
            SELECT COUNT(*) FROM business_open_log
            WHERE user_id = $1
              AND EXTRACT(YEAR  FROM opened_at) = $2
              AND EXTRACT(MONTH FROM opened_at) = $3
            """,
            user_id, year, month,
        )
        return int(val or 0)
    except Exception:
        logger.error("open_log_count_month failed for user '%s'", user_id, exc_info=True)
        return 0


async def open_log_add(user_id: str, opened_at: datetime) -> bool:
    """Insert a business opening event (idempotent)."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                "INSERT INTO business_open_log (user_id, opened_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, opened_at,
            ),
            label="open_log_add",
        )
        return True
    except Exception:
        logger.error("open_log_add failed for user '%s'", user_id, exc_info=True)
        return False


async def open_log_get_user_month(user_id: str, year: int, month: int) -> list[datetime]:
    """Return all opening datetimes for a user in the given month."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT opened_at FROM business_open_log
            WHERE user_id = $1
              AND EXTRACT(YEAR  FROM opened_at) = $2
              AND EXTRACT(MONTH FROM opened_at) = $3
            ORDER BY opened_at
            """,
            user_id, year, month,
        )
        return [row["opened_at"] for row in rows]
    except Exception:
        logger.error("open_log_get_user_month failed for user '%s'", user_id, exc_info=True)
        return []


async def open_log_get_all() -> dict[str, list[str]]:
    """Return all open log entries as {user_id: [iso_str, ...]}."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT user_id, opened_at FROM business_open_log ORDER BY opened_at")
        result: dict[str, list[str]] = {}
        for row in rows:
            uid = row["user_id"]
            result.setdefault(uid, []).append(row["opened_at"].isoformat())
        return result
    except Exception:
        logger.error("open_log_get_all failed", exc_info=True)
        return {}


async def open_log_add_if_absent(user_id: str, opened_at_iso: str) -> bool:
    """Add an entry only if that exact timestamp is not already present."""
    try:
        pool = await get_pool()
        dt = datetime.fromisoformat(opened_at_iso)
        await _with_retry(
            lambda: pool.execute(
                "INSERT INTO business_open_log (user_id, opened_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, dt,
            ),
            label="open_log_add_if_absent",
        )
        return True
    except Exception:
        logger.error("open_log_add_if_absent failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Last payment helpers
# ---------------------------------------------------------------------------

async def last_payment_get(user_id: str) -> Optional[str]:
    """Return the last payment summary for a user, or None."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT summary FROM last_payment WHERE user_id = $1", user_id)
        return row["summary"] if row else None
    except Exception:
        logger.error("last_payment_get failed for user '%s'", user_id, exc_info=True)
        return None


async def last_payment_get_with_ts(user_id: str) -> "tuple[Optional[str], Optional[datetime]]":
    """Return (summary, updated_at) for the user's last payment, or (None, None)."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT summary, updated_at FROM last_payment WHERE user_id = $1", user_id
        )
        if row:
            return row["summary"], row["updated_at"]
        return None, None
    except Exception:
        logger.error("last_payment_get_with_ts failed for user '%s'", user_id, exc_info=True)
        return None, None


async def last_payment_set(user_id: str, summary: str) -> bool:
    """Store or update the last payment summary for a user."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO last_payment (user_id, summary, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW()
                """,
                user_id, summary,
            ),
            label="last_payment_set",
        )
        return True
    except Exception:
        logger.error("last_payment_set failed for user '%s'", user_id, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Rent run helpers
# ---------------------------------------------------------------------------

async def rent_run_get_last() -> Optional[datetime]:
    """Return the datetime of the most recent rent run, or None."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT run_at FROM rent_runs ORDER BY run_at DESC LIMIT 1")
        return row["run_at"] if row else None
    except Exception:
        logger.error("rent_run_get_last failed", exc_info=True)
        return None


async def rent_run_record(initiated_by: Optional[str] = None) -> bool:
    """Record that a rent run just completed."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                "INSERT INTO rent_runs (run_at, initiated_by) VALUES (NOW(), $1)",
                initiated_by,
            ),
            label="rent_run_record",
        )
        return True
    except Exception:
        logger.error("rent_run_record failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# System settings helpers
# ---------------------------------------------------------------------------

async def system_settings_get_all() -> dict[str, bool]:
    """Return all system enable/disable states as {system_name: bool}."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT system_name, enabled FROM system_settings")
        return {row["system_name"]: row["enabled"] for row in rows}
    except Exception:
        logger.error("system_settings_get_all failed", exc_info=True)
        return {}


async def system_settings_set(system_name: str, enabled: bool) -> bool:
    """Enable or disable a named system."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO system_settings (system_name, enabled, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (system_name) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW()
                """,
                system_name, enabled,
            ),
            label="system_settings_set",
        )
        return True
    except Exception:
        logger.error("system_settings_set failed for '%s'", system_name, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Cyberware helpers
# ---------------------------------------------------------------------------

async def cyberware_status_get_all() -> dict[str, dict]:
    """Return all user cyberware records as {user_id: {weeks, last_processed}}."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT user_id, weeks, last_processed FROM cyberware_status")
        result = {}
        for row in rows:
            last = row["last_processed"]
            result[row["user_id"]] = {
                "weeks": row["weeks"],
                "last": last.isoformat() if last else None,
            }
        return result
    except Exception:
        logger.error("cyberware_status_get_all failed", exc_info=True)
        return {}


async def cyberware_status_upsert(user_id: str, weeks: int, last_processed: Optional[datetime]) -> bool:
    """Insert or update a single user's cyberware record."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO cyberware_status (user_id, weeks, last_processed, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET weeks = EXCLUDED.weeks,
                        last_processed = EXCLUDED.last_processed,
                        updated_at = NOW()
                """,
                user_id, weeks, last_processed,
            ),
            label="cyberware_status_upsert",
        )
        return True
    except Exception:
        logger.error("cyberware_status_upsert failed for user '%s'", user_id, exc_info=True)
        return False


async def cyberware_status_upsert_many(data: dict[str, dict]) -> bool:
    """Bulk upsert cyberware status for all users."""
    try:
        pool = await get_pool()
        rows = []
        for user_id, v in data.items():
            weeks = int(v.get("weeks", 0))
            last_str = v.get("last")
            last_dt = None
            if last_str:
                try:
                    last_dt = datetime.fromisoformat(last_str)
                except Exception:
                    pass
            rows.append((user_id, weeks, last_dt))

        async def _do():
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for user_id, weeks, last_dt in rows:
                        await conn.execute(
                            """
                            INSERT INTO cyberware_status (user_id, weeks, last_processed, updated_at)
                            VALUES ($1, $2, $3, NOW())
                            ON CONFLICT (user_id) DO UPDATE
                                SET weeks = EXCLUDED.weeks,
                                    last_processed = EXCLUDED.last_processed,
                                    updated_at = NOW()
                            """,
                            user_id, weeks, last_dt,
                        )

        await _with_retry(_do, label="cyberware_status_upsert_many")
        return True
    except Exception:
        logger.error("cyberware_status_upsert_many failed", exc_info=True)
        return False


async def cyberware_last_run_get() -> Optional[datetime]:
    """Return the timestamp of the last full cyberware weekly run."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT value FROM cyberware_meta WHERE key = 'last_full_run'")
        if row:
            return datetime.fromisoformat(row["value"])
        return None
    except Exception:
        logger.error("cyberware_last_run_get failed", exc_info=True)
        return None


async def cyberware_last_run_set(dt: datetime) -> bool:
    """Record the timestamp of the most recent full cyberware weekly run."""
    try:
        pool = await get_pool()
        iso = dt.isoformat()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO cyberware_meta (key, value, updated_at) VALUES ('last_full_run', $1, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                iso,
            ),
            label="cyberware_last_run_set",
        )
        return True
    except Exception:
        logger.error("cyberware_last_run_set failed", exc_info=True)
        return False


async def cyberware_weekly_add(
    run_at: datetime,
    checkup_ids: list[str],
    paid_ids: list[str],
    unpaid_ids: list[str],
) -> bool:
    """Record the results of a weekly cyberware run."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO cyberware_weekly_runs (run_at, checkup_ids, paid_ids, unpaid_ids)
                VALUES ($1, $2, $3, $4)
                """,
                run_at, checkup_ids, paid_ids, unpaid_ids,
            ),
            label="cyberware_weekly_add",
        )
        return True
    except Exception:
        logger.error("cyberware_weekly_add failed", exc_info=True)
        return False


async def cyberware_weekly_get_all() -> list[dict]:
    """Return all weekly run records as a list of dicts (oldest first)."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT run_at, checkup_ids, paid_ids, unpaid_ids FROM cyberware_weekly_runs ORDER BY run_at")
        return [
            {
                "timestamp": row["run_at"].isoformat(),
                "checkup": list(row["checkup_ids"] or []),
                "paid": list(row["paid_ids"] or []),
                "unpaid": list(row["unpaid_ids"] or []),
            }
            for row in rows
        ]
    except Exception:
        logger.error("cyberware_weekly_get_all failed", exc_info=True)
        return []


async def cyberware_weekly_get_last_row() -> tuple[Optional[int], dict]:
    """Return (db_id, entry_dict) of the most recent weekly run, or (None, {})."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT id, run_at, checkup_ids, paid_ids, unpaid_ids FROM cyberware_weekly_runs ORDER BY run_at DESC LIMIT 1"
        )
        if row is None:
            return None, {}
        return row["id"], {
            "timestamp": row["run_at"].isoformat(),
            "checkup": [str(x) for x in (row["checkup_ids"] or [])],
            "paid": [str(x) for x in (row["paid_ids"] or [])],
            "unpaid": [str(x) for x in (row["unpaid_ids"] or [])],
        }
    except Exception:
        logger.error("cyberware_weekly_get_last_row failed", exc_info=True)
        return None, {}


async def cyberware_weekly_insert_empty() -> Optional[int]:
    """Insert a new empty weekly run row and return its ID."""
    try:
        pool = await get_pool()
        row_id = await _with_retry(
            lambda: pool.fetchval(
                "INSERT INTO cyberware_weekly_runs (run_at) VALUES (NOW()) RETURNING id"
            ),
            label="cyberware_weekly_insert_empty",
        )
        return row_id
    except Exception:
        logger.error("cyberware_weekly_insert_empty failed", exc_info=True)
        return None


async def cyberware_weekly_update_row(
    row_id: int,
    paid_ids: list[str],
    unpaid_ids: list[str],
) -> bool:
    """Update the paid/unpaid arrays for a specific weekly run row."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                "UPDATE cyberware_weekly_runs SET paid_ids = $2, unpaid_ids = $3 WHERE id = $1",
                row_id, paid_ids, unpaid_ids,
            ),
            label="cyberware_weekly_update_row",
        )
        return True
    except Exception:
        logger.error("cyberware_weekly_update_row failed for id=%s", row_id, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# DM thread helpers
# ---------------------------------------------------------------------------

async def dm_thread_get_all() -> dict[str, int]:
    """Return all user→thread_id mappings."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT user_id, thread_id FROM dm_threads")
        return {row["user_id"]: row["thread_id"] for row in rows}
    except Exception:
        logger.error("dm_thread_get_all failed", exc_info=True)
        return {}


async def dm_thread_set(user_id: str, thread_id: int) -> bool:
    """Upsert a user→thread mapping."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO dm_threads (user_id, thread_id, updated_at) VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET thread_id = EXCLUDED.thread_id, updated_at = NOW()
                """,
                user_id, thread_id,
            ),
            label="dm_thread_set",
        )
        return True
    except Exception:
        logger.error("dm_thread_set failed for user '%s'", user_id, exc_info=True)
        return False


async def dm_thread_delete(user_id: str) -> bool:
    """Remove a user→thread mapping."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute("DELETE FROM dm_threads WHERE user_id = $1", user_id),
            label="dm_thread_delete",
        )
        return True
    except Exception:
        logger.error("dm_thread_delete failed for user '%s'", user_id, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Wholesaler helpers
# ---------------------------------------------------------------------------

async def wh_lots_get_all() -> list[dict]:
    """Return all wholesale lots."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT lot_id, gun_name, gun_level, unit_cost, qty_available, data FROM wholesale_lots")
        result = []
        for row in rows:
            extra = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
            lot = {
                "lot_id": row["lot_id"],
                "gun_name": row["gun_name"],
                "gun_level": row["gun_level"],
                "unit_cost": row["unit_cost"],
                "qty_available": row["qty_available"],
                **extra,
            }
            result.append(lot)
        return result
    except Exception:
        logger.error("wh_lots_get_all failed", exc_info=True)
        return []


async def wh_lots_replace_all(lots: list[dict]) -> bool:
    """Atomically replace all wholesale lots."""
    try:
        pool = await get_pool()

        async def _do():
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM wholesale_lots")
                    for lot in lots:
                        if not isinstance(lot, dict):
                            continue
                        lot_id = lot.get("lot_id")
                        if not lot_id:
                            continue
                        extra = {k: v for k, v in lot.items()
                                 if k not in ("lot_id", "gun_name", "gun_level", "unit_cost", "qty_available")}
                        await conn.execute(
                            """
                            INSERT INTO wholesale_lots (lot_id, gun_name, gun_level, unit_cost, qty_available, data)
                            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                            """,
                            lot_id,
                            str(lot.get("gun_name", "")),
                            str(lot.get("gun_level", "")),
                            int(lot.get("unit_cost", 0)),
                            int(lot.get("qty_available", 0)),
                            json.dumps(extra),
                        )

        await _with_retry(_do, label="wh_lots_replace_all")
        return True
    except Exception:
        logger.error("wh_lots_replace_all failed", exc_info=True)
        return False


async def wh_stores_get_all() -> dict[str, dict]:
    """Return all wholesaler stores as {store_id: store_dict}."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT store_id, owner_id, data FROM wholesaler_stores")
        result = {}
        for row in rows:
            extra = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
            store = {"owner_id": row["owner_id"], **extra}
            result[row["store_id"]] = store
        return result
    except Exception:
        logger.error("wh_stores_get_all failed", exc_info=True)
        return {}


async def wh_stores_replace_all(stores: dict[str, dict]) -> bool:
    """Atomically replace all wholesaler stores."""
    try:
        pool = await get_pool()

        async def _do():
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM wholesaler_stores")
                    for store_id, store in stores.items():
                        if not isinstance(store, dict):
                            continue
                        owner_id = store.get("owner_id")
                        extra = {k: v for k, v in store.items() if k != "owner_id"}
                        await conn.execute(
                            """
                            INSERT INTO wholesaler_stores (store_id, owner_id, data)
                            VALUES ($1, $2, $3::jsonb)
                            """,
                            str(store_id),
                            str(owner_id) if owner_id is not None else None,
                            json.dumps(extra),
                        )

        await _with_retry(_do, label="wh_stores_replace_all")
        return True
    except Exception:
        logger.error("wh_stores_replace_all failed", exc_info=True)
        return False


async def wh_shops_get_all() -> dict[str, Any]:
    """Return all wholesaler shops (shop registry) as {shop_key: data_dict}."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT shop_key, data FROM wholesaler_shops")
        result = {}
        for row in rows:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
            result[row["shop_key"]] = data
        return result
    except Exception:
        logger.error("wh_shops_get_all failed", exc_info=True)
        return {}


async def wh_shops_replace_all(shops: dict[str, Any]) -> bool:
    """Atomically replace all wholesaler shop registry entries."""
    try:
        pool = await get_pool()

        async def _do():
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM wholesaler_shops")
                    for shop_key, data in shops.items():
                        await conn.execute(
                            "INSERT INTO wholesaler_shops (shop_key, data) VALUES ($1, $2::jsonb)",
                            str(shop_key), json.dumps(data),
                        )

        await _with_retry(_do, label="wh_shops_replace_all")
        return True
    except Exception:
        logger.error("wh_shops_replace_all failed", exc_info=True)
        return False


async def wh_pending_payouts_get() -> list[dict]:
    """Return all pending wholesaler payouts as a list of dicts."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, user_id, amount, reason, created_at FROM wholesaler_pending_payouts ORDER BY id"
        )
        return [
            {"id": row["id"], "seller_id": row["user_id"], "amount": row["amount"],
             "reason": row["reason"], "created_at": row["created_at"].isoformat()}
            for row in rows
        ]
    except Exception:
        logger.error("wh_pending_payouts_get failed", exc_info=True)
        return []


async def wh_pending_payouts_add(user_id: str, amount: int, reason: str = "") -> bool:
    """Add a new pending payout record."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                "INSERT INTO wholesaler_pending_payouts (user_id, amount, reason) VALUES ($1, $2, $3)",
                user_id, amount, reason,
            ),
            label="wh_pending_payouts_add",
        )
        return True
    except Exception:
        logger.error("wh_pending_payouts_add failed", exc_info=True)
        return False


async def wh_pending_payouts_delete(payout_id: int) -> bool:
    """Remove a specific pending payout by its DB row id."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                "DELETE FROM wholesaler_pending_payouts WHERE id = $1", payout_id
            ),
            label="wh_pending_payouts_delete",
        )
        return True
    except Exception:
        logger.error("wh_pending_payouts_delete failed", exc_info=True)
        return False


async def wh_pending_payouts_clear() -> bool:
    """Remove all pending payouts."""
    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute("DELETE FROM wholesaler_pending_payouts"),
            label="wh_pending_payouts_clear",
        )
        return True
    except Exception:
        logger.error("wh_pending_payouts_clear failed", exc_info=True)
        return False


async def wh_settings_get() -> dict:
    """Return the wholesaler settings dict."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT value FROM wholesaler_settings WHERE key = 'main'")
        if row:
            val = row["value"]
            return json.loads(val) if isinstance(val, str) else (val or {})
        return {}
    except Exception:
        logger.error("wh_settings_get failed", exc_info=True)
        return {}


async def wh_settings_save(settings: dict) -> bool:
    """Persist the wholesaler settings dict."""
    try:
        pool = await get_pool()
        serialized = json.dumps(settings)
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO wholesaler_settings (key, value, updated_at) VALUES ('main', $1::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                serialized,
            ),
            label="wh_settings_save",
        )
        return True
    except Exception:
        logger.error("wh_settings_save failed", exc_info=True)
        return False


async def wh_tx_append(tx: dict) -> bool:
    """Append a single wholesaler transaction record."""
    try:
        pool = await get_pool()
        tx_id = tx.get("tx_id")
        if not tx_id:
            logger.warning("wh_tx_append: tx has no tx_id, skipping")
            return False
        ts_str = tx.get("timestamp")
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.utcnow()
        actor = str(tx.get("seller_id", tx.get("buyer_id", tx.get("actor_id", "")))) or None
        serialized = json.dumps(tx)
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO wholesaler_transactions (tx_id, tx_type, ts, status, actor_id, lot_id, data)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (tx_id) DO NOTHING
                """,
                tx_id,
                str(tx.get("type", "")),
                ts,
                str(tx.get("status", "SUCCESS")),
                actor,
                tx.get("lot_id"),
                serialized,
            ),
            label="wh_tx_append",
        )
        return True
    except Exception:
        logger.error("wh_tx_append failed", exc_info=True)
        return False


async def wh_tx_get_all() -> list[dict]:
    """Return all wholesaler transactions (oldest first)."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT data FROM wholesaler_transactions ORDER BY ts"
        )
        result = []
        for row in rows:
            val = row["data"]
            if isinstance(val, str):
                result.append(json.loads(val))
            elif isinstance(val, dict):
                result.append(val)
        return result
    except Exception:
        logger.error("wh_tx_get_all failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Bot config (editable monetary constants stored in DB)
# ---------------------------------------------------------------------------

async def bot_config_get_all() -> list[tuple[str, str, str]]:
    """Return all bot_config rows as list of (key, value, description) tuples."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT key, value, COALESCE(description, '') AS description FROM bot_config ORDER BY key"
        )
        return [(row["key"], row["value"], row["description"]) for row in rows]
    except Exception:
        logger.error("bot_config_get_all failed", exc_info=True)
        return []


async def bot_config_get(key: str, default: str | None = None) -> str | None:
    """Return a single bot_config value, or *default* if missing."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT value FROM bot_config WHERE key = $1", key)
        return row["value"] if row else default
    except Exception:
        logger.error("bot_config_get failed for key '%s'", key, exc_info=True)
        return default


async def bot_config_set(key: str, value: str, description: str = "") -> bool:
    """Upsert a bot_config key-value pair."""
    try:
        pool = await get_pool()
        str_value = str(value)
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        description = CASE WHEN EXCLUDED.description = '' THEN bot_config.description
                                           ELSE EXCLUDED.description END,
                        updated_at = NOW()
                """,
                key, str_value, description,
            ),
            label="bot_config_set",
        )
        return True
    except Exception:
        logger.error("bot_config_set failed for key '%s'", key, exc_info=True)
        return False


async def bot_config_seed(defaults: dict[str, tuple[Any, str]]) -> int:
    """Insert defaults for any key not yet present.

    *defaults* maps key → (default_value, description).
    Returns the number of rows inserted.
    """
    inserted = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            for key, (value, description) in defaults.items():
                result = await conn.execute(
                    """
                    INSERT INTO bot_config (key, value, description, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (key) DO NOTHING
                    """,
                    key, str(value), description,
                )
                if result.endswith("1"):
                    inserted += 1
        if inserted:
            logger.info("bot_config: seeded %d default rows", inserted)
    except Exception:
        logger.error("bot_config_seed failed", exc_info=True)
    return inserted


# ---------------------------------------------------------------------------
# Admin-triggered json_store migration
# ---------------------------------------------------------------------------

def _mig_result(target: str, found: int, inserted: int, errors: int) -> dict:
    return {"target": target, "found": found, "inserted": inserted,
            "skipped": max(0, found - inserted - errors), "errors": errors}


def _count_inserted(result_str: str) -> int:
    """Parse asyncpg execute() result string 'INSERT 0 N' → N."""
    try:
        return int(result_str.split()[-1])
    except Exception:
        return 0


async def migrate_json_store_blobs(pool: asyncpg.Pool | None = None) -> dict[str, dict]:
    """Read every key from json_store and upsert records into the corresponding table.

    Returns a dict mapping each blob key to stats:
      {found, inserted, skipped, errors, target}

    Unknown keys are logged as warnings and appear with target=None.
    Fully idempotent — all inserts use ON CONFLICT DO NOTHING semantics.
    """
    if pool is None:
        pool = await get_pool()

    summary: dict[str, dict] = {}

    try:
        rows = await pool.fetch("SELECT key, value FROM json_store ORDER BY key")
    except Exception:
        logger.error("migrate_json_store_blobs: failed to query json_store", exc_info=True)
        return summary

    known_keys = {
        "attendance", "open_log", "last_payment", "rent_run", "last_rent",
        "cyberware_status", "cyberware_log", "cyberware_last_run", "cyberware_weekly",
        "system_status", "wholesaler_lots", "wholesaler_stores", "wholesaler_shops",
        "wholesaler_settings", "wholesaler_state", "thread_map",
    }

    for row in rows:
        key = row["key"]
        raw = row["value"]
        data = json.loads(raw) if isinstance(raw, str) else raw

        if key not in known_keys:
            logger.warning("migrate_json_store_blobs: unknown key '%s' — skipping", key)
            summary[key] = {"target": None, "found": None, "inserted": 0, "skipped": 0, "errors": 0}
            continue

        try:
            result = await _mig_dispatch(pool, key, data)
        except Exception:
            logger.error("migrate_json_store_blobs: handler for '%s' raised", key, exc_info=True)
            result = _mig_result("(error)", 0, 0, 1)

        summary[key] = result
        logger.info(
            "migrate_json_store[%s] → %s: found=%s inserted=%d skipped=%d errors=%d",
            key, result["target"], result["found"], result["inserted"],
            result["skipped"], result["errors"],
        )

    return summary


async def _mig_dispatch(pool: asyncpg.Pool, key: str, data) -> dict:
    """Route a json_store key to the appropriate migration handler."""
    if key == "attendance":
        return await _mig_attendance(pool, data)
    if key == "open_log":
        return await _mig_open_log(pool, data)
    if key == "last_payment":
        return await _mig_last_payment(pool, data)
    if key in ("rent_run", "last_rent"):
        return await _mig_rent_run(pool, data)
    if key in ("cyberware_status", "cyberware_log"):
        return await _mig_cyberware_status(pool, data)
    if key == "cyberware_last_run":
        return await _mig_cyberware_last_run(pool, data)
    if key == "cyberware_weekly":
        return await _mig_cyberware_weekly(pool, data)
    if key == "system_status":
        return await _mig_system_status(pool, data)
    if key == "wholesaler_lots":
        return await _mig_wholesaler_lots(pool, data)
    if key == "wholesaler_stores":
        return await _mig_wholesaler_stores(pool, data)
    if key == "wholesaler_shops":
        return await _mig_wholesaler_shops(pool, data)
    if key == "wholesaler_settings":
        return await _mig_wholesaler_settings(pool, data)
    if key == "wholesaler_state":
        return await _mig_wholesaler_state(pool, data)
    if key == "thread_map":
        return await _mig_thread_map(pool, data)
    return _mig_result("(unhandled)", 0, 0, 0)


async def _mig_attendance(pool: asyncpg.Pool, data) -> dict:
    target = "attendance_log"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for user_id, timestamps in data.items():
            if not isinstance(timestamps, list):
                continue
            for ts in timestamps:
                if not isinstance(ts, str):
                    continue
                found += 1
                try:
                    dt = datetime.fromisoformat(ts)
                    res = await conn.execute(
                        "INSERT INTO attendance_log (user_id, logged_at)"
                        " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        str(user_id), dt,
                    )
                    inserted += _count_inserted(res)
                except Exception:
                    errors += 1
                    logger.warning("_mig_attendance: row error user=%s ts=%s", user_id, ts, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_open_log(pool: asyncpg.Pool, data) -> dict:
    target = "business_open_log"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for user_id, timestamps in data.items():
            if not isinstance(timestamps, list):
                continue
            for ts in timestamps:
                if not isinstance(ts, str):
                    continue
                found += 1
                try:
                    dt = datetime.fromisoformat(ts)
                    res = await conn.execute(
                        "INSERT INTO business_open_log (user_id, opened_at)"
                        " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        str(user_id), dt,
                    )
                    inserted += _count_inserted(res)
                except Exception:
                    errors += 1
                    logger.warning("_mig_open_log: row error user=%s ts=%s", user_id, ts, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_last_payment(pool: asyncpg.Pool, data) -> dict:
    target = "last_payment"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for user_id, summary in data.items():
            found += 1
            if not isinstance(summary, str):
                summary = str(summary)
            try:
                res = await conn.execute(
                    "INSERT INTO last_payment (user_id, summary)"
                    " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    str(user_id), summary,
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_last_payment: row error user=%s", user_id, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_rent_run(pool: asyncpg.Pool, data) -> dict:
    target = "rent_runs"
    # rent_runs has only a SERIAL PK — use WHERE NOT EXISTS to stay idempotent.
    # Legacy format: {"last_run": "<iso>", ...}  OR a direct ISO string.
    found = inserted = errors = 0
    ts_str = None
    if isinstance(data, dict):
        ts_str = data.get("last_run") or data.get("run_at")
    elif isinstance(data, str):
        ts_str = data
    if ts_str:
        found = 1
        try:
            dt = datetime.fromisoformat(ts_str)
            res = await pool.execute(
                """
                INSERT INTO rent_runs (run_at, initiated_by)
                SELECT $1, 'migrated'
                WHERE NOT EXISTS (
                    SELECT 1 FROM rent_runs WHERE run_at = $1 AND initiated_by = 'migrated'
                )
                """,
                dt,
            )
            inserted += _count_inserted(res)
        except Exception:
            errors = 1
            logger.warning("_mig_rent_run: row error", exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_cyberware_status(pool: asyncpg.Pool, data) -> dict:
    target = "cyberware_status"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for user_id, v in data.items():
            if user_id == "_last_run":
                continue
            found += 1
            try:
                if isinstance(v, dict):
                    weeks = int(v.get("weeks", 0))
                    last_str = v.get("last")
                else:
                    weeks = int(v)
                    last_str = None
                last_dt = None
                if last_str:
                    try:
                        last_dt = datetime.fromisoformat(last_str)
                    except Exception:
                        pass
                res = await conn.execute(
                    "INSERT INTO cyberware_status (user_id, weeks, last_processed)"
                    " VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    str(user_id), weeks, last_dt,
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_cyberware_status: row error user=%s", user_id, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_cyberware_last_run(pool: asyncpg.Pool, data) -> dict:
    target = "cyberware_meta"
    # data may be an ISO string or {"last_run": "<iso>"}
    found = inserted = errors = 0
    ts_str = None
    if isinstance(data, str):
        ts_str = data
    elif isinstance(data, dict):
        ts_str = data.get("last_run") or data.get("value")
    if ts_str:
        found = 1
        try:
            res = await pool.execute(
                "INSERT INTO cyberware_meta (key, value)"
                " VALUES ('last_full_run', $1) ON CONFLICT DO NOTHING",
                str(ts_str),
            )
            inserted += _count_inserted(res)
        except Exception:
            errors = 1
            logger.warning("_mig_cyberware_last_run: row error", exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_cyberware_weekly(pool: asyncpg.Pool, data) -> dict:
    target = "cyberware_weekly_runs"
    # cyberware_weekly_runs has only a SERIAL PK — use WHERE NOT EXISTS to stay idempotent.
    if not isinstance(data, list):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            ts_str = entry.get("timestamp") or entry.get("run_at")
            if not ts_str:
                continue
            found += 1
            try:
                run_at = datetime.fromisoformat(ts_str)
                checkup = [str(x) for x in entry.get("checkup", [])]
                paid = [str(x) for x in entry.get("paid", [])]
                unpaid = [str(x) for x in entry.get("unpaid", [])]
                res = await conn.execute(
                    """
                    INSERT INTO cyberware_weekly_runs (run_at, checkup_ids, paid_ids, unpaid_ids)
                    SELECT $1, $2, $3, $4
                    WHERE NOT EXISTS (
                        SELECT 1 FROM cyberware_weekly_runs WHERE run_at = $1
                    )
                    """,
                    run_at, checkup, paid, unpaid,
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_cyberware_weekly: row error", exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_system_status(pool: asyncpg.Pool, data) -> dict:
    target = "system_settings"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for system_name, enabled in data.items():
            found += 1
            try:
                res = await conn.execute(
                    "INSERT INTO system_settings (system_name, enabled)"
                    " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    str(system_name), bool(enabled),
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_system_status: row error system=%s", system_name, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_wholesaler_lots(pool: asyncpg.Pool, data) -> dict:
    target = "wholesale_lots"
    if not isinstance(data, list):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for lot in data:
            if not isinstance(lot, dict):
                continue
            lot_id = lot.get("lot_id")
            if not lot_id:
                continue
            found += 1
            try:
                extra = {k: v for k, v in lot.items()
                         if k not in ("lot_id", "gun_name", "gun_level", "unit_cost", "qty_available")}
                res = await conn.execute(
                    "INSERT INTO wholesale_lots (lot_id, gun_name, gun_level, unit_cost, qty_available, data)"
                    " VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT DO NOTHING",
                    lot_id, str(lot.get("gun_name", "")), str(lot.get("gun_level", "")),
                    int(lot.get("unit_cost", 0)), int(lot.get("qty_available", 0)),
                    json.dumps(extra),
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_wholesaler_lots: row error lot=%s", lot_id, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_wholesaler_stores(pool: asyncpg.Pool, data) -> dict:
    target = "wholesaler_stores"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for store_id, store in data.items():
            if not isinstance(store, dict):
                continue
            found += 1
            try:
                owner_id = store.get("owner_id")
                extra = {k: v for k, v in store.items() if k != "owner_id"}
                res = await conn.execute(
                    "INSERT INTO wholesaler_stores (store_id, owner_id, data)"
                    " VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING",
                    str(store_id), str(owner_id) if owner_id is not None else None, json.dumps(extra),
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_wholesaler_stores: row error store=%s", store_id, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_wholesaler_shops(pool: asyncpg.Pool, data) -> dict:
    target = "wholesaler_shops"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for shop_key, shop_data in data.items():
            found += 1
            try:
                res = await conn.execute(
                    "INSERT INTO wholesaler_shops (shop_key, data)"
                    " VALUES ($1, $2::jsonb) ON CONFLICT DO NOTHING",
                    str(shop_key), json.dumps(shop_data),
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_wholesaler_shops: row error shop=%s", shop_key, exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_wholesaler_settings(pool: asyncpg.Pool, data) -> dict:
    target = "wholesaler_settings"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = 1
    inserted = errors = 0
    try:
        res = await pool.execute(
            "INSERT INTO wholesaler_settings (key, value)"
            " VALUES ('main', $1::jsonb) ON CONFLICT DO NOTHING",
            json.dumps(data),
        )
        inserted += _count_inserted(res)
    except Exception:
        errors = 1
        logger.warning("_mig_wholesaler_settings: row error", exc_info=True)
    return _mig_result(target, found, inserted, errors)


async def _mig_wholesaler_state(pool: asyncpg.Pool, data) -> dict:
    """Legacy combined wholesaler blob — dispatches sub-keys to individual handlers."""
    if not isinstance(data, dict):
        return _mig_result("wholesale_lots/stores/shops", 0, 0, 1)
    lots_res = await _mig_wholesaler_lots(pool, data.get("wholesale_lots", []))
    stores_res = await _mig_wholesaler_stores(pool, data.get("stores", {}))
    shops_res = await _mig_wholesaler_shops(pool, data.get("shop_registry", {}))
    settings = data.get("settings")
    if settings:
        await _mig_wholesaler_settings(pool, settings)
    total_found = (lots_res["found"] or 0) + (stores_res["found"] or 0) + (shops_res["found"] or 0)
    total_inserted = lots_res["inserted"] + stores_res["inserted"] + shops_res["inserted"]
    total_errors = lots_res["errors"] + stores_res["errors"] + shops_res["errors"]
    return _mig_result("wholesale_lots+stores+shops", total_found, total_inserted, total_errors)


async def _mig_thread_map(pool: asyncpg.Pool, data) -> dict:
    target = "dm_threads"
    if not isinstance(data, dict):
        return _mig_result(target, 0, 0, 1)
    found = inserted = errors = 0
    async with pool.acquire() as conn:
        for user_id, thread_id in data.items():
            found += 1
            try:
                res = await conn.execute(
                    "INSERT INTO dm_threads (user_id, thread_id)"
                    " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    str(user_id), int(thread_id),
                )
                inserted += _count_inserted(res)
            except Exception:
                errors += 1
                logger.warning("_mig_thread_map: row error user=%s", user_id, exc_info=True)
    return _mig_result(target, found, inserted, errors)


# ---------------------------------------------------------------------------
# Pool teardown
# ---------------------------------------------------------------------------

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
