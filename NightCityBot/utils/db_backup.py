"""Read-only database export and import for NightCityBot backups.

CRITICAL: export_all_tables() is strictly read-only — SELECT queries only.
import_all_tables() is the ONLY write path and must only be called from the
restore command with explicit confirmation.
"""

import gzip
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ALL_TABLES = [
    "json_store",
    "attendance_log",
    "ticket_index",
    "business_open_log",
    "last_payment",
    "rent_runs",
    "system_settings",
    "cyberware_status",
    "cyberware_meta",
    "cyberware_weekly_runs",
    "dm_threads",
    "wholesale_lots",
    "wholesaler_stores",
    "wholesaler_shops",
    "wholesaler_pending_payouts",
    "wholesaler_settings",
    "wholesaler_transactions",
    "bot_config",
    "payment_labels",
    "cyberware_catalog",
    "gun_catalog",
    "player_inventory",
    "pending_transfers",
    "item_history",
]


def _serialise_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)
        else:
            out[k] = v
    return out


async def export_all_tables(pool) -> dict[str, Any]:
    export_data: dict[str, Any] = {
        "metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": [],
        },
        "tables": {},
    }

    async with pool.acquire() as conn:
        existing = set()
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        for r in rows:
            existing.add(r["tablename"])

        for table_name in ALL_TABLES:
            if table_name not in existing:
                logger.warning("Table %s not found, skipping", table_name)
                continue

            table_rows = await conn.fetch(f'SELECT * FROM "{table_name}"')
            serialised = [_serialise_row(dict(r)) for r in table_rows]
            export_data["tables"][table_name] = serialised
            export_data["metadata"]["tables"].append(
                {"name": table_name, "row_count": len(serialised)}
            )

    logger.info(
        "Exported %d table(s), total rows: %d",
        len(export_data["tables"]),
        sum(len(v) for v in export_data["tables"].values()),
    )
    return export_data


def compress_export(export_data: dict) -> bytes:
    raw = json.dumps(export_data, default=str, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw)


def decompress_export(data: bytes) -> dict:
    raw = gzip.decompress(data)
    return json.loads(raw.decode("utf-8"))


def save_export_to_file(export_data: dict, output_dir: str = "/tmp") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"nightcitybot_backup_{ts}.json.gz"
    filepath = os.path.join(output_dir, filename)
    compressed = compress_export(export_data)
    with open(filepath, "wb") as f:
        f.write(compressed)
    logger.info("Saved backup to %s (%d bytes)", filepath, len(compressed))
    return filepath


async def import_all_tables(pool, export_data: dict) -> dict[str, int]:
    tables_data = export_data.get("tables", {})
    if not tables_data:
        raise ValueError("No table data found in backup")

    imported: dict[str, int] = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            for table_name, rows in tables_data.items():
                if table_name not in ALL_TABLES:
                    logger.warning(
                        "Skipping unknown table %s from backup", table_name
                    )
                    continue

                await conn.execute(f'DELETE FROM "{table_name}"')

                if not rows:
                    imported[table_name] = 0
                    continue

                columns = list(rows[0].keys())
                col_list = ", ".join(f'"{c}"' for c in columns)
                placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
                insert_sql = (
                    f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})'
                )

                for row in rows:
                    values = [row.get(c) for c in columns]
                    try:
                        await conn.execute(insert_sql, *values)
                    except Exception:
                        logger.error(
                            "Failed to insert row into %s: %s",
                            table_name,
                            row,
                            exc_info=True,
                        )
                        raise

                imported[table_name] = len(rows)

    logger.info(
        "Imported %d table(s), total rows: %d",
        len(imported),
        sum(imported.values()),
    )
    return imported


def collect_local_backup_files() -> list[dict]:
    import config as _config

    files: list[dict] = []
    scan_dirs = [
        ("balance_backups", getattr(_config, "BALANCE_BACKUP_DIR", None)),
        ("sheet_backups", getattr(_config, "CHARACTER_BACKUP_DIR", None)),
        ("rent_audits", getattr(_config, "RENT_AUDIT_DIR", None)),
    ]

    for label, dir_path in scan_dirs:
        if dir_path is None:
            continue
        p = Path(dir_path)
        if not p.exists():
            continue
        for f in p.iterdir():
            if f.is_file():
                try:
                    files.append(
                        {
                            "label": label,
                            "name": f.name,
                            "path": str(f),
                            "size": f.stat().st_size,
                        }
                    )
                except Exception:
                    logger.warning("Could not read %s", f, exc_info=True)

    return files
