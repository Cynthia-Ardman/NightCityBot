# NightCityBot

A Discord bot for NCRP (Cyberpunk-themed RP server) managing economy, roleplay utilities, and automated systems.

## Architecture

- **Entry point**: `NightCityBot/bot.py` — runs the Discord bot with Flask keep-alive server on port 5000
- **Config**: `config.py` (root) — all IDs, paths, and secrets loaded from env vars
- **Cogs**: `NightCityBot/cogs/` — modular command groups (economy, gun store, cyberware, etc.)
- **Services**: `NightCityBot/services/` — UnbelievaBoat API wrapper, Trauma Team logic
- **Utils**: `NightCityBot/utils/` — helpers, permissions, startup checks
- **Tests**: `NightCityBot/tests/` — pytest suite (coverage floor: 63%)

## Data Storage

Operational state is persisted to **PostgreSQL** via normalized tables (29 tables total, including `characters`, `item_history`, `cw_shop_state`, `fixer_event`, `store_inventory`, and `shop_permitted_roles`). The legacy `json_store` key-value table remains for backward compatibility. File-based JSON storage is retained for per-member balance backups.

### `bot_config` table — runtime-editable economy constants

All hardcoded dollar amounts (baseline living cost, housing/business/trauma rent tiers, Xanadu Gold premium membership fee, attendance reward, cyberware max costs, tier-0 income scale, open-shop percent) are seeded into the `bot_config` table at startup and read via `NightCityBot/utils/config_loader.py`. Values in the DB override code defaults without requiring a redeploy. Admins can change values at runtime using `!config set <key> <value>` (fixer-only). Members holding the Xanadu Gold role (`config.XANADU_GOLD_ROLE_ID`) are charged `xanadu_gold_cost` ($500 default) on the 1st of each month as part of the monthly rent run; the fee is skipped on LOA and can be toggled with `!disable_system xanadu_gold`.

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
| `cw_shop_state` | Cyberware | **Legacy key, auto-migrated to `cw_shop_state` table on first load** |
| `fixer_event` | Economy | **Legacy key, auto-migrated to `fixer_event` table on first load** |
| `open_log_history_YYYY_MM` | Economy | Monthly archive of open_log before reset |

DB helpers: `NightCityBot/utils/db.py` — `get_pool()`, `db_load(key, default, seed_path)`, `db_save(key, value)`, `close_pool()`, `open_log_delete_today(user_id, opened_at)` (rollback helper for failed open_shop rewards). On first `db_load` for a key not yet in DB, seeds automatically from the legacy JSON file on disk (one-time migration). Typed helpers `cw_shop_state_load/save` and `fixer_event_load/save` replace `db_load`/`db_save` usage for those keys and auto-migrate from `json_store` on first access. Gun store tables use promoted typed columns (`weapon_type`, `restriction`, `store_name`, `balance`, `item_name`, `qty`, `cost`, etc.) alongside the `data` JSONB fallback. Join tables `store_inventory` and `shop_permitted_roles` normalize nested arrays.

`pi_delete_item(item_id, *, expected_owner_id=None)` — accepts an optional `expected_owner_id` kwarg; when provided, the SQL uses `AND owner_id = $2` as a TOCTOU guard so a stale caller cannot delete another player's item. All player-facing callers (give, sell-to-store) pass the current user's ID; admin callers (fixer remove) pass the target player's ID.

Seller refund convention: all rollback/refund paths that claw back money from the seller must use `{"bank": -price}`, never `{"cash": -price}`, to avoid pushing the seller's cash balance negative.

Balance deduction convention: all payment flows must use the cash+bank split pattern — fetch the balance with `get_balance()`, check total affordability, then `min(max(cash, 0), total)` for the cash portion and `max(0, total - cash_deducted)` for the bank portion. Never deduct from cash-only as this fails for users with funds in bank. All three Fixer add-item flows (player, gun store, cyberware store) follow this pattern.

Partial failure refund convention: if money is deducted but item insertion fails (partial or total), refund the failed portion proportionally — `price * failed_qty` — using the same cash/bank split that was originally deducted. The store add flows refund fully and abort if `_save_state` or `_save_inventory` returns False.

