# NightCityBot

A Discord bot for NCRP (Cyberpunk-themed RP server) managing economy, roleplay utilities, and automated systems.

## Architecture

- **Entry point**: `NightCityBot/bot.py` — runs the Discord bot with Flask keep-alive server on port 5000
- **Config**: `config.py` (root) — all IDs, paths, and secrets loaded from env vars
- **Cogs**: `NightCityBot/cogs/` — modular command groups (economy, wholesaler, cyberware, etc.)
- **Services**: `NightCityBot/services/` — UnbelievaBoat API wrapper, Trauma Team logic
- **Utils**: `NightCityBot/utils/` — helpers, permissions, startup checks
- **Tests**: `NightCityBot/tests/` — pytest suite

## Data Storage

Operational state is now persisted to **PostgreSQL** via the `json_store` table (key TEXT PK, value JSONB). File-based JSON storage is retained only for per-member balance backups.

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
