# NightCityBot

A Discord bot for NCRP (Cyberpunk-themed RP server) managing economy, roleplay utilities, and automated systems.

## Run & Operate

- **Run bot**: `python NightCityBot/bot.py`
- **Post-merge setup**: `scripts/post-merge.sh` (triggered automatically by `.replit`)
- **DB health check**: `!db_health` (admin command)
- **DB backup**: `!backup_now` (Fixer-only)
- **DB restore**: `!restore_db <id>` (Fixer-only, requires confirmation)
- **Required ENV VARS**: `GDRIVE_SERVICE_ACCOUNT_JSON`, `GDRIVE_BACKUP_FOLDER_ID`. Optional: `BACKUP_RETENTION_DAYS`, `BACKUP_HOUR`, `BACKUP_MINUTE`.

## Stack

- **Frameworks**: discord.py, Flask
- **Runtime**: Python 3.x (specific version not stated, infer from `requirements.txt`)
- **ORM**: _Populate as you build_
- **Validation**: _Populate as you build_
- **Build Tool**: _Populate as you build_
- **Database**: PostgreSQL
- **Key Libraries**: aiohttp, openpyxl, aiofiles, python-dotenv, rapidfuzz, asyncpg, google-api-python-client, google-auth

## Where things live

- `NightCityBot/bot.py`: Main bot entry point and Flask keep-alive server.
- `config.py`: Global configuration and secrets.
- `NightCityBot/cogs/`: Modular command groups (economy, stores, admin, player, missions, etc.).
- `NightCityBot/cogs/missions.py`: Fixer-only mission tracking (`!mission_check`, `!mission_record`).
- `NightCityBot/services/`: External API wrappers and complex business logic.
- `NightCityBot/utils/`: General utilities, helpers, permissions, startup checks.
- `NightCityBot/tests/`: Pytest test suite.
- `NightCityBot/utils/db.py`: Database interaction utilities and resilience.
- `NightCityBot/utils/characters.py`: Character management CRUD.
- `NightCityBot/utils/inline_helpers.py`: Discord UI interaction helpers.
- `NightCityBot/services/unbelievaboat.py`: UnbelievaBoat API wrapper.
- `NightCityBot/utils/db_backup.py`: Database export/import logic.
- `NightCityBot/utils/gdrive_backup.py`: Google Drive API wrapper.
- `NightCityBot/utils/interaction_safety.py`: Concurrency and error handling for interactions.
- **DB Schema**: Defined implicitly by ORM/SQL in `db.py` and cog files. Key tables: `characters`, `player_inventory`, `item_history`, `cw_shop_state`, `fixer_event`, `store_inventory`, `shop_permitted_roles`, `balance_history`, `bot_config`, `json_store`, `cyberware_catalog`, `gun_catalog`, `pending_transfers`.
- **API Contracts**: UnbelievaBoat API (wrapped in `unbelievaboat.py`), Google Drive API (wrapped in `gdrive_backup.py`).
- **Theme Files**: User-facing text and embed formats defined in cog files and `utils/constants.py`.

## Architecture decisions

- **Database-driven configuration**: Key economy constants are stored in `bot_config` table, allowing runtime modification without redeployment.
- **Character-centric inventory**: Player inventory is owned by characters, not directly by Discord users, enabling multi-character play.
- **Unified Interactive Hubs**: All major user-facing features (player inventory, shops, admin, fixer) are managed via interactive Discord UI hubs, replacing fragmented command sets.
- **Comprehensive Audit Trail**: `item_history` table records all item lifecycle events for accountability and debugging.
- **Robust Concurrency & Error Handling**: Extensive use of DB connection pooling, per-resource locks, interaction cooldowns, and ephemeral auto-deleting error messages ensure bot stability and a smooth user experience under load.
- **Automated Disaster Recovery**: Scheduled and on-demand database backups to Google Drive provide a robust disaster recovery mechanism.

## Product