Panel failure logging convention: `log_panel_failure(bot, channel_id_attr, action, user, reason)` in `interaction_safety.py` sends a `⚠️ **Panel Failure**` message to the appropriate log channel when a user is denied access or a critical transaction fails. Wired into interaction_check denials (Gun Store, Ripperdoc, Fixer, Admin panels), owner-only action denials (catalogue buy, manage store/employees), and key transaction failures (payment failed, store disappeared, save error). The call is always fire-and-forget after the user has already been notified — never blocks the user flow. Channel routing: Gun Store → `GUN_LOG_CHANNEL_ID`, Ripperdoc → `CYBERWARE_LOG_CHANNEL_ID`, Fixer/Admin → `NIGHTCITYBOT_LOG_CHANNEL_ID`.

Inventory restore convention: when `pi_add_item` fails in a sell flow, the item must be restored to the store's stock (gun lot or ripperdoc inventory) before refunding money. `cyberware_shop.py` adds to player inventory first then removes from stock; hub sell flows (`gunstore_hub.py`, `ripperdoc_hub.py`) remove first and restore on failure.

Item field propagation convention: gun attributes use three distinct fields — `gun_category` (damage type: Power/Smart/Tech), `weapon_type` (gun class: pistol/revolver/shotgun etc.), and `gun_level` (L/M/H, the power level). These must be carried through the entire chain — Google Sheet → catalog → store lots → player inventory (as `power_level` low/medium/high, `weapon_subtype` from `gun_category`, `weapon_type` for gun class). All gun list display surfaces (store inventory, fixer stock view) group guns by `weapon_type` with section headers (▬▬ Pistols ▬▬ etc.) and use the format: `Name — [Restriction] · [Level] · [DamageType] — $Price × Qty`. Power level is displayed as full words (Low/Medium/High). Guns without a weapon_type appear in an "Other" section. Shared constants in `NightCityBot/utils/constants.py`: `VALID_GUN_CLASSES`, `GUN_CLASS_DISPLAY_NAMES`, `GUN_CLASS_ORDER`, `POWER_LEVEL_WORDS`. Shared formatter: `NightCityBot/utils/helpers.py::format_gun_lines_grouped()`. Fixer add flows (store, player add-item) now collect `weapon_type` (gun class) interactively or inline. The `player_inventory` DB table includes a `weapon_type` column propagated through buy flows. Cyberware attributes (`cwp` integer, `slot` body location) must likewise propagate — Google Sheet → catalog → ripperdoc inventory → player inventory. All cyberware list display surfaces (ripperdoc store inventory, fixer stock view, `!cw_inventory`) group cyberware by `slot` with section headers (▬▬ Neural ▬▬ etc.) and use the format: `Name — [CWP: X] — $Price × Qty`. Cyberware has NO restriction tag. Slots follow a body-logical display order (Neural → Ocular → Auditory → Integumentary → Hands & Feet → Arms → Legs → Skeleton & Torso → Universal Muscular → Circulatory & Immune → Miscellaneous). Items without a recognized slot appear in an "Other" section. Player inventory cyberware uses the bracketed format: `Name — [CWP: X] · [Slot Name]`. Shared constants in `NightCityBot/utils/constants.py`: `CW_SLOT_ORDER`, `CW_SLOT_DISPLAY_NAMES`. Shared formatter: `NightCityBot/utils/helpers.py::format_cw_lines_grouped()`. Fixer store add flows collect these attributes interactively if not provided inline.

`pi_update_character(item_id, new_character, expected_owner_id=None, *, new_character_id=None)` — when `new_character_id` is provided, also sets `character_id` in the UPDATE. All callers that know the target character (reassign, trade, give) should look up and pass the `character_id` to keep the column in sync.

Ripperdoc inventory lock convention: all load-mutate-save of ripperdoc inventory must use `async with cw_cog._locks.acquire(str(owner_id))`. This applies in `cyberware_shop.py`, `ripperdoc_hub.py`, `fixer_hub.py`, and `player_hub.py` (give-to-ripperdoc path).

