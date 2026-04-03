# NightCityBot

A Discord bot for NCRP (Cyberpunk-themed RP server) managing economy, roleplay utilities, and automated systems.

## Architecture

- **Entry point**: `NightCityBot/bot.py` — runs the Discord bot with Flask keep-alive server on port 5000
- **Config**: `config.py` (root) — all IDs, paths, and secrets loaded from env vars
- **Cogs**: `NightCityBot/cogs/` — modular command groups (economy, wholesaler, cyberware, etc.)
- **Services**: `NightCityBot/services/` — UnbelievaBoat API wrapper, Trauma Team logic
- **Utils**: `NightCityBot/utils/` — helpers, permissions, startup checks
- **Tests**: `NightCityBot/tests/` — pytest suite (coverage floor: 63%)

## Data Storage

Operational state is now persisted to **PostgreSQL** via the `json_store` table (key TEXT PK, value JSONB). File-based JSON storage is retained only for per-member balance backups.

### `bot_config` table — runtime-editable economy constants

All hardcoded dollar amounts (baseline living cost, housing/business/trauma rent tiers, attendance reward, cyberware max costs, tier-0 income scale, open-shop percent) are seeded into the `bot_config` table at startup and read via `NightCityBot/utils/config_loader.py`. Values in the DB override code defaults without requiring a redeploy. Admins can change values at runtime using `!config set <key> <value>` (fixer-only).

`config_loader.py` public getters: `get_baseline_living_cost`, `get_attend_reward`, `get_role_costs_housing`, `get_role_costs_business`, `get_trauma_role_costs`, `get_tier0_income_scale`, `get_open_percent`, `get_cyber_max_cost`. Lifecycle: `seed_and_reload()` at startup, `reload_config()` on demand.

### PostgreSQL `json_store` keys

| Key | Cog | Description |
|-----|-----|-------------|
| `open_log` | Economy | Business opening timestamps per user |
| `attend_log` | Economy | Attendance event timestamps per user |
| `last_rent` | Economy | Timestamp of last full rent collection |
| `last_payment` | Economy | Last payment summary per user |
| `cyberware_log` | Cyberware | Weekly streak data per member |
| `cyberware_weekly` | Cyberware | Append-only list of weekly processing results |
| `thread_map` | DMHandler | DM user ID → forum thread ID map |
| `system_status` | SystemControl | Enable/disable flags for each subsystem |
| `wholesaler_state` | Wholesaler | Full assembled wholesaler state (lots, stores, settings) |
| `wholesaler_tx` | Wholesaler | Append-only transaction log |
| `open_log_history_YYYY_MM` | Economy | Monthly archive of open_log before reset |

DB helpers: `NightCityBot/utils/db.py` — `get_pool()`, `db_load(key, default, seed_path)`, `db_save(key, value)`, `close_pool()`. On first `db_load` for a key not yet in DB, seeds automatically from the legacy JSON file on disk (one-time migration).

### DB Resilience (Task #3)

All write-path helpers in `db.py` are wrapped with `_with_retry()` — 2 automatic retries with exponential backoff on transient errors (`PostgresConnectionError`, `InterfaceError`, `TooManyConnectionsError`). Non-transient errors (constraint violations, etc.) propagate immediately.

Key utilities:
- `_with_retry(coro_factory, *, label, retries=2, delay=0.5)` — internal retry loop; increments `_db_failures` counter on exhaustion
- `get_failure_count() -> int` — cumulative write-failure counter since startup
- `db_ping() -> float | None` — runs `SELECT 1` and returns latency in ms (None on error)
- `warn_db_failure(bot, operation, detail)` — sends a Discord alert to the audit channel; call from cog code when the bot object is available

Startup health check: `startup_checks.py::check_db_health()` runs `db_ping()` after `wait_until_ready()` and posts an audit-channel alert if it fails.

Admin command: `!db_health` — shows DB ping, write-failure count, and pool stats (size/idle/min/max).

### Balance backup files (still file-based)

- `BALANCE_BACKUP_DIR/<member_id>.json` — per-member balance history
- `CHARACTER_BACKUP_DIR/` — character thread archive files

### Wholesaler data files (still written for audit reference)

- `data/wholesaler/state.json`, `stores.json`, `inventory/wholesale.json`, `inventory/stores/<store_id>.json`, `transactions.json`

## Key Dependencies

- discord.py, aiohttp, Flask, openpyxl, aiofiles, python-dotenv, rapidfuzz, asyncpg

## Wholesaler System Flow

1. `!wh_restock` — downloads Google Sheet, generates random weapon lots, saves to `state.json` + `wholesale.json`
2. `!wh_buy` — store owners buy lots from wholesaler, creates per-store files in `inventory/stores/`
3. `!wh_sell` — store owners sell weapons to players (syntax: `!wh_sell @buyer "character_name" <lot_id> <qty> <price>`; `!sell` kept as alias)

## Cyberware Shop & Weekly Wholesale

Two-table catalog system in PostgreSQL:
- `cyberware_catalog` (name UNIQUE, price, updated_at) — full item list, populated by `!cw_setsheet`
- Weekly wholesale lots stored as `cw_wholesale_lots` in `data/cyberware_shop/state.json`

