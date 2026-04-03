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

Operational state is persisted to **PostgreSQL** via normalized tables (25 tables total, including `characters` and `item_history`). The legacy `json_store` key-value table remains for backward compatibility. File-based JSON storage is retained for per-member balance backups and wholesaler local fallback.

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

## Post-Merge Setup

`scripts/post-merge.sh` runs automatically after task agent merges to install dependencies. Configured via `.replit` `[postMerge]` section.

## Config cleanup notes

Removed dead constants: `TICKET_INDEX_FILE`, `WHOLESALER_RESTOCK_SCHEDULE`, `FIXER_ROLE_NAME` (only the ID is used).

## Key Dependencies

- discord.py, aiohttp, Flask, openpyxl, aiofiles, python-dotenv, rapidfuzz, asyncpg, google-api-python-client, google-auth

## Player Hub (`!player`)

Interactive panel for inventory management. Buttons: View Inventory, Trade Item, Sell to Store, Give Item.

- **Trade Item** — sell an item to another player with payment (DM confirmation, UnbelievaBoat balance transfer)
- **Give Item** — transfer an item for free (no payment, direct ownership transfer)
- **Sell to Store** — sell any gun to a gunstore owner. Player picks store owner (validated by `WHOLESALER_STORE_ROLE_IDS`), selects a gun from inventory, enters price. Store owner gets DM confirmation. On accept: payment transfers, item removed from player inventory, gun added as a lot to the store's state. Supports controlled/restricted guns. Compensation paths for save-state failures.

All flows use the UserSelect + item Select + Continue → modal pattern.

## Wholesaler System Flow

1. `!wh_restock` — downloads Google Sheet, generates random weapon lots, saves to `state.json` + `wholesale.json`
2. `!wh_buy` — store owners buy lots from wholesaler, creates per-store files in `inventory/stores/`
3. `!wh_sell` — store owners sell weapons to players (syntax: `!wh_sell @buyer "character_name" <lot_id> <qty> <price>`; `!sell` kept as alias)

## Cyberware Shop & Weekly Wholesale

Two-table catalog system in PostgreSQL:
- `cyberware_catalog` (name UNIQUE, price, updated_at) — full item list, populated by `!cw_setsheet`
- Weekly wholesale lots stored in PostgreSQL via the cyberware shop cog (local file fallback in `data/cyberware_shop/state.json`)

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

## Character Ownership System (Task #16)

Inventory is now owned by characters, not directly by Discord users. A Discord user can have multiple characters; each character belongs to exactly one user.

### Database
- `characters` table: `character_id` (PK), `discord_user_id`, `character_name`, `normalized_character_name`, `status` (active/inactive), `created_at`, `updated_at`, `deactivated_at`, `reactivated_at`
- Unique constraint on `(discord_user_id, normalized_character_name)`; indexes on `discord_user_id`, `status`, `(discord_user_id, status)`
- `player_inventory` gained a `character_id TEXT` column (indexed) linking items to characters

### Service Module
`NightCityBot/utils/characters.py` — CRUD for characters:
- `create_character(discord_user_id, character_name)` — creates with validation, returns None on duplicate
- `deactivate_character(character_id)` / `reactivate_character(character_id)` — soft status toggle
- `get_active_characters(discord_user_id)` / `get_inactive_characters(discord_user_id)`
- `get_character(character_id)`
- `normalize_name(name)` — strip + lowercase
- `validate_name(name)` — empty/whitespace rejected, max 64 chars

### Migration
`migrate_inventory_to_characters()` in `db.py` — creates a "Legacy Character" for each distinct `owner_id` in `player_inventory` with NULL `character_id`, then backfills. Transaction-wrapped per owner, idempotent, handles concurrent races safely. Runs automatically at startup after schema creation.

### Updated `pi_*` functions
- `pi_add_item` accepts optional `character_id` in the item dict
- `pi_get_by_owner` accepts optional `character_id` keyword filter
- `pi_update_owner` accepts optional `new_character_id` keyword; preserves existing `character_id` when omitted (backward compatible with all existing callers)
- `ih_record_event` accepts optional `character_id` keyword (stored in metadata JSONB)

### Tests
`NightCityBot/tests/test_characters.py` — 38 tests covering character CRUD, validation, normalization, migration (including concurrency race), pi_* character_id handling, and ih_record_event character_id metadata.

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
- `NightCityBot/cogs/fixer_hub.py` — `!fixer` Fixer management panel with three-tier menu (Player/Store/Wholesaler sub-menus for inventory, items, LOA, store stock, wholesale management)
- `NightCityBot/cogs/player_hub.py` — `!player` Player hub for viewing inventory, trading items, and giving items (replaces individual `!trade`, `!inv_give` commands in help)

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

## Google Drive Database Backups (Task #15)

Automated and on-demand full database backups to Google Drive for disaster recovery.

### Modules
- `NightCityBot/utils/db_backup.py` — read-only database export (`export_all_tables()`) and restore (`import_all_tables()`). Export uses SELECT-only queries against all tables; import is the only write path.
- `NightCityBot/utils/gdrive_backup.py` — Google Drive API wrapper using service account credentials (upload, download, list, delete, rotate).
- `NightCityBot/cogs/backup.py` — Discord cog with `!backup_now`, `!backup_status`, `!restore_db` commands (all Fixer-only) and automated daily backup via `discord.ext.tasks`.

### Commands
| Command | Description |
|---|---|
| `!backup_now` | Immediate full backup to Google Drive |
| `!backup_status` | Show last backup time, size, Drive link |
| `!restore_db` | List available backups |
| `!restore_db <id>` | Restore from backup (requires CONFIRM) |

### Backup Contents
Each backup bundles: all database tables (compressed JSON), plus local files from `backups/`, `sheet_backups/`, `rent_audits/`.

### Environment Secrets Required
- `GDRIVE_SERVICE_ACCOUNT_JSON` — full service account credentials JSON
- `GDRIVE_BACKUP_FOLDER_ID` — Google Drive folder ID
- Optional: `BACKUP_RETENTION_DAYS` (default 30), `BACKUP_HOUR` (default 4), `BACKUP_MINUTE` (default 0)

Setup guide: `BACKUP_SETUP.md`

### Dependencies
- `google-api-python-client`, `google-auth` (added to `requirements.txt`)

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
