"""Tests for NightCityBot/utils/characters.py — character service module."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from NightCityBot.utils.characters import (
    normalize_name,
    validate_name,
    create_character,
    deactivate_character,
    reactivate_character,
    get_active_characters,
    get_inactive_characters,
    get_character,
    MAX_NAME_LENGTH,
)


def _run(coro):
    return asyncio.run(coro)


class TestNormalizeName:
    def test_strips_whitespace(self):
        assert normalize_name("  V  ") == "v"

    def test_lowercases(self):
        assert normalize_name("Johnny Silverhand") == "johnny silverhand"

    def test_empty(self):
        assert normalize_name("") == ""

    def test_mixed_whitespace(self):
        assert normalize_name("\t  Hello World \n") == "hello world"


class TestValidateName:
    def test_valid_name(self):
        ok, msg = validate_name("V")
        assert ok is True
        assert msg == ""

    def test_empty_rejected(self):
        ok, msg = validate_name("")
        assert ok is False
        assert "empty" in msg.lower()

    def test_whitespace_only_rejected(self):
        ok, msg = validate_name("   ")
        assert ok is False
        assert "empty" in msg.lower()

    def test_too_long_rejected(self):
        ok, msg = validate_name("A" * (MAX_NAME_LENGTH + 1))
        assert ok is False
        assert "64" in msg

    def test_exact_max_length_ok(self):
        ok, _ = validate_name("A" * MAX_NAME_LENGTH)
        assert ok is True


class TestCreateCharacter:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _run(create_character("user1", ""))

    def test_whitespace_name_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _run(create_character("user1", "   "))

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="64"):
            _run(create_character("user1", "X" * 65))

    def test_success_returns_dict(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await create_character("user1", "  Johnny Silverhand  ")
            assert result is not None
            assert result["character_name"] == "Johnny Silverhand"
            assert result["normalized_character_name"] == "johnny silverhand"
            assert result["discord_user_id"] == "user1"
            assert result["status"] == "active"
            assert result["character_id"]
            assert result["deactivated_at"] is None
            assert result["reactivated_at"] is None

        _run(_test())

    def test_duplicate_returns_none(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(
            side_effect=Exception("duplicate key value violates unique constraint")
        )

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await create_character("user1", "V")
            assert result is None

        _run(_test())


class TestDeactivateCharacter:
    def test_success(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await deactivate_character("char-id-1")
            assert result is True

        _run(_test())

    def test_already_inactive(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 0")

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await deactivate_character("char-id-1")
            assert result is False

        _run(_test())

    def test_db_error_returns_false(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(side_effect=RuntimeError("db down"))

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                with patch("NightCityBot.utils.characters._with_retry", side_effect=RuntimeError("db down")):
                    result = await deactivate_character("char-id-1")
            assert result is False

        _run(_test())


class TestReactivateCharacter:
    def test_success(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await reactivate_character("char-id-1")
            assert result is True

        _run(_test())

    def test_already_active(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 0")

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await reactivate_character("char-id-1")
            assert result is False

        _run(_test())


class TestGetActiveCharacters:
    def test_returns_list(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        mock_row = {
            "character_id": "c1",
            "discord_user_id": "u1",
            "character_name": "V",
            "normalized_character_name": "v",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "deactivated_at": None,
            "reactivated_at": None,
        }
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[mock_row])

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await get_active_characters("u1")
            assert len(result) == 1
            assert result[0]["character_name"] == "V"
            assert result[0]["status"] == "active"

        _run(_test())

    def test_empty(self):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await get_active_characters("u1")
            assert result == []

        _run(_test())


class TestGetInactiveCharacters:
    def test_returns_list(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        mock_row = {
            "character_id": "c1",
            "discord_user_id": "u1",
            "character_name": "V",
            "normalized_character_name": "v",
            "status": "inactive",
            "created_at": now,
            "updated_at": now,
            "deactivated_at": now,
            "reactivated_at": None,
        }
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[mock_row])

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await get_inactive_characters("u1")
            assert len(result) == 1
            assert result[0]["status"] == "inactive"

        _run(_test())


class TestGetCharacter:
    def test_found(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        mock_row = {
            "character_id": "c1",
            "discord_user_id": "u1",
            "character_name": "V",
            "normalized_character_name": "v",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "deactivated_at": None,
            "reactivated_at": None,
        }
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=mock_row)

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await get_character("c1")
            assert result is not None
            assert result["character_id"] == "c1"

        _run(_test())

    def test_not_found(self):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await get_character("nonexistent")
            assert result is None

        _run(_test())

    def test_db_error_returns_none(self):
        async def _test():
            with patch("NightCityBot.utils.characters.get_pool", new=AsyncMock(side_effect=RuntimeError("db"))):
                result = await get_character("c1")
            assert result is None

        _run(_test())


class TestMigrateInventoryToCharacters:
    def test_no_null_rows_returns_zero(self):
        from NightCityBot.utils.db import migrate_inventory_to_characters

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        async def _test():
            result = await migrate_inventory_to_characters(mock_pool)
            assert result == 0

        _run(_test())

    def test_creates_legacy_characters(self):
        from NightCityBot.utils.db import migrate_inventory_to_characters

        def _make_conn_ctx():
            mock_conn = MagicMock()
            captured_id = {}

            async def _fake_execute(sql, *args):
                if "INSERT INTO characters" in sql:
                    captured_id["id"] = args[0]
                return "INSERT 0 1"

            async def _fake_fetchrow(sql, *args):
                if captured_id:
                    return {"character_id": captured_id["id"]}
                return None

            mock_conn.execute = AsyncMock(side_effect=_fake_execute)
            mock_conn.fetchrow = AsyncMock(side_effect=_fake_fetchrow)
            mock_tx = MagicMock()
            mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
            mock_tx.__aexit__ = AsyncMock(return_value=False)
            mock_conn.transaction = MagicMock(return_value=mock_tx)
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            return mock_ctx

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[
            {"owner_id": "user1"},
            {"owner_id": "user2"},
        ])
        mock_pool.acquire = MagicMock(side_effect=lambda: _make_conn_ctx())
        mock_pool.fetchval = AsyncMock(return_value=0)

        async def _test():
            result = await migrate_inventory_to_characters(mock_pool)
            assert result == 2

        _run(_test())

    def test_idempotent_existing_legacy(self):
        from NightCityBot.utils.db import migrate_inventory_to_characters

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value={"character_id": "existing-char-id"})
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[
            {"owner_id": "user1"},
        ])
        mock_pool.acquire = MagicMock(return_value=mock_ctx)
        mock_pool.fetchval = AsyncMock(return_value=0)

        async def _test():
            result = await migrate_inventory_to_characters(mock_pool)
            assert result == 0
            update_calls = [
                c for c in mock_conn.execute.await_args_list
                if "UPDATE player_inventory" in str(c)
            ]
            assert len(update_calls) == 1

        _run(_test())

    def test_concurrent_race_uses_persisted_id(self):
        from NightCityBot.utils.db import migrate_inventory_to_characters

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            None,
            {"character_id": "other-worker-id"},
        ])
        mock_conn.execute = AsyncMock(return_value="INSERT 0 0")

        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[
            {"owner_id": "user1"},
        ])
        mock_pool.acquire = MagicMock(return_value=mock_ctx)
        mock_pool.fetchval = AsyncMock(return_value=0)

        async def _test():
            result = await migrate_inventory_to_characters(mock_pool)
            assert result == 0
            update_call = [
                c for c in mock_conn.execute.await_args_list
                if "UPDATE player_inventory" in str(c)
            ]
            assert len(update_call) == 1
            assert "other-worker-id" in str(update_call[0])

        _run(_test())


class TestIhRecordEventCharacterId:
    def test_character_id_in_metadata(self):
        import json
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import ih_record_event
                result = await ih_record_event(
                    "item-1", "purchase",
                    actor_id="user1",
                    character_id="char-1",
                    metadata={"note": "test"},
                )
            assert result is True
            call_args = mock_pool.execute.call_args
            meta_json = call_args[0][6]
            meta = json.loads(meta_json)
            assert meta["character_id"] == "char-1"
            assert meta["note"] == "test"

        _run(_test())

    def test_no_character_id_no_key(self):
        import json
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import ih_record_event
                result = await ih_record_event("item-1", "purchase")
            assert result is True
            call_args = mock_pool.execute.call_args
            meta_json = call_args[0][6]
            meta = json.loads(meta_json)
            assert "character_id" not in meta

        _run(_test())


class TestPiAddItemCharacterId:
    def test_character_id_passed_through(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_add_item
                result = await pi_add_item({
                    "item_id": "i1",
                    "owner_id": "u1",
                    "name": "Katana",
                    "character_id": "char-1",
                })
            assert result is True
            call_args = mock_pool.execute.call_args
            assert call_args[0][-1] == "char-1"

        _run(_test())

    def test_no_character_id_passes_none(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_add_item
                result = await pi_add_item({
                    "item_id": "i1",
                    "owner_id": "u1",
                    "name": "Katana",
                })
            assert result is True
            call_args = mock_pool.execute.call_args
            assert call_args[0][-1] is None

        _run(_test())


class TestPiGetByOwnerCharacterIdFilter:
    def test_without_filter(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[{
            "item_id": "i1", "owner_id": "u1", "character_name": "V",
            "item_type": "gun", "name": "Katana", "restriction": "basic",
            "description": "", "price_paid": 100, "seller_id": None,
            "seller_name": "", "acquired_at": now, "created_at": now,
            "character_id": "c1",
        }])

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_get_by_owner
                result = await pi_get_by_owner("u1")
            assert len(result) == 1
            assert result[0]["character_id"] == "c1"
            call_sql = mock_pool.fetch.call_args[0][0]
            assert "character_id = $2" not in call_sql

        _run(_test())

    def test_with_filter(self):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_get_by_owner
                await pi_get_by_owner("u1", character_id="c1")
            call_sql = mock_pool.fetch.call_args[0][0]
            assert "character_id = $2" in call_sql

        _run(_test())


class TestPiUpdateOwnerCharacterId:
    def test_without_character_id_preserves_existing(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_update_owner
                result = await pi_update_owner("i1", "new_owner", "NewChar", "old_owner")
            assert result is True
            call_sql = mock_pool.execute.call_args[0][0]
            assert "character_id" not in call_sql

        _run(_test())

    def test_with_character_id(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        async def _test():
            with patch("NightCityBot.utils.db.get_pool", new=AsyncMock(return_value=mock_pool)):
                from NightCityBot.utils.db import pi_update_owner
                result = await pi_update_owner(
                    "i1", "new_owner", "NewChar", "old_owner",
                    new_character_id="char-new",
                )
            assert result is True
            call_sql = mock_pool.execute.call_args[0][0]
            assert "character_id = $5" in call_sql
            call_args = mock_pool.execute.call_args[0]
            assert call_args[-1] == "char-new"

        _run(_test())