### Cyberware Wholesale Flow (mirrors gun wholesaler)

1. Each Sunday (auto) or via `!cw_wh_restock`, 15 random items from the full catalog are selected with 1–3 qty each
2. Ripperdocs use `!cw_wh_list` to see what's available this week (numbered lots)
3. `!cw_buy <lot#> [qty]` — buy by lot number, first-come first-served
4. Sold-out items show ~~strikethrough~~ in the list; race conditions auto-refund
5. Auto-restock fires during the Sunday weekly cyberware process

### Ripperdoc Inventory Flow

1. `!cw_inventory` — shows numbered inventory rows (row# used by sell/install)
2. `!cw_sell @patient <row> <price> character_name` — sells to patient, creates `player_inventory` record
3. `!cw_install @patient <row> character_name` — free install (no payment), creates `player_inventory` record

### Admin commands
- `!cw_wh_restock [seed]` — force restock
- `!cw_wh_add <qty> <item>` — add/top-up a specific item mid-week
- `!cw_wh_remove <item>` — pull an item from current week
- `!cw_wh_settings [key] [value]` — tune total_items, qty_min, qty_max

## Player Inventory System (new in Task 11)

Tables: `player_inventory`, `pending_transfers`
Cog: `NightCityBot/cogs/player_inventory.py`

- `!my_inventory [@player] [page]` — view item inventory with row numbers
- `!inv_give @target <row> "sender_char" ["receiver_char"]` — transfer item (no payment)
- `!trade @buyer <row> <price> buyer_character` — sell item with DM confirmation (Accept/Decline buttons, 60s timeout); controlled/restricted blocked; DB failure → pending_transfers + alert to #nightcitybot-logs
- `!inv_add @player <type> "name" <restriction> "desc" [price]` — admin add item
- `!inv_remove @player <row>` — admin delete item
- `!inv_reassign @player <row> new_character` — admin character reassignment

Guns sold via `!guns_wh_sell` also write to `player_inventory` automatically.

## Unified Shop System (Task #14)

Consolidates separate command sets into interactive hub commands with Discord UI (dropdowns, buttons, modals), DM-confirmation trade flows, and a full per-item audit trail.

### New Cogs
- `NightCityBot/cogs/ripperdoc_hub.py` — `!ripperdoc` interactive panel (Buy/Sell/Install/Stock/Wholesale)
- `NightCityBot/cogs/gunstore_hub.py` — `!gunstore` interactive panel (Buy/Sell/Inventory/Approve/Unapprove/Wholesale/Approved Buyers)
- `NightCityBot/cogs/admin_shop.py` — `!admin_shop` admin panel (Add/Remove/Reassign/History/Inventory)

### Item History / Audit Trail
Table: `item_history` (keyed by item UUID, stores event_type, actor_id, target_id, price, metadata JSONB, created_at)
- `ih_record_event()` and `ih_get_history()` in `db.py`
- Event types: `created`, `wholesale_buy`, `player_sale`, `traded`, `given`, `admin_add`, `admin_remove`, `admin_reassign`, `cw_wholesale_buy`, `cw_sold`, `cw_installed`
- `!item_history <uuid>` command for lookup

### DM Confirmation Flow
All sell/trade operations with another player now send a DM to the buyer/patient with Accept/Decline buttons (60s timeout). Self-trades (same user, different characters) bypass DM confirmation.

### Deprecation Notices
Old commands (`!cw_buy`, `!cw_sell`, `!guns_wh_buy`, `!guns_wh_sell`) remain functional but docstrings now hint at the new hub commands.

### Test Coverage
- `NightCityBot/tests/test_unified_shop.py` — 56 tests covering all three new cogs, View interaction checks, button callbacks, DM confirm views, member resolution, log channels, timeouts, deprecation notices, cog registration
- Existing `test_player_inventory.py` updated for DM confirmation flow compatibility (51 tests)

### Audit log channels
- `CYBERWARE_LOG_CHANNEL_ID` — cyberware shop events
- `GUN_LOG_CHANNEL_ID` — gun shop events
- `GEAR_MISC_LOG_CHANNEL_ID` — player inventory trades/gives
- `NIGHTCITYBOT_LOG_CHANNEL_ID` — system alerts (pending transfers, admin actions)

## Gun Restriction System

Each weapon lot has a `restriction` field (default: `basic`):
- **basic** — anyone can buy, no special requirements
- **controlled** — buyer must be on the store owner's controlled-buyer list (`!wh_approve @user`)
- **restricted** — buyer must be on the controlled-buyer list AND an admin must approve the sale via audit channel reaction (5-minute timeout)

Store owner commands: `!wh_approve @user`, `!wh_unapprove @user`, `!wh_approved`
Admin commands:
- `!wh_add` and `!store_add` accept optional restriction parameter (e.g., `!wh_add "Nue" M 1300 5 controlled`)
- `!wh_remove <lot_id> [qty]` — remove a lot or reduce its quantity from the wholesaler
- `!store_remove @owner <lot_id> [qty]` — remove a lot or reduce its quantity from a store

Restrictions carry over from wholesaler to store when purchased via `!wh_buy`.
Controlled buyers list is persisted per-store in `inventory/stores/<store_id>.json`.