Save-result checking convention: every call to `_save_state()` or `_save_inventory()` after a payment has been taken must check the boolean return value. If `False`, the flow must refund the buyer (and reverse seller credit if applicable), send the user an error message, and `return`. This is enforced in: `gunstore_hub.py` (catalogue buy + sell), `ripperdoc_hub.py` (catalogue buy + sell + install), `fixer_hub.py` (add-to-store flows), `cyberware_shop.py` (catalogue buy).

Refund failure safety net: when a compensating refund (via `update_balance`) also fails, a `pt_create` pending transfer record is created as a fallback so admins can resolve manually. This is enforced in: `gunstore_hub.py` (gun sale pi_add_item failure), `fixer_hub.py` (store-add gun/CW save failure). Refund failures are logged at CRITICAL level.

Open shop rollback convention: if the business reward balance update fails after `open_log_add` has committed the opening, `open_log_delete_today` removes the log entry so the player can retry. If the rollback itself fails, the old behavior (warn + admin contact) is used as fallback.

Active-character guard: `get_character_by_name(discord_user_id, name, *, active_only=False)` accepts an `active_only` kwarg. When `True`, the query adds `AND status = 'active'`. Trade and give flows in `player_hub.py` pass `active_only=True` before transferring items, preventing delivery to deactivated characters between confirmation and transfer.

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

## Post-Merge Setup

`scripts/post-merge.sh` runs automatically after task agent merges to install dependencies. Configured via `.replit` `[postMerge]` section.

## Naming conventions (user-facing text)

All user-facing messages, button labels, embed titles, and audit logs use full words: "Cyberware" (never "CW"). Internal variable names, DB keys, and code-level references still use the `cw_` prefix.

## Config cleanup notes

Removed dead constants: `TICKET_INDEX_FILE`, `WHOLESALER_RESTOCK_SCHEDULE`, `FIXER_ROLE_NAME` (only the ID is used).

## Key Dependencies

- discord.py, aiohttp, Flask, openpyxl, aiofiles, python-dotenv, rapidfuzz, asyncpg, google-api-python-client, google-auth

## Player Hub (`!player`)

Interactive panel for inventory management. Row 0 buttons: View Inventory, Trade Item, Sell to Store, Give Item. Row 2 buttons: Create Character, Manage Characters.

### Character System
Players can create, deactivate, and reactivate characters via the Player Hub. Characters are stored in the `characters` PostgreSQL table (25th table). Character helpers live in `NightCityBot/utils/characters.py`.

- **Create Character** — prompts for a name (≤64 chars, unique per user), creates an active character record
- **Manage Characters** — opens a sub-view with Deactivate/Reactivate buttons, each showing a character select menu
- All character state changes are ownership-enforced (`WHERE character_id=? AND user_id=?`) and logged to `NIGHTCITYBOT_LOG_CHANNEL_ID`

### Character-Aware Flows
All Trade/Give/Sell-to-Store flows now use character select menus instead of free-text character name inputs:
- **Trade Item** — after selecting a buyer, fetches their active characters and shows a StringSelect; buyer character is passed into the modal
- **Give Item** — after selecting a recipient, fetches their active characters and shows a StringSelect (Ripperdoc recipients bypass character selection); receiver character is passed into the modal
- **Sell to Store** — fetches the seller's active characters at button click; character select shown in setup view; seller character is passed through to the details modal
- **View Inventory** — when items belong to multiple characters, shows a character filter dropdown (All Characters + per-character options) before displaying the inventory embed

All flows use the UserSelect + item Select + character Select + Continue → modal pattern.

Blocking behavior: if a user (buyer/recipient/seller) has no active characters, the flow is blocked with an error message.

## Cyberware Shop

Two-table catalog system in PostgreSQL:
- `cyberware_catalog` (name UNIQUE, price, updated_at) — full item list, populated by `!cw_setsheet`
- Ripperdoc inventory stored per-owner in PostgreSQL

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
- `get_active_characters(discord_user_id)` / `get_inactive_characters(discord_user_id)` / `get_all_characters(discord_user_id)`
- `get_character(character_id)` / `get_character_by_name(discord_user_id, name)`
- `ensure_character_active(character_id)` — returns bool
- `character_name_exists(discord_user_id, name)` — returns bool
- `normalize_name(name)` — strip + lowercase
- `validate_name(name)` — empty/whitespace rejected, max 64 chars
- Removed: `get_character_by_id` (was redundant alias for `get_character`), `resolve_character_name` (was dangerous — auto-created characters on lookup miss)

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
Cog: `NightCityBot/cogs/player_inventory.py` — helper methods and `TradeConfirmView` only; all commands removed in favor of hub commands.

