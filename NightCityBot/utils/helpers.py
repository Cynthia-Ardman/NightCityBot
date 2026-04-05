from typing import Optional
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import config
from pathlib import Path
import json
import aiofiles
import logging
import os
import uuid

logger = logging.getLogger(__name__)

def build_channel_name(usernames, max_length=100):
    """Builds a Discord channel name for a group RP."""
    full_name = "text-rp-" + "-".join(f"{name}-{uid}" for name, uid in usernames)
    if len(full_name) <= max_length:
        return re.sub(r"[^a-z0-9\-]", "", full_name.lower())

    simple_name = "text-rp-" + "-".join(name for name, _ in usernames)
    if len(simple_name) > max_length:
        simple_name = simple_name[:max_length]

    return re.sub(r"[^a-z0-9\-]", "", simple_name.lower())

def safe_filename(name: str) -> str:
    """Return a filesystem-safe version of ``name``."""
    cleaned = re.sub(r"[\\/*?:\"<>|]", "", name).strip()
    return cleaned or "unnamed"

async def load_json_file(file_path: Path | str, default=None):
    """Safely load a JSON file with fallback to default value.

    `file_path` can be either a :class:`pathlib.Path` or a string path.
    """
    path = Path(file_path)
    try:
        if path.exists():
            async with aiofiles.open(path, 'r') as f:
                content = await f.read()
                if not content.strip():
                    return default if default is not None else {}
                return json.loads(content)
    except json.JSONDecodeError as e:
        # File had invalid JSON; treat as empty and log without traceback
        logger.error("Invalid JSON in %s: %s", path.name, e)
    except Exception as e:
        logger.exception("Error loading %s: %s", path.name, e)
    return default if default is not None else {}

async def save_json_file(file_path: Path | str, data):
    """Safely save data to a JSON file using an atomic replace."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        async with aiofiles.open(temp_path, 'w') as f:
            await f.write(json.dumps(data, indent=2))
        os.replace(temp_path, path)
        return True
    except Exception as e:
        logger.exception("Error saving %s: %s", path.name, e)
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed cleaning temp file for %s", path.name)
        return False

async def append_json_file(file_path: Path | str, item) -> bool:
    """Append an item to a JSON list file."""
    path = Path(file_path)
    entries = await load_json_file(path, default=[])
    if not isinstance(entries, list):
        entries = []
    entries.append(item)
    return await save_json_file(path, entries)

def get_tz_now() -> datetime:
    """Return current time in the configured timezone."""
    tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
    return datetime.now(tz)


MAX_SELECT_OPTIONS = 25


def truncation_note(total: int, kind: str = "items") -> str:
    """Return a note string if *total* exceeds the Discord 25-option limit."""
    if total > MAX_SELECT_OPTIONS:
        return f"\n⚠️ Showing first {MAX_SELECT_OPTIONS} of {total} {kind}."
    return ""


def format_gun_lines_grouped(lots, *, qty_key="qty_available", max_items=30):
    from NightCityBot.utils.constants import (
        GUN_CLASS_ORDER, GUN_CLASS_DISPLAY_NAMES, POWER_LEVEL_WORDS,
    )

    groups: dict[str, list] = {}
    for lot in lots:
        qty = int(lot.get(qty_key, 0))
        if qty <= 0:
            continue
        wt = (lot.get("weapon_type") or "").strip().lower()
        if wt not in GUN_CLASS_DISPLAY_NAMES:
            wt = "other"
        groups.setdefault(wt, []).append(lot)

    ordered_keys = [k for k in GUN_CLASS_ORDER if k in groups]
    if "other" in groups:
        ordered_keys.append("other")

    lines: list[str] = []
    row = 1
    for key in ordered_keys:
        if row > max_items:
            break
        header = GUN_CLASS_DISPLAY_NAMES.get(key, "Other")
        lines.append(f"\n▬▬ {header} ▬▬")
        for lot in groups[key]:
            if row > max_items:
                break
            qty = int(lot.get(qty_key, 0))
            restriction = (lot.get("restriction") or "basic").title()
            level_raw = lot.get("gun_level", "?")
            level = POWER_LEVEL_WORDS.get(level_raw, level_raw)
            gc = lot.get("gun_category") or "?"
            name = lot.get("gun_name", "?")
            cost = int(lot.get("unit_cost", 0))
            lines.append(
                f"`{row}.` **{name}** — [{restriction}] · [{level}] · [{gc}] — ${cost:,} × {qty}"
            )
            row += 1

    return lines


def format_cw_lines_grouped(lots, *, qty_key="qty_available", name_key="item_name",
                             cost_key="unit_cost", max_items=30, show_sold_out=False):
    from NightCityBot.utils.constants import CW_SLOT_ORDER, CW_SLOT_DISPLAY_NAMES

    groups: dict[str, list] = {}
    sold_out: list[dict] = []
    for lot in lots:
        qty = int(lot.get(qty_key, 0))
        if qty <= 0:
            if show_sold_out:
                sold_out.append(lot)
            continue
        slot = (lot.get("slot") or "").strip().lower()
        if slot not in CW_SLOT_DISPLAY_NAMES:
            slot = "other"
        groups.setdefault(slot, []).append(lot)

    ordered_keys = [k for k in CW_SLOT_ORDER if k in groups]
    if "other" in groups:
        ordered_keys.append("other")

    lines: list[str] = []
    row = 1
    for key in ordered_keys:
        if row > max_items:
            break
        header = CW_SLOT_DISPLAY_NAMES.get(key, "Other")
        lines.append(f"\n▬▬ {header} ▬▬")
        for lot in groups[key]:
            if row > max_items:
                break
            qty = int(lot.get(qty_key, 0))
            cwp = lot.get("cwp", "")
            name = lot.get(name_key, "?")
            cost = int(lot.get(cost_key, 0))
            cwp_tag = f" — [CWP: {cwp}]" if cwp else ""
            cost_tag = f" — ${cost:,} × {qty}" if cost else f" × {qty}"
            lines.append(f"`{row}.` **{name}**{cwp_tag}{cost_tag}")
            row += 1

    for lot in sold_out:
        if row > max_items:
            break
        lines.append(f"~~`{row}.` {lot.get(name_key, '?')}~~ — Sold out")
        row += 1

    return lines
