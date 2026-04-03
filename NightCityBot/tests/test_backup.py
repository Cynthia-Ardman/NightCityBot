"""Tests for the backup/export system (db_backup and gdrive_backup modules)."""

import asyncio
import gzip
import json
import os
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class FakeRecord(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class FakeConnection:
    def __init__(self, tables_data=None):
        self._tables_data = tables_data or {}
        self._deleted = []
        self._inserted = {}

    async def fetch(self, query, *args):
        if "pg_tables" in query:
            return [FakeRecord(tablename=t) for t in self._tables_data.keys()]
        for table_name in self._tables_data:
            if f'"{table_name}"' in query or table_name in query:
                return [FakeRecord(r) for r in self._tables_data[table_name]]
        return []

    async def execute(self, query, *args):
        if query.strip().startswith("DELETE"):
            for t in self._tables_data:
                if t in query:
                    self._deleted.append(t)
        elif query.strip().startswith("INSERT"):
            for t in self._tables_data:
                if t in query:
                    self._inserted.setdefault(t, []).append(args)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakePool:
    def __init__(self, tables_data=None):
        self._conn = FakeConnection(tables_data or {})
        self._conn.transaction = lambda: FakeTransaction()

    def acquire(self):
        return self._conn


@pytest.fixture
def sample_tables():
    return {
        "json_store": [
            {"key": "test_key", "value": '{"data": 1}', "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc)},
        ],
        "bot_config": [
            {"key": "base_fee", "value": "500", "description": "Base fee", "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc)},
        ],
    }


class TestExportAllTables:
    def test_export_returns_metadata(self, sample_tables):
        from NightCityBot.utils.db_backup import export_all_tables

        pool = FakePool(sample_tables)
        result = asyncio.get_event_loop().run_until_complete(export_all_tables(pool))

        assert "metadata" in result
        assert "tables" in result
        assert len(result["tables"]) == 2

        table_names = [t["name"] for t in result["metadata"]["tables"]]
        assert "json_store" in table_names
        assert "bot_config" in table_names

    def test_export_serialises_dates(self, sample_tables):
        from NightCityBot.utils.db_backup import export_all_tables

        pool = FakePool(sample_tables)
        result = asyncio.get_event_loop().run_until_complete(export_all_tables(pool))

        row = result["tables"]["json_store"][0]
        assert isinstance(row["updated_at"], str)
        assert "2025" in row["updated_at"]

    def test_export_row_counts(self, sample_tables):
        from NightCityBot.utils.db_backup import export_all_tables

        pool = FakePool(sample_tables)
        result = asyncio.get_event_loop().run_until_complete(export_all_tables(pool))

        meta = {t["name"]: t["row_count"] for t in result["metadata"]["tables"]}
        assert meta["json_store"] == 1
        assert meta["bot_config"] == 1

    def test_export_skips_missing_tables(self):
        from NightCityBot.utils.db_backup import export_all_tables

        pool = FakePool({})
        result = asyncio.get_event_loop().run_until_complete(export_all_tables(pool))
        assert len(result["tables"]) == 0


class TestCompressDecompress:
    def test_roundtrip(self):
        from NightCityBot.utils.db_backup import compress_export, decompress_export

        data = {"tables": {"test": [{"id": 1}]}, "metadata": {"exported_at": "2025-01-01"}}
        compressed = compress_export(data)
        assert isinstance(compressed, bytes)
        assert len(compressed) > 0

        restored = decompress_export(compressed)
        assert restored == data


class TestSaveExportToFile:
    def test_creates_file(self, tmp_path):
        from NightCityBot.utils.db_backup import save_export_to_file

        data = {"tables": {}, "metadata": {"exported_at": "2025-01-01"}}
        path = save_export_to_file(data, output_dir=str(tmp_path))
        assert os.path.exists(path)
        assert path.endswith(".json.gz")

        with open(path, "rb") as f:
            content = gzip.decompress(f.read())
        restored = json.loads(content)
        assert restored == data


class TestImportAllTables:
    def test_import_known_tables(self, sample_tables):
        from NightCityBot.utils.db_backup import import_all_tables

        export_data = {
            "tables": {
                "json_store": [
                    {"key": "k1", "value": '{}', "updated_at": "2025-01-01T00:00:00+00:00"}
                ],
                "bot_config": [
                    {"key": "fee", "value": "100", "description": "d", "updated_at": "2025-01-01T00:00:00+00:00"}
                ],
            }
        }

        pool = FakePool(sample_tables)
        result = asyncio.get_event_loop().run_until_complete(
            import_all_tables(pool, export_data)
        )
        assert result["json_store"] == 1
        assert result["bot_config"] == 1

    def test_import_empty_raises(self):
        from NightCityBot.utils.db_backup import import_all_tables

        pool = FakePool({})
        with pytest.raises(ValueError, match="No table data"):
            asyncio.get_event_loop().run_until_complete(
                import_all_tables(pool, {"tables": {}})
            )


class TestCollectLocalBackupFiles:
    def test_collects_existing_files(self, tmp_path, monkeypatch):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "snap1.json").write_text('{"test": true}')

        mock_config = types.ModuleType("config")
        mock_config.BALANCE_BACKUP_DIR = str(backup_dir)
        mock_config.CHARACTER_BACKUP_DIR = str(tmp_path / "nonexistent")
        mock_config.RENT_AUDIT_DIR = str(tmp_path / "also_nonexistent")

        monkeypatch.setitem(sys.modules, "config", mock_config)

        from NightCityBot.utils.db_backup import collect_local_backup_files

        files = collect_local_backup_files()
        assert len(files) >= 1
        assert files[0]["label"] == "balance_backups"
        assert files[0]["name"] == "snap1.json"


class TestGDriveBackupModule:
    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("GDRIVE_SERVICE_ACCOUNT_JSON", raising=False)
        from NightCityBot.utils.gdrive_backup import _get_credentials

        with pytest.raises(RuntimeError, match="GDRIVE_SERVICE_ACCOUNT_JSON"):
            _get_credentials()

    def test_rotate_old_backups_mock(self, monkeypatch):
        from NightCityBot.utils import gdrive_backup

        old_backup = {
            "id": "old123",
            "name": "old_backup.json.gz",
            "createdTime": "2020-01-01T00:00:00Z",
            "size": "1024",
        }
        monkeypatch.setattr(gdrive_backup, "list_backups", lambda **kw: [old_backup])
        deleted_ids = []
        monkeypatch.setattr(gdrive_backup, "delete_file", lambda fid: deleted_ids.append(fid))

        result = gdrive_backup.rotate_old_backups(retention_days=30)
        assert "old_backup.json.gz" in result
        assert "old123" in deleted_ids

    def test_get_last_backup_empty(self, monkeypatch):
        from NightCityBot.utils import gdrive_backup

        monkeypatch.setattr(gdrive_backup, "list_backups", lambda **kw: [])
        assert gdrive_backup.get_last_backup() is None

    def test_get_last_backup_returns_first(self, monkeypatch):
        from NightCityBot.utils import gdrive_backup

        backups = [{"id": "abc", "name": "latest.gz"}]
        monkeypatch.setattr(gdrive_backup, "list_backups", lambda **kw: backups)
        assert gdrive_backup.get_last_backup()["id"] == "abc"