All inventory operations are handled through the interactive hubs:
- `!player` — View Inventory, Trade Item, Sell to Store, Give Item
- `!fixer` → Player sub-menu — Add Item, Remove Item, Reassign
- `!admin` — Admin panel for inventory management

## Unified Shop System (Task #14)

Consolidates separate command sets into interactive hub commands with Discord UI (dropdowns, buttons, inline text input), DM-confirmation trade flows, and a full per-item audit trail.

### New Cogs
- `NightCityBot/cogs/ripperdoc_hub.py` — `/ripperdoc` interactive panel. Layout: row0=[Buy from Catalogue, Catalogue List], row1=[Sell/Install to Patient], row2=[Manage Store, Manage Employees]. Manage Store submenu (`_ManageRDStoreView`): Create Store, Change Store Name (shows current name), My Stock, Transfer Ownership (DM confirmation to receiver), Close Store. Store data stored in CyberwareShop state as `ripperdoc_stores: {store_id: {owner_id, employees, store_name}}`. Store ID format: `"rd:{guild_id}:{owner_id}"`. Owner role (`RIPPERDOC_OWNER_ROLE_ID`) required for catalogue buy, manage store/employees. Employee role (`RIPPERDOC_EMPLOYEE_ROLE_ID`) can sell/install from assigned store only. Money goes to store owner. Dual-role users see a store picker. 1-store limit per player. DM confirmation flow for ownership transfer (`_RDTransferDMConfirmView`).
- `NightCityBot/cogs/gunstore_hub.py` — `/gunstore` interactive panel. Layout: row0=[Buy from Catalogue, Catalogue List], row1=[Sell to Customer, My Store Inventory], row2=[Manage Store, Manage Employees, Manage Buyers]. Manage Store submenu (`_ManageGunStoreView`): Create Store, Change Store Name (shows current name), Transfer Ownership (DM confirmation), Close Store. Manage Buyers submenu (`_ManageBuyersView`): Approve Buyer, Unapprove Buyer, Approved Buyers list. Store owners can nickname their store (`store_name` in store data). Owners can add/remove employees. Employees can sell from mapped store and manage buyers, but CANNOT buy from catalogue or manage store. Manage Store header shows "⚙️ Manage Store — {Name}". Money goes to store owner. Dual-role users see a store picker. 1-store limit per player. DM confirmation flow for ownership transfer (`_GunTransferDMConfirmView`).
- `NightCityBot/cogs/admin_shop.py` — `/admin` admin panel (Add/Remove/Reassign/History/Inventory/Seed/Sheets/Perm Overwrites); alias `!admin_shop` still works
- `NightCityBot/cogs/fixer_hub.py` — `/fixer` Fixer management panel with two-tier menu (Player/Store sub-menus for inventory, items, LOA, store stock). No Done buttons — sub-views replace the ephemeral message in-place. Store dropdown shows store_name as label with owner name as description.
- `NightCityBot/cogs/player_hub.py` — `/player` Player hub for viewing inventory, trading items, and giving items (replaces individual `!trade`, `!inv_give` commands in help)

All five hub commands are **hybrid commands** — they work as both `/slash` and `!prefix` commands. Panel responses are **ephemeral** (only visible to the invoker). Slash commands are synced automatically on bot startup via `tree.sync()` in `on_ready`.

### Item History / Audit Trail
Table: `item_history` (keyed by item UUID, stores event_type, actor_id, target_id, price, metadata JSONB, created_at)
- `ih_record_event()` and `ih_get_history()` in `db.py`
- Event types: `created`, `catalogue_buy`, `player_sale`, `traded`, `given`, `admin_add`, `admin_remove`, `admin_reassign`, `cw_catalogue_buy`, `cw_sold`, `cw_installed`
- `!item_history <uuid>` command for lookup

### DM Confirmation Flow
All sell/trade operations with another player now send a DM to the buyer/patient with Accept/Decline buttons (5-minute timeout). Self-trades (same user, different characters) bypass DM confirmation.

