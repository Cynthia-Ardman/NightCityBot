"""Async PostgreSQL connection pool and persistent KV store for NightCityBot."""
import asyncio
import asyncpg
import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None


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
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        logger.info("PostgreSQL connection pool created.")
        await _ensure_schema(_pool)
    return _pool


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    """Create required tables if they do not exist."""
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS json_store (
            key         TEXT PRIMARY KEY,
            value       JSONB NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_log (
            user_id     TEXT NOT NULL,
            logged_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, logged_at)
        )
        """
    )
    logger.info("DB schema verified (json_store + attendance_log tables ready).")
    await _seed_attendance_log_from_file(pool)


async def _seed_attendance_log_from_file(pool: asyncpg.Pool) -> None:
    """One-time import of the legacy attend_log JSON blob into attendance_log rows."""
    from datetime import datetime as _dt

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
                            dt = _dt.fromisoformat(ts)
                            await conn.execute(
                                "INSERT INTO attendance_log (user_id, logged_at)"
                                " VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                str(user_id),
                                dt,
                            )
                            inserted += 1
                        except Exception:
                            pass
        if inserted:
            logger.info("Seeded attendance_log with %d rows from %s", inserted, attend_file)
    except Exception as exc:
        logger.error("_seed_attendance_log_from_file failed: %s", exc)


async def db_load(key: str, default=None, seed_path: Path | str | None = None):
    """Load a JSONB blob from the database by key.

    If the key is absent and *seed_path* points to an existing JSON file the
    file is read once, stored in the database, and returned.  This provides a
    one-time migration from the old JSON-file storage so no data is lost.
    """
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT value FROM json_store WHERE key = $1", key
        )
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
                except Exception as exc:
                    logger.error(
                        "Failed to seed DB key '%s' from %s: %s", key, path, exc
                    )
    except Exception as exc:
        logger.error("db_load failed for key '%s': %s", key, exc)
    return default


async def db_save(key: str, value) -> bool:
    """Persist a JSON-serialisable value under *key* in the database."""
    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO json_store (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value      = EXCLUDED.value,
                    updated_at = NOW()
            """,
            key,
            json.dumps(value),
        )
        return True
    except Exception as exc:
        logger.error("db_save failed for key '%s': %s", key, exc)
        return False


async def attendance_get_user(user_id: str) -> list[str]:
    """Return ISO timestamp strings for all attendance records for *user_id*."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT logged_at FROM attendance_log WHERE user_id = $1 ORDER BY logged_at",
            user_id,
        )
        return [row["logged_at"].isoformat() for row in rows]
    except Exception as exc:
        logger.error("attendance_get_user failed for user '%s': %s", user_id, exc)
        return []


async def attendance_append(user_id: str, logged_at_iso: str) -> bool:
    """Insert a single attendance record for *user_id* (idempotent on conflict)."""
    from datetime import datetime

    try:
        pool = await get_pool()
        dt = datetime.fromisoformat(logged_at_iso)
        await pool.execute(
            "INSERT INTO attendance_log (user_id, logged_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id,
            dt,
        )
        return True
    except Exception as exc:
        logger.error("attendance_append failed for user '%s': %s", user_id, exc)
        return False


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
