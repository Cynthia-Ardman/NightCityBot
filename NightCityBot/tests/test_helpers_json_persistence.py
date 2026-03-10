import asyncio
import json
from pathlib import Path

from NightCityBot.utils import helpers


def test_save_json_file_is_roundtrip_and_overwrite_safe(tmp_path: Path):
    target = tmp_path / "nested" / "state.json"

    async def _run():
        ok1 = await helpers.save_json_file(target, {"wholesale_lots": [1], "stores": {"a": 1}})
        ok2 = await helpers.save_json_file(target, {"wholesale_lots": [2], "stores": {"b": 2}})
        loaded = await helpers.load_json_file(target, default={})
        return ok1, ok2, loaded

    ok1, ok2, loaded = asyncio.run(_run())

    assert ok1 is True
    assert ok2 is True
    assert loaded == {"wholesale_lots": [2], "stores": {"b": 2}}


def test_save_json_file_writes_valid_json(tmp_path: Path):
    target = tmp_path / "state.json"

    async def _run():
        return await helpers.save_json_file(target, {"stores": {"x": [1, 2, 3]}})

    assert asyncio.run(_run()) is True
    parsed = json.loads(target.read_text())
    assert parsed["stores"]["x"] == [1, 2, 3]