### Legacy Fallback Commands
`!cw_buy`, `!cw_sell`, `!cw_install`, and `!cw_inventory` are retained as fallbacks for cases exceeding the 25-item Discord dropdown limit.

### Test Coverage
- `NightCityBot/tests/test_unified_shop.py` — 56 tests covering all three new cogs, View interaction checks, button callbacks, DM confirm views, member resolution, log channels, timeouts, deprecation notices, cog registration
- Existing `test_player_inventory.py` updated for DM confirmation flow compatibility (51 tests)

### Audit log channels
- `CYBERWARE_LOG_CHANNEL_ID` — cyberware shop events (ripperdoc hub actions)
- `GUN_LOG_CHANNEL_ID` — gun shop events (gunstore hub actions)
- `GEAR_MISC_LOG_CHANNEL_ID` — player inventory trades/gives
- `NIGHTCITYBOT_LOG_CHANNEL_ID` — system alerts (pending transfers, admin actions, fixer hub actions)

### Panel success logging
All 23 panel actions across all hubs send audit log messages to the appropriate log channel on success:
- **Player Hub** (4): Attend, Open Shop, Start LOA, End LOA
- **Gun Store Hub** (6): Create Store, Change Store Name, Add Employee, Remove Employee, Approve Buyer, Unapprove Buyer
- **Ripperdoc Hub** (4): Create Store, Change Store Name, Add Employee, Remove Employee
- **Fixer Hub** (6): Start LOA, End LOA, Store-Add Gun, Store-Add Cyberware, Store-Remove Gun, Store-Remove Cyberware
- **Admin Shop** (3): Set Gun Sheet, Set Cyberware Sheet, Reload Sheets

All log sends are wrapped in try/except so failures never block the user action.

### Admin Shop — Manage Stores
- **Manage Stores** — 🏪 button in admin panel row 1. Consolidated hub for all store management. Flow: pick Gun/Ripperdoc type → management menu with:
  - **Create Store** — Pick owner via user select → type store name. Creates store in cog state, assigns owner role, posts audit log. Black market owners auto-detected via `config.BLACK_MARKET_OWNER_IDS`.
  - **Seed Stores** — Gun: dropdown picker to seed specific or all empty stores. Ripperdoc: seeds all empty stores automatically. Uses `status=live` catalog filter, records `ih_record_event`, posts audit embed.
  - **Manage Employees** — Pick a store → Add Employee (user select, grants role), Remove Employee (dropdown of current employees, removes role if not employed elsewhere), View Employees (list). 25-employee limit enforced.
  - **Change Store Name** — Pick a store → enter new name. Posts audit log with old/new name.

### Admin Shop — Permission Overwrites Audit
- **Perm Overwrites** — 🔐 button in admin panel row 4. Scans all server channels and reports total permission overwrite count, channels sorted by overwrite count (top 30 shown), with footer showing Discord's 500 overwrite limit for server templates.

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

## Concurrency & Scalability Hardening (Task #20)

### DB Pool
- Pool `max_size=20`, `min_size=2`, `command_timeout=60`
- All `pool.acquire()` calls use `POOL_ACQUIRE_TIMEOUT=10.0`
- `asyncio.TimeoutError` added to transient retry errors

### ResourceLockManager (`utils/db.py`)
Per-key `asyncio.Lock` dict with automatic cleanup (max 1024 entries). Used by `guns_shop`, `cyberware_shop`, and `economy` cogs to replace global locks with per-user/per-resource granular locks.

### Concurrency Limits
All hub commands (`!player`, `!fixer`, `!ripperdoc`, `!gunstore`, `!admin`, `!open_shop`, `!attend`) have:
- `@commands.max_concurrency(1, per=BucketType.user)` — prevents a user from running the same command concurrently
- `@commands.cooldown(1, 5, BucketType.user)` — 5-second per-user cooldown

### Error Isolation & Ephemeral Auto-Delete (`utils/interaction_safety.py`)
`SafeView` base class provides `on_error` handler that logs the error and sends an ephemeral "something went wrong" message — preventing one user's error from crashing the UI for others. All View classes across all hub cogs and utility modules inherit from it. (Note: `SafeModal` was removed; there are no modals anywhere — all input is collected inline via `collect_text_input` from `inline_helpers.py`.)

