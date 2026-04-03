import logging
import uuid
from datetime import datetime, timezone

from NightCityBot.utils.db import get_pool, _with_retry

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64


def normalize_name(name: str) -> str:
    return name.strip().lower()


def validate_name(name: str) -> tuple[bool, str]:
    stripped = name.strip()
    if not stripped:
        return False, "Character name cannot be empty or whitespace-only."
    if len(stripped) > MAX_NAME_LENGTH:
        return False, f"Character name must be at most {MAX_NAME_LENGTH} characters."
    return True, ""


async def create_character(discord_user_id: str, character_name: str) -> dict | None:
    valid, err = validate_name(character_name)
    if not valid:
        raise ValueError(err)

    stripped = character_name.strip()
    norm = normalize_name(character_name)
    char_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        pool = await get_pool()
        await _with_retry(
            lambda: pool.execute(
                """
                INSERT INTO characters
                    (character_id, discord_user_id, character_name,
                     normalized_character_name, status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'active', $5, $5)
                """,
                char_id,
                str(discord_user_id),
                stripped,
                norm,
                now,
            ),
            label="create_character",
        )
        return {
            "character_id": char_id,
            "discord_user_id": str(discord_user_id),
            "character_name": stripped,
            "normalized_character_name": norm,
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "deactivated_at": None,
            "reactivated_at": None,
        }
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            logger.warning(
                "create_character: duplicate name '%s' for user '%s'",
                stripped,
                discord_user_id,
            )
            return None
        logger.error("create_character failed", exc_info=True)
        raise


async def deactivate_character(character_id: str) -> bool:
    try:
        now = datetime.now(timezone.utc)
        pool = await get_pool()
        result = await _with_retry(
            lambda: pool.execute(
                """
                UPDATE characters
                SET status = 'inactive', deactivated_at = $2, updated_at = $2
                WHERE character_id = $1 AND status = 'active'
                """,
                str(character_id),
                now,
            ),
            label="deactivate_character",
        )
        rows = int(result.split()[-1]) if result else 0
        return rows > 0
    except Exception:
        logger.error("deactivate_character failed for '%s'", character_id, exc_info=True)
        return False


async def reactivate_character(character_id: str) -> bool:
    try:
        now = datetime.now(timezone.utc)
        pool = await get_pool()
        result = await _with_retry(
            lambda: pool.execute(
                """
                UPDATE characters
                SET status = 'active', reactivated_at = $2, updated_at = $2
                WHERE character_id = $1 AND status = 'inactive'
                """,
                str(character_id),
                now,
            ),
            label="reactivate_character",
        )
        rows = int(result.split()[-1]) if result else 0
        return rows > 0
    except Exception:
        logger.error("reactivate_character failed for '%s'", character_id, exc_info=True)
        return False


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("created_at", "updated_at", "deactivated_at", "reactivated_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


async def get_active_characters(discord_user_id: str) -> list[dict]:
    try:
        pool = await get_pool()
        rows = await _with_retry(
            lambda: pool.fetch(
                """
                SELECT character_id, discord_user_id, character_name,
                       normalized_character_name, status,
                       created_at, updated_at, deactivated_at, reactivated_at
                FROM characters
                WHERE discord_user_id = $1 AND status = 'active'
                ORDER BY character_name
                """,
                str(discord_user_id),
            ),
            label="get_active_characters",
        )
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.error("get_active_characters failed for '%s'", discord_user_id, exc_info=True)
        return []


async def get_inactive_characters(discord_user_id: str) -> list[dict]:
    try:
        pool = await get_pool()
        rows = await _with_retry(
            lambda: pool.fetch(
                """
                SELECT character_id, discord_user_id, character_name,
                       normalized_character_name, status,
                       created_at, updated_at, deactivated_at, reactivated_at
                FROM characters
                WHERE discord_user_id = $1 AND status = 'inactive'
                ORDER BY character_name
                """,
                str(discord_user_id),
            ),
            label="get_inactive_characters",
        )
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.error("get_inactive_characters failed for '%s'", discord_user_id, exc_info=True)
        return []


async def get_character(character_id: str) -> dict | None:
    try:
        pool = await get_pool()
        row = await _with_retry(
            lambda: pool.fetchrow(
                """
                SELECT character_id, discord_user_id, character_name,
                       normalized_character_name, status,
                       created_at, updated_at, deactivated_at, reactivated_at
                FROM characters
                WHERE character_id = $1
                """,
                str(character_id),
            ),
            label="get_character",
        )
        if row is None:
            return None
        return _row_to_dict(row)
    except Exception:
        logger.error("get_character failed for '%s'", character_id, exc_info=True)
        return None
