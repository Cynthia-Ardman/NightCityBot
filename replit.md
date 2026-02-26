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

All state is persisted as JSON files in the workspace:
- Root-level: `system_status.json`, `thread_map.json`, `business_open_log.json`, etc.
- **Wholesaler data**: `data/wholesaler/` directory containing:
  - `state.json` — main wholesaler state (lots, settings, pending payouts)
  - `stores.json` — store registry and owner mappings
  - `inventory/wholesale.json` — current wholesale stock available for purchase
  - `inventory/stores/<store_id>.json` — individual store inventories (created when stores buy stock)
  - `transactions.json` — append-only transaction log

## Key Dependencies

- discord.py, aiohttp, Flask, openpyxl, aiofiles, python-dotenv, rapidfuzz

## Wholesaler System Flow

1. `!wh_restock` — downloads Google Sheet, generates random weapon lots, saves to `state.json` + `wholesale.json`
2. `!wh_buy` — store owners buy lots from wholesaler, creates per-store files in `inventory/stores/`
3. `!sell` — store owners sell weapons to players