All ephemeral messages auto-delete after 5 minutes (300 seconds) via fire-and-forget asyncio tasks. Three helpers:
- `schedule_ephemeral_delete(target, *, delay=300)` — core scheduler; accepts `WebhookMessage` (calls `.delete()`) or `Interaction` (calls `.delete_original_response()`). Silently ignores `NotFound`/`Forbidden`/`HTTPException`.
- `send_ephemeral(interaction, content, **kwargs)` — wraps `followup.send(ephemeral=True)` + schedules auto-delete. Used across all cogs for followup ephemeral sends.
- `respond_ephemeral(interaction, content, **kwargs)` — wraps `response.send_message(ephemeral=True)` + schedules auto-delete. Used across all cogs for initial response ephemeral sends.
- `_safe_respond` and `safe_followup` also schedule auto-delete for ephemeral messages.

Convention: **never** call `followup.send(ephemeral=True)` or `response.send_message(ephemeral=True)` directly in cog code. Always use `send_ephemeral()` / `respond_ephemeral()` to ensure auto-delete coverage. `defer(ephemeral=True)` and `ctx.send(ephemeral=True)` are NOT affected (defers don't send visible messages; ctx.send is for slash command responses).

All sell/install flow error and status messages (decline/timeout, balance failures, refund notices, out-of-stock, save errors) are sent as ephemeral messages via `send_ephemeral()` — never as public channel messages via `ctx.send()`.

### View Timeouts
All interactive View timeouts across all hub cogs are standardized to **300 seconds (5 minutes)**. This applies to: sell/install setup views, DM confirmation views, employee hiring confirmations, store transfer confirmations, store close confirmations, manage views, buyer approval views, catalogue buy selects, and character creation text input. The only exception is the backup restore confirmation (`backup.py`) which stays at 30 seconds as a safety measure for that destructive operation. Discord interaction tokens last 15 minutes, so 5-minute view timeouts are safely within that window. The persistent hub panels (`GunstoreMenuView`, `RipperdocMenuView`, `PlayerMenuView`) use `timeout=None` (never expire).

### Inline Helpers (`utils/inline_helpers.py`)
`collect_text_input(bot, channel_id, author_id)` — waits for a user's text message reply, auto-deletes it, supports cancel. Used by hub flows that replaced modals with inline text collection.
`QtySelectView` — dropdown for quantity selection (1-25, capped at Discord limit).
`PriceSelectView` — dropdown with preset prices + custom amount option.

### UnbelievaBoat Rate Limiting (`services/unbelievaboat.py`)
- Retry attempts increased from 3→5
- Exponential backoff on non-429 errors: `min(1 * 2^attempt, 8)` seconds
- 429 retry uses server-provided `retry_after` clamped to [0.25, 30] seconds
- Robust `retry_after` parsing with fallback on JSON decode failure

## Gun Restriction System

Each weapon lot has a `restriction` field (default: `basic`):
- **basic** — anyone can buy, no special requirements
- **controlled** — buyer must be on the store owner's controlled-buyer list (`!wh_approve @user`)
- **restricted** — buyer must be on the controlled-buyer list AND an admin must approve the sale via audit channel reaction (5-minute timeout)

Store owner commands: `!wh_approve @user`, `!wh_unapprove @user`, `!wh_approved`
Admin commands:
- `!store_remove @owner <lot_id> [qty]` — remove a lot or reduce its quantity from a store

### Black Market Store Type

Stores owned by users in `config.BLACK_MARKET_OWNER_IDS` (currently Shyzuki, Discord ID 352598809357975572) are treated as Black Market stores. When a Black Market owner uses "Buy from Catalogue" in `!gunstore`, the system queries `gun_catalog` for all `status='live'` weapons with `restriction` of `controlled` or `restricted` (no basic weapons). The purchase price is `ceil(price * BLACK_MARKET_PRICE_MULTIPLIER)` (default 1.5×). Selling to customers still enforces controlled buyer list checks and restricted sale fixer-approval flows. Store data includes `store_type: "black_market"` for Black Market stores (default/absent = `"standard"`).
