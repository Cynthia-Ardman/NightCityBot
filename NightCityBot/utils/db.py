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
    logger.info("DB schema verified (json_store table ready).")


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
    return default if default is not None else {}


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


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