- **Economy Management**: Tracks user balances, manages monthly/weekly charges (rent, cyberware meds), and attendance rewards.
- **Player Inventory System**: Allows players to view, trade, sell, and give items to other players or stores, with character-aware flows.
- **Character System**: Players can create, activate, and deactivate multiple in-game characters.
- **Unified Shop System**: Interactive Discord UI for gun stores and ripperdoc clinics, including catalog browsing, buying, selling, and inventory management for store owners.
- **Item History**: Provides a detailed audit log for all item transactions.
- **Admin & Fixer Tools**: Dedicated panels for administrators and fixers to manage economy, inventory, stores, and system configurations.
- **Google Drive Backups**: Automated and manual database backups for data integrity and recovery.
- **Gun Restriction System**: Implements `basic`, `controlled`, and `restricted` categories for firearms, with associated approval workflows.
- **Mission Tracking**: Fixer-only roster tracking how recently and how often each player has been on missions, backed by a `mission_log` Postgres table, with a one-shot Google-Sheet importer for legacy data.
- **Mission Creation & Auto-Payout**: Fixer panel button (`Missions → Create Mission`) opens a short Modal (name / pay per player / location), then a `CreateMissionScheduleView` with four dropdowns — date (next 14 days), start hour (24 hourly slots, 12h AM/PM labels), duration (30m → 24h), and timezone (US Eastern / Central / Mountain / Pacific / UTC, defaults to the fixer's last-used pick — persisted via `bot_config` key `fixer_tz:{user_id}` so it auto-fills next time). The fixer picks in their own timezone; the bot converts to UTC at Continue and Discord auto-renders the scheduled event in each viewer's local timezone. Continue advances to the existing attendees view (UserSelect for up to 25 attendees, Attach Banner button). Publishes a Discord scheduled event titled "Actors Needed: …" (cover image: the Fixer can tap **📎 Attach Banner** on the attendees screen and paste/upload a custom image in chat within 60 seconds — PNG/JPEG/WebP/GIF, ≤ 8 MiB — otherwise the bot randomly picks from `attached_assets/NCRP_SquareBanner*.png` / `NCRP_GroupBanner*.png`, with legacy `attached_assets/mission_banner.png` still rotated if present) and stores a row in `mission_event` (including `creator_username`). On creation, every selected attendee also gets a `mission_log` entry written immediately (so the mission counts toward their record at sign-up). A `tasks.loop(minutes=5)` in `MissionsCog` checks for due missions and, at the next midnight US Eastern after the start time, auto-pays each attendee to bank via UnbelievaBoat. The Fixer who created the mission is never paid.
- **Actor Pay (Fixer panel → Missions → Actor Pay)**: UserSelect (up to 25 actors) + a Select dropdown populated with the 25 most recent recorded missions; clicking **Continue** opens a Modal asking for the per-actor pay. Each actor is paid via UnbelievaBoat (to bank) and a row is written to `actor_attendance` tagged with the fixer, mission, and amount.
- **Check Actor (Fixer panel → Missions → Check Actor)**: UserSelect (up to 25) returns each user's total acting count, the dates they acted, and a recent list of missions they were acting in (with mission title, fixer username, and pay).
- **Check Missions enrichment**: `!mission_check` and the panel's **Check Missions** button now also list recent mission titles + the fixer username who created each mission for each player (queried from `mission_event`).
- **Mission Reconciliation & Edit**: The payout loop re-checks each due mission against Discord before paying, then atomically claims the row via `mission_event_claim_for_payout` (CAS on `paid=FALSE AND canceled=FALSE`) before any UnbelievaBoat call — this closes the double-payout race when ticks overlap or a concurrent cancel arrives. Reconcile branches: (a) Discord status `canceled` → mark row canceled, no pay. (b) Event renamed off `Actors Needed:` → mark row canceled, no pay. (c) Event was `fetch_scheduled_event` NotFound — if `start_ts` is in the past, treat as "Discord auto-purged the completed event" and pay anyway; if `start_ts` is still in the future, the fixer deleted it pre-flight → mark canceled, no pay. (d) Event rescheduled into the future → sync `start_ts`/`end_ts`/`payout_ts`, defer to new midnight ET. (e) Event rewound >24h into the past → refuse to pay, mark canceled, notify (blocks "rewind an old mission to retrigger payout" misuse). All edits go through `mission_event_update` / `mission_event_cancel`; reconciliation notices post back to the mission's origin channel. The Missions panel **Edit Mission** button (✏️ row 2) lists only **active** missions (`paid=FALSE AND canceled=FALSE` via `mission_event_list_active`) — paid and canceled missions are not editable. The Edit panel auto-disables every action button except Refresh/Close when the row becomes paid or canceled (defensive — the dropdown already filters them). Editing flows: **Edit Date/Time** (modal, updates Discord event + recomputes payout_ts), **Edit Attendees** (UserSelect replaces the list; two toggles — `Credit Added` ON by default writes a gig-log credit for newly-added attendees, `Reverse Removed` OFF by default removes one gig-log credit for the mission's date from removed attendees), **Edit Payout** (modal, updates pay_per_player and rewrites event description), **Cancel Mission** (deletes the Discord event + marks the row canceled; `Reverse Gig-Log Credits` toggle OFF by default — when ON, removes one mission_log date entry per attendee), and **Refresh** to re-read the row. Gig-log credit reversal uses `mission_log_remove_date(user_id, date)`, which splices exactly one matching entry from the `mission_dates` array and decrements `mission_count` (so a player with two legitimate missions on the same day still keeps one).

## User preferences

_Populate as you build_

## Gotchas

- **Refund Convention**: All rollbacks/refunds must use `{"bank": -price}` to prevent negative cash balances for sellers.
- **Balance Deduction**: Always use the cash+bank split pattern for payments; never deduct from cash-only.
- **Save Result Checking**: After any payment, always check the boolean return of `_save_state()` or `_save_inventory()`; if `False`, refund and error.
- **Ripperdoc Inventory Lock**: All load-mutate-save operations on ripperdoc inventory must use `async with cw_cog._locks.acquire(str(owner_id))` to prevent race conditions.
- **Ephemeral Message Sending**: Always use `send_ephemeral()` or `respond_ephemeral()` (or their safe wrappers) for ephemeral messages to ensure auto-deletion. Avoid direct `followup.send(ephemeral=True)` or `response.send_message(ephemeral=True)`.

## Pointers

- **discord.py docs**: [https://discordpy.readthedocs.io/en/stable/](https://discordpy.readthedocs.io/en/stable/)
- **asyncpg docs**: [https://magicstack.github.io/asyncpg/current/](https://magicstack.github.io/asyncpg/current/)
- **Google API Python Client**: [https://googleapis.dev/python/google-api-python-client/latest/index.html](https://googleapis.dev/python/google-api-python-client/latest/index.html)
- **PostgreSQL docs**: [https://www.postgresql.org/docs/](https://www.postgresql.org/docs/)
- **UnbelievaBoat API**: [https://docs.unbelievaboat.com/api/](https://docs.unbelievaboat.com/api/)
- **Backup Setup Guide**: `BACKUP_SETUP.md`