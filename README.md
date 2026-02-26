# NightCityBot

NightCityBot is a Discord bot built with `discord.py` that provides roleplay utilities, economy management and automated moderation tools for a Cyberpunk themed server.  The bot is organised using *cogs* – modular components that group related commands and background tasks.

This document gives an overview of the major modules and how they work.

## Requirements

* Python 3.11+
* The packages listed in `requirements.txt`
* A Discord bot token and configuration values in `config.py`

Install dependencies with:

```bash
pip install -r requirements.txt
```

## Configuration

Create a `config.py` file with your role and channel IDs. The bot uses an IANA
timezone string via `TIMEZONE` to schedule weekly tasks such as `!open_shop`.
Credentials like the Discord bot token and UnbelievaBoat API token can be
provided through the environment variables `TOKEN` and
`UNBELIEVABOAT_API_TOKEN`.  If a `.env` file exists in the project root its
contents will be loaded automatically. Example `config.py`:

```python
import os
TOKEN = os.environ.get("TOKEN")
UNBELIEVABOAT_API_TOKEN = os.environ.get("UNBELIEVABOAT_API_TOKEN")
GUILD_ID = 1234567890
TIMEZONE = "America/Los_Angeles"  # or your preferred zone
```

Configuration is verified automatically when the bot starts.

Wholesaler-specific configuration:

* `WHOLESALER_GOOGLE_SHEET_XLSX_URL` – optional Google Sheets export URL (XLSX). When set, the bot downloads fresh stock source data directly from the sheet before restocks/rechecks.
* `WHOLESALER_XLSX_PATH` – local fallback XLSX path if Google export URL is not set.
* `WHOLESALER_MASTER_SHEET_NAME` – source tab name (default `Master Gun List`).
* `WHOLESALER_AUDIT_CHANNEL_ID` – channel where immutable sales receipts and payout alerts are posted.
* `WHOLESALER_ADMIN_ROLE_IDS` / `WHOLESALER_STORE_ROLE_IDS` – comma-separated role IDs for command permissions.


## Running the bot

Execute the entry point script:

```bash
python -m NightCityBot.bot
```

A small Flask server is also started to keep the bot alive on certain hosting platforms.

### Replit restart behavior (why it comes back after stopping)

If your bot appears to auto-restart on Replit, that is usually platform behavior rather than bot code:

* `.replit` defines a deployment target (`cloudrun`) and run command. Replit Deployments supervise the process and restart it when it exits.
* The bot also starts a keep-alive HTTP listener by default, which can make the Repl look active whenever external health checks or uptime pings hit `/`.

To disable the internal keep-alive listener for manual/dev runs, set `DISABLE_KEEP_ALIVE=true` in your environment before starting the bot.

## Cogs

### DMHandler
*File: `NightCityBot/cogs/dm_handling.py`*

Handles anonymous DMs from Fixers to players and maintains a logging thread for each user in the channel defined by `DM_INBOX_CHANNEL_ID`.

Key features:

* `!dm @user <message>` – send an anonymous DM to a player. Attachments are forwarded and the entire exchange is logged in a private thread so staff can review it later.
* Commands typed from a DM log thread (for example `!roll` or `!start_rp`) are relayed back to the user, allowing full interaction without revealing your identity.
* The mapping of users to logging threads is persisted in `thread_map.json` and loaded on startup.

### Economy
*File: `NightCityBot/cogs/economy.py`*

