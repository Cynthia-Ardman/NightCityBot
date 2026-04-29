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
* The mapping of users to logging threads is persisted in the `dm_threads` PostgreSQL table and cached on startup.

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

Economy data (attendance, business opens, payments) is stored in PostgreSQL tables. Role costs are defined in `NightCityBot/utils/constants.py` and runtime-editable via the `bot_config` table.

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

Cyberware status and streak data is stored in the `cyberware_status` and `cyberware_meta` PostgreSQL tables. Weekly results are recorded in the `cyberware_weekly_runs` table.

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


### PlayerInventory
*File: `NightCityBot/cogs/player_inventory.py`*

Tracks all items (guns, gear, cyberware) owned by players in the `player_inventory` PostgreSQL table. Supports admin management, player-to-player trading, and full audit trails.

* `!inv @user` – view a player's inventory.
* `!inv_add @player "name" <qty> "character_name"` – add items to a player's inventory (admin/fixer only).
* `!inv_remove @player <item_id>` – remove an item from inventory (admin/fixer only).
* `!inv_trade @from @to <item_id>` – transfer an item between players (admin/fixer only).
* `!item_history <item_id>` – view the full audit trail of an item (admin/fixer only).

### AdminShop
*File: `NightCityBot/cogs/admin_shop.py`*

A unified interactive panel for admins and fixers to manage the shop system. Provides button-based UI for adding/removing items, viewing inventories, and reviewing transaction history without memorizing commands.

* `!admin_shop` – open the interactive admin shop panel.

### GunstoreHub
*File: `NightCityBot/cogs/gunstore_hub.py`*

Interactive hub for gun store owners. Provides a Discord button/menu UI to buy from the full wholesale catalogue, sell to customers, and manage store inventory without using text commands.

* `!gunstore` – open the interactive gun store panel.

### RipperdocHub
*File: `NightCityBot/cogs/ripperdoc_hub.py`*

Interactive hub for Ripperdocs. Provides a Discord button/menu UI to buy cyberware directly from the full catalog, sell/install for patients, and manage stock.

* `!ripperdoc` – open the interactive ripperdoc panel.

### CyberwareShop
*File: `NightCityBot/cogs/cyberware_shop.py`*

Implements the Ripperdoc marketplace where Ripperdocs buy parts from the full cyberware catalog (no rotating wholesale) and sell/install them for patients. The Ripperdoc Hub panel (`!ripperdoc`) exposes the catalog through "Buy from Catalogue" and "Catalogue List" buttons. Also runs the weekly cyberware processor (`process_week`), which charges meds fees and DMs each affected member with one of three notices:

* **Checkup-due notice** – first time a cyberware user is missing their checkup, prompting them to book a Ripperdoc visit before charges begin.
* **Charged notice** – sent when meds fees are successfully deducted, showing the amount and current streak.
* **Payment-failed notice** – sent when the meds deduction fails (insufficient funds), warning of impending consequences.

After each weekly run finishes, a per-member summary embed is posted to the cyberware-logs channel (`CYBERWARE_LOG_CHANNEL_ID`) listing who was charged what, who failed payment, and who received a checkup notice.

### GunsShopCog
*File: `NightCityBot/cogs/guns_shop.py`*

Implements the gun store backend used by the Gunstore Hub (`!gunstore`) and Fixer Hub (`!fixer`). All player-facing gun-shop actions are driven through interactive Discord menus — the legacy `!wh_*` and `!store_*` prefix commands have been removed. The cog exposes helper methods (state management, sheet parsing, balance helpers) for the hubs via `bot.cogs.get("GunsShopCog")`.

The full Master Gun List catalogue is always available — there is no rotating wholesale lottery and no admin restock command. Each gun's wholesale cost comes from a dedicated **Wholesale Price** column in the spreadsheet (header aliases: `Wholesale Price`, `Wholesale`, `Price (Wholesale)`); if that column is blank the parser falls back to the existing `Price New` / sticker price. Guns are grouped for display by weapon type (Pistol, Revolver, Shotgun, Submachine Gun, Assault Rifle, etc.) based on section headers in the source spreadsheet. Fixers can also Add custom guns through the Fixer Hub for one-off items not present in the catalogue; these custom additions are stored as quantity-tracked overlay lots in `state["wholesale_lots"]` and appear alongside the catalogue. Sheet cache, custom-overlay lots, and per-store inventories are persisted in PostgreSQL (with local file fallback under `data/wholesaler/`) so stock survives restarts. All sales produce immutable receipts in the wholesaler audit channel for manual staff spreadsheet updates.

