import asyncio
import asyncpg
import os
import re

import pytest


EXPECTED_TABLES = [
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
    "characters",
    "player_inventory",
    "pending_transfers",
    "item_history",
    "cw_shop_state",
    "fixer_event",
    "store_inventory",
    "shop_permitted_roles",
]


def _parse_tables_from_source() -> list[str]:
    here = os.path.dirname(__file__)
    db_path = os.path.join(here, "..", "utils", "db.py")
    with open(db_path) as f:
        source = f.read()
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", source)


@pytest.fixture(scope="module")
def dev_tables():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set — cannot verify dev schema")

    async def fetch():
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return {r["tablename"] for r in rows}
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(fetch())
    finally:
        loop.close()


class TestDevSchemaSync:
    def test_all_expected_tables_exist_in_dev(self, dev_tables):
        missing = [t for t in EXPECTED_TABLES if t not in dev_tables]
        assert not missing, (
            f"Tables defined in db.py but missing from dev database: {missing}. "
            f"Run the bot once or create them manually before deploying."
        )

    def test_source_tables_match_expected_list(self):
        parsed = _parse_tables_from_source()
        expected_set = set(EXPECTED_TABLES)
        parsed_set = set(parsed)
        missing_from_list = parsed_set - expected_set
        assert not missing_from_list, (
            f"New tables found in db.py but not in EXPECTED_TABLES: {missing_from_list}. "
            f"Add them to EXPECTED_TABLES in test_schema_sync.py."
        )