Manages the in‑game economy and rent collection. It integrates with the [UnbelievaBoat](https://unbelievaboat.com/) economy API.

Main commands:

* `!open_shop` – used by business owners on Sundays. Logs a shop opening and immediately awards passive income based on the business tier. Each player can record up to four openings per month.
* `!attend` – every verified player can run this on Sundays to receive a weekly $250 attendance reward. The command refuses to run more than once per week.
* `!event_start` – fixers can activate this in the attendance channel to temporarily allow `!attend` and `!open_shop` for four hours outside of Sunday.
* `!due [@user]` – show a full breakdown of the baseline fee, housing and business rent, Trauma Team subscription and upcoming cyberware medication costs that will be charged on the 1st. When a user is supplied the estimate is for that member.
* `!last_payment` – show the details of your last automated payment.
* `!collect_rent [@user] [-v] [-force]` – run the monthly rent cycle. Supply a user mention to limit the collection to that member. Use `-force` to ignore the 30 day cooldown. With `-v`, each step is announced as it happens and balance backup progress for each member is shown so you can track the cycle live.
* `!paydue [-v]` – pay your monthly obligations early. Works like `!collect_rent` but only for yourself. Use `-v` for a detailed summary.
* `!simulate_rent [@user] [-v] [-cyberware]` – identical to `!collect_rent` but performs a dry run without updating balances. When a user is specified the output notes that a DM and last_payment entry would be created. With `-cyberware` the upcoming medication cost for the specified user is also shown.
* `!simulate_all [@user]` – run both simulations at once. When a user is given the rent output indicates that a DM and last_payment entry would be created.
* `!list_deficits` – run the same checks as `!simulate_all` but only list members who would fail any charge. Each entry shows the shortfall and unpaid items, marking rent with "(eviction)".
* `!collect_housing @user [-force]`, `!collect_business @user [-force]`, `!collect_trauma @user [-force]` – immediately charge a single user's housing rent, business rent or Trauma Team subscription. Pass `-force` to override the 30 day limit.
* `!backup_balances` – save all member balances to a timestamped JSON file. Each
  backup entry records the balance and the `change` since the previous entry.
* `!backup_balance @user` – save a single member's balance to a timestamped file.
* `!restore_balances <file>` – restore balances from a previous backup file.
* `!restore_balance @user [file]` – restore a single user's balance. If no file
  is provided (or the user's automatic backup file is used) the latest entry is
  applied.

The cog stores logs in JSON files such as `business_open_log.json` and `attendance_log.json` and consults `NightCityBot/utils/constants.py` for role costs.

### CyberwareManager
*File: `NightCityBot/cogs/cyberware.py`*

Implements weekly check‑up reminders and medication costs for players with cyberware. A background task runs every Saturday:

1. Gives the `CYBER_CHECKUP_ROLE_ID` role each week (Ripperdocs are skipped).
2. If the role is kept the following week, deducts a cost based on the cyberware level (medium/high/extreme).

Commands:

* `!simulate_cyberware [@user] [week]` – with no arguments this performs a dry run of the entire weekly cycle for every player. When a user and week number are provided it simply reports the medication cost that would be charged on that week.
* `!checkup @user` – ripperdoc command to remove the weekly check‑up role from a player after their in‑character medical exam, resetting their streak to zero.
* `!weeks_without_checkup @user` – show how many weeks the specified player has kept the check‑up role without visiting a ripperdoc.
* `!give_checkup_role [@user]` – give the check-up role to a member or all cyberware users.
* `!checkup_report` – list who did a checkup last week, who paid their meds and who couldn't pay.
* `!cyberware_status` – show the current week status for all cyberware users.
* `!collect_cyberware @user [-v]` – manually charge a member for their meds unless they already paid or did a checkup this week. Without `-v` only the last few log lines are shown.
* `!paycyberware [-v]` – pay your own cyberware meds manually. Mirrors `!collect_cyberware` but only affects you.

All data is stored in `cyberware_log.json`. The file now keeps each user's
streak together with a ``last`` timestamp indicating when that player was last
processed. The file also stores a `_last_run` timestamp for the most recent
weekly task. Weekly results are appended to `cyberware_weekly.json`.

### RPManager
*File: `NightCityBot/cogs/rp_manager.py`*

Provides tools for creating private RP text channels and archiving them when complete.

* `!start_rp @users` – fixers and admins can create a private text channel for the mentioned users. The channel name is generated automatically using `utils.helpers.build_channel_name` and only the participants and staff can view it.
* `!end_rp` – once the scene is finished, this command archives the entire channel into the group audit forum and deletes the original channel.
* Any command typed inside a `text-rp-*` channel is relayed back to the bot, so players can roll dice or trigger other commands without leaving the RP session.

### RollSystem
*File: `NightCityBot/cogs/roll_system.py`*

Simple dice rolling logic supporting `XdY+Z` syntax. Rolls can be used in any channel or DM.

Highlights:

* Logs DM rolls back to the user's logging thread for record keeping.
* Supports rolling on behalf of another user when invoked via `!post` or `!dm`.

### LOA
*File: `NightCityBot/cogs/loa.py`*

Manages Leave‑of‑Absence status.

* `!start_loa` and `!end_loa` – players may place themselves on LOA to pause monthly fees and Trauma Team billing. Fixers can provide a user mention to toggle LOA for someone else.
* While a player is on LOA, the economy cog automatically skips baseline costs, housing rent and Trauma Team payments until `!end_loa` is used.

### CharacterManager
*File: `NightCityBot/cogs/character_manager.py`*

Utilities for the character sheet forums.

* `!retire` – move all threads tagged "Retired" from the main sheet forum to the retired forum. *(Fixers only)*
  Reports the number of threads moved and logs any failures.
* `!move_npcs` – move all threads tagged "NPC" from the main sheet forum to the NPC forum. *(Fixers only)*
* `!unretire <thread id>` – move a specific thread back to the main forum. *(Fixers only)*
* `!search_characters <keyword> [-depth N]` – fuzzy search thread titles, tags and posts. Depth controls how many messages per thread are scanned (default 20). *(Fixers only)*

### RoleButtons
*File: `NightCityBot/cogs/role_buttons.py`*

Provides a button for players to self-assign the NPC role.

* `!npc_button` – post a persistent button that grants the NPC role.

### TraumaTeam
*File: `NightCityBot/cogs/trauma_team.py`*

Commands:

* `!call_trauma` – notify the Trauma Team channel with your plan role.


### WholesalerCog
*File: `NightCityBot/cogs/wholesaler.py`*

Implements a two-tier gun supply chain with scarcity: corporate wholesaler lots are generated from the Master Gun List spreadsheet, store owners purchase those lots, then sell to players using UnbelievaBoat balance transfers. Wholesaler state, transaction logs, sheet cache, wholesale lots, and per-store inventories are persisted under `data/wholesaler/` so stock survives restarts. The wholesaler auto-refreshes weekly right after cyberware processing, and all sales produce immutable receipts in the wholesaler audit channel for manual staff spreadsheet updates.

Lots are grouped by weapon type (Pistol, Revolver, Shotgun, Submachine Gun, Assault Rifle, etc.) based on section headers in the source spreadsheet. During restock, duplicate guns with the same name, level, cost and type are automatically consolidated into a single lot with combined quantity.

Main commands (separated by role):

**Gun Store Owner Commands**
* `!wh_list` – list available wholesaler lots grouped by weapon type.
* `!wh_buy <lot_id> <qty>` – buy stock from wholesaler into your store inventory (deducts owner funds).
* `!store_inv [shop_name]` – view your own store inventory, or an admin can look up any shop by alias.
* `!wh_sell @buyer "character_name" <lot_id> <qty> <price>` – process player sale (deduct buyer, credit seller, decrement inventory, post receipt). `!sell` also works as an alias.
* `!wh_shops` – list all configured shop aliases and their owners.

**Wholesaler / Admin Commands**
* `!wh_setshop <shop_name> @owner` – bind a shop alias to a Discord user.
* `!wh_setsheet <xlsx_export_url|off>` – set/clear a runtime Google Sheets source URL without restarting the bot (regular share links are auto-converted to XLSX export).
* `!wh_restock [seed]` – regenerate weekly wholesaler lots from sheet data with L/M/H weighted scarcity and per-weapon-type mix settings.
* `!wh_clear_inventory` – clear all current wholesaler lots without modifying store inventories.
* `!wh_restock_settings [key] [value]` – view/tune refresh settings (total lots, lots per level, qty ranges, and per-weapon-type lot counts).
* `!wh_recheck` – compare current wholesaler lot level/cost data against the current source sheet and report mismatches.
* `!wh_add <gun_name> <L|M|H> <unit_cost> <qty>` – manually add a lot to the wholesaler.
* `!store_add @owner <gun_name> <L|M|H> <unit_cost> <qty>` – manually add a lot directly to a store.
* `!wh_tx <tx_id>` – look up raw transaction data by ID.
* `!wh_retry_payout <tx_id>` – retry a failed seller payout from a previous sale where the buyer was charged but the seller credit failed.
* `!wh_paths` – display the file system paths where wholesaler data is stored.

### SystemControl
*File: `NightCityBot/cogs/system_control.py`*

A small cog that allows administrators to enable or disable major subsystems at runtime. States are persisted in `system_status.json`, and the `wholesaler` subsystem defaults to enabled when no prior status exists.

Commands:

* `!enable_system <name>` / `!disable_system <name>` – flip a specific subsystem such as `cyberware` or `open_shop` on or off at runtime. The setting persists in `system_status.json`.
* `!system_status` – list every tracked subsystem and whether it is currently enabled.

### Admin
*File: `NightCityBot/cogs/admin.py`*

Offers helper commands for staff and global error handling.

* `!post <channel> <message>` – send a message or execute a command in another channel or thread. If `<message>` begins with `!`, the command is run as if it were typed in that location.
* `!helpme`, `!helpfixer`, `!helpadmin` and `!helpbusiness` (aliases: `!helpshop`, `!helpstore`) – show the built in help embeds. `!helpme` lists player commands, `!helpfixer` covers fixer tools, `!helpadmin` documents administrator-only features, and `!helpbusiness` walks gun store owners through the buy-and-sell workflow step by step.
* `!backfill_logs [limit]` – rebuild `attendance_log.json` and `business_open_log.json` by scanning recent messages. Only successful command usages are recorded. The optional limit controls how many messages are parsed (default 1000).
* All sensitive actions are logged via `log_audit` to the channel defined by `AUDIT_LOG_CHANNEL_ID`.

### TestSuite
*File: `NightCityBot/cogs/test_suite.py`*

Exposes the internal test suite directly through Discord commands.

* `!test_bot [tests]` – execute the built-in test functions. Provide one or more test names or prefixes to run them selectively. Use `-silent` to send results via DM and `-verbose` for step-by-step logs. All output is also mirrored to the audit log channel for debugging.
* `!list_tests` – display the available self-test names.
* `!test__bot [pattern]` – run the full PyTest suite. Optional patterns limit execution to matching tests. This command is primarily for repository maintainers.

## Services

The `services` package contains integrations used by the cogs:

* **UnbelievaBoatAPI** (`services/unbelievaboat.py`) – minimal wrapper around the UnbelievaBoat REST API for fetching and updating user balances. The wrapper includes basic retry logic for resilience against temporary failures.
* **TraumaTeamService** (`services/trauma_team.py`) – helper for processing Trauma Team subscription payments and posting into the configured forum channel.

## Startup checks

On initialisation the bot runs `perform_startup_checks` which verifies that all configured roles and channels exist, confirms the bot has the permissions it needs, and cleans up orphaned entries from the JSON log files. This helps catch configuration issues early and keeps data files tidy.

## Utilities

Utility helpers reside in `NightCityBot/utils`:

* `helpers.py` – asynchronous JSON helpers and the `build_channel_name` function.
* `permissions.py` – custom checks such as `is_fixer` and `is_ripperdoc`.
* `constants.py` – economy related constants and command filters.

## Data files

Several JSON files store runtime data:

* `thread_map.json` – mapping of user IDs to DM log thread IDs.
* `business_open_log.json` – timestamps of each user's `!open_shop` usage.
* `attendance_log.json` – records weekly attendance.
* `cyberware_log.json` – for each user stores the streak and the last time they
  were processed, plus the last run timestamp for the weekly task.
* `system_status.json` – persisted enable/disable flags for subsystems.

These files are loaded on startup via `utils.helpers.load_json_file`.

## Testing

A comprehensive suite of automated tests lives in `NightCityBot/tests`. They can be executed with:

```bash
pytest
```

Key test files:

* `test_wholesaler_full.py` – **64 tests** covering the entire wholesaler system end-to-end: spreadsheet parsing with weapon type assignment, restock lot consolidation, state save/load roundtrips, wholesale buying (success, insufficient funds, invalid lot, zero qty, exceeding stock), player sales (success, buyer/seller failures, pending payouts), permission enforcement for all roles, shop registry management, admin stock commands (`wh_add`, `store_add`, `wh_clear_inventory`), transaction logging, full lifecycle flows (restock → buy → sell), multi-store independence, display grouping by weapon type, restock settings, and edge cases for URL normalization and utility functions.
* `test_wholesaler_parsing.py` – **35 tests** for parsing logic, restock math, state migration, and file persistence.
* `test_wholesaler_commands.py` – smoke tests verifying all wholesaler commands are registered.
* `test_release_readiness_simulation.py` – simulated multi-week economy + wholesaler cycles with buy/sell and UnbelievaBoat balance transfers.

For release-readiness checks only:

```bash
pytest -q NightCityBot/tests/test_release_readiness_simulation.py
```

For the full wholesaler system test suite:

```bash
pytest -v NightCityBot/tests/test_wholesaler_full.py
```

Alternatively, run `!test_bot` inside Discord to perform many of the same checks without leaving the chat.


## Setting up a new gun store owner

1. **Grant store permissions role** to the player (one of `WHOLESALER_STORE_ROLE_IDS`).
2. **Bind a shop alias** to that owner: `!wh_setshop shop1 @User` (use any alias like `shop2`, `shop3`, etc.).
3. **Seed inventory** either by wholesaler flow (`!wh_restock` then owner runs `!wh_buy`) or direct admin injection with `!store_add @User <gun> <L|M|H> <unit_cost> <qty>`.
4. **Verify mapping and stock** with `!wh_shops` and `!store_inv shop1`.
5. (Optional) **Set/rotate source sheet URL live** with `!wh_setsheet <google_xlsx_export_url>`. Use `!wh_setsheet off` to revert to config values.