Each weapon has a restriction level that controls who can purchase it:
* **basic** (default) – anyone can buy, no special requirements.
* **controlled** – only buyers on the store owner's controlled-buyer list can purchase.
* **restricted** – requires being on the controlled-buyer list AND admin approval via audit channel reaction (5-minute timeout).

Restrictions are read from a "Restriction" column in the master spreadsheet (if present), and carry over from the catalogue to store inventories when stock is purchased through the Gunstore Hub.

**Gun Store Owner workflow** — open `!gunstore` and use the buttons:

* **Buy from Catalogue** – browse the full gun catalogue (plus any custom Fixer-added items) and purchase lots into your store inventory at the catalogue's wholesale price.
* **Sell to Customer** – pick a customer + character, choose stock, set price; payment moves from buyer to seller and a receipt is posted to the audit channel.
* **My Inventory** – view your current store stock grouped by weapon type.
* **Controlled-Buyer List** – approve/unapprove customers for controlled and restricted weapons.
* **Employees / Transfer Ownership** – manage employees who can sell on the store's behalf, or hand the store to a new owner.

**Fixer / Admin workflow** — open `!fixer` and use the **Store** sub-menu to:

* View any gun store's inventory.
* Add or remove stock directly to a store.
* Bind shop aliases to owners.
* Inspect transaction history for any item.

### SystemControl
*File: `NightCityBot/cogs/system_control.py`*

A small cog that allows administrators to enable or disable major subsystems at runtime. States are persisted in the `system_settings` PostgreSQL table.

Commands:

* `!enable_system <name>` / `!disable_system <name>` – flip a specific subsystem such as `cyberware` or `open_shop` on or off at runtime.
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

## Data storage

All operational state is persisted to **PostgreSQL** via normalized tables (economy, inventory, cyberware, wholesaler, system settings, etc.). Legacy JSON files on disk are retained only as seed sources for one-time migration and for per-member balance backups.

Key local data directories:

* `data/wholesaler/` – local file fallback for wholesaler state, store inventories, and transactions.
* `data/cyberware_shop/` – local file fallback for cyberware shop state and transactions.
* `backups/` – balance backup snapshots.
* `sheet_backups/` – character sheet backups.
* `rent_audits/` – monthly rent audit logs.

## Testing

A comprehensive suite of automated tests lives in `NightCityBot/tests`. They can be executed with:

```bash
pytest
```

Key test files:

* `test_wholesaler_full.py` – covers the gun-store backend end-to-end: spreadsheet parsing with weapon type assignment and Wholesale Price column, state save/load roundtrips, catalogue buying (success, insufficient funds, invalid lot), player sales (success, buyer/seller failures, pending payouts), shop registry management, transaction logging, full lifecycle flows (catalogue → buy → sell), multi-store independence, display grouping by weapon type, gun restrictions (basic/controlled/restricted enforcement and restriction carry-over), controlled-buyer list management, and URL-normalisation edge cases.
* `test_wholesaler_parsing.py` – tests for parsing logic, state migration, and file persistence.
* `test_wholesaler_commands.py` – smoke tests verifying all gun-store commands are registered.
* `test_release_readiness_simulation.py` – simulated multi-week economy + catalogue cycles with buy/sell and UnbelievaBoat balance transfers.

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

All steps below run through the interactive hubs — there are no `!wh_*` text commands.

1. **Grant store permissions role** to the player (one of `WHOLESALER_STORE_ROLE_IDS`).
2. **Bind a shop alias** to that owner: open `!fixer` → **Store** → use the shop-binding flow to attach an alias (e.g. `shop1`) to the user.
3. **Seed inventory** either by catalogue flow (the new owner opens `!gunstore` → **Buy from Catalogue**) or direct admin injection via `!fixer` → **Store** → select the store → **Add Item**.
4. **Verify mapping and stock** in `!fixer` → **Store** (lists shops and lets you inspect any inventory).
5. (Optional) **Set/rotate the source sheet URL live** through `!admin_shop` (or by updating the configured sheet URL); the cached catalogue refreshes from the sheet automatically.
