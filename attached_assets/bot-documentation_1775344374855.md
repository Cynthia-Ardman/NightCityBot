# NightCityBot v3.0 -- Complete Documentation

---

## Table of Contents

1. [Overview](#overview)
2. [Hub Panels](#hub-panels)
   - [Player Hub](#player-hub)
   - [Gun Store Hub](#gun-store-hub)
   - [Ripperdoc Hub](#ripperdoc-hub)
   - [Fixer Hub](#fixer-hub)
   - [Admin Panel](#admin-panel)
3. [Player Inventory System](#player-inventory-system)
4. [Character System](#character-system)
5. [Gun Store System](#gun-store-system)
   - [Standard Stores](#standard-stores)
   - [Black Market](#black-market)
   - [Controlled-Weapon Approvals](#controlled-weapon-approvals)
   - [Employee System (Gun Stores)](#employee-system-gun-stores)
6. [Cyberware Shop System](#cyberware-shop-system)
   - [Ripperdoc Stores](#ripperdoc-stores)
   - [Employee System (Ripperdoc)](#employee-system-ripperdoc)
7. [Economy System](#economy-system)
   - [Rent Collection](#rent-collection)
   - [Passive Income](#passive-income)
   - [Attendance](#attendance)
   - [Leave of Absence](#leave-of-absence)
   - [Trauma Team](#trauma-team)
   - [Cyberware Medication](#cyberware-medication)
8. [Roles and Permissions](#roles-and-permissions)
9. [System Control Flags](#system-control-flags)
10. [Text Commands Reference](#text-commands-reference)
11. [Configuration](#configuration)
12. [Database Tables](#database-tables)
13. [Infrastructure](#infrastructure)

---

## Overview

NightCityBot is a Discord bot for the NCRP (Night City Roleplay) server. It manages the RP economy, player inventories, gun and cyberware shops, rent collection, dice rolls, DM relays, and character management.

The primary interface is a set of **persistent hub panels** -- permanent embeds in dedicated channels with buttons that players click. Each role (player, gun store owner, Ripperdoc, Fixer, admin) has its own hub channel. All hub responses are ephemeral (only visible to the person who clicked).

Text commands still work for power users and specific admin tasks.

---

## Hub Panels

Each hub is posted once by an admin using a hybrid command (e.g., `!player`). The panel stays in the channel permanently and survives bot restarts. Any eligible user can click the buttons.

### Player Hub

**Channel:** `#player-hub` (config: `PLAYER_HUB_CHANNEL_ID`)
**Posted by:** `!player` (administrator only)
**Who can use it:** Anyone in the server

| Row | Button | What it does |
|-----|--------|-------------|
| 0 | **View Inventory** | Select a character, see all their items with pagination |
| 0 | **Manage Inventory** | Opens sub-menu: Sell to Player, Sell to Store, Give Item |
| 1 | **Manage Characters** | Opens sub-menu: Create Character, View Characters, Deactivate Character |
| 1 | **Manage Businesses** | View your businesses and employment across gun stores and ripperdoc clinics |
| 2 | **Start LOA** | Add the LOA role to yourself (exempts you from baseline/housing/Trauma fees) |
| 2 | **End LOA** | Remove the LOA role |
| 3 | **Attend** | Log weekly attendance for a $250 reward (Sundays and active Fixer events only) |
| 3 | **Open Shop** | Log a business opening for a cash payout (Sundays and active Fixer events only) |
| 3 | **View Due** | See a full breakdown of your estimated monthly costs |

**Manage Inventory sub-menu:**

| Button | Flow |
|--------|------|
| **Sell to Player** | Select item -> select buyer (user picker) -> select buyer's character -> enter price -> buyer gets DM to Accept/Decline -> payment processed -> item transferred |
| **Give Item** | Select item -> select recipient -> select recipient's character -> enter your character name -> item transferred (no payment). If item is cyberware and recipient is a Ripperdoc, item goes to their clinic stock instead |
| **Sell to Store** | Select gun -> select store owner -> select your selling character -> enter price -> store owner gets DM to Accept/Decline -> payment processed -> item moves to store inventory |

**Manage Characters sub-menu:**

| Button | Flow |
|--------|------|
| **Create Character** | Bot prompts for a name (60s timeout) -> validates length and uniqueness -> creates character |
| **View Characters** | Shows all your active and inactive characters |
| **Deactivate Character** | Select an active character -> confirm -> character set to inactive (items preserved) |
| **Reactivate** | (Shown when you have inactive characters) Select an inactive character -> confirm -> character set to active |

### Gun Store Hub

**Channel:** `#gun-store` (config: `GUN_HUB_CHANNEL_ID`)
**Posted by:** `!gunstore` (administrator only)
**Who can use it:** Gun store owners (`WHOLESALER_STORE_ROLE_IDS`), gun store employees (`GUN_STORE_EMPLOYEE_ROLE_ID`), and administrators

| Row | Button | What it does |
|-----|--------|-------------|
| 0 | **Buy from Wholesale** | Browse this week's gun rotation (or Black Market catalog if applicable), select a gun, pick quantity, pay from your balance. **Owner only** -- employees cannot buy wholesale |
| 0 | **Wholesale List** | View the current wholesale lots without buying |
| 1 | **Sell to Customer** | Select a gun from your store stock -> select customer (user picker) -> select customer's character -> enter price -> customer gets DM to Accept/Decline -> payment processed -> item added to customer's inventory. Controlled/restricted items require per-character buyer approval |
| 1 | **My Store Inventory** | View your store's current stock (employees see a store picker if they work at multiple stores) |
| 2 | **Manage Store** | Create store, change name, transfer ownership, close store |
| 2 | **Manage Employees** | Add employee, remove employee, view employees |
| 2 | **Manage Buyers** | Approve buyer (per-character), unapprove buyer, view approved buyers |

### Ripperdoc Hub

**Channel:** `#ripperdoc` (config: `RIPPERDOC_HUB_CHANNEL_ID`)
**Posted by:** `!ripperdoc` (administrator only)
**Who can use it:** Ripperdoc owners (`RIPPERDOC_OWNER_ROLE_ID`), Ripperdoc employees (`RIPPERDOC_EMPLOYEE_ROLE_ID`), anyone with the Ripperdoc role (`RIPPERDOC_ROLE_ID`), and administrators

| Row | Button | What it does |
|-----|--------|-------------|
| 0 | **Buy from Wholesale** | Browse this week's cyberware rotation, select an item, pick quantity, pay from your balance. **Owner only** |
| 0 | **Wholesale List** | View the current CW wholesale lots |
| 1 | **Sell to Patient** | Select item from your stock -> select patient -> select patient's character -> enter price -> patient gets DM to Accept/Decline -> payment processed -> item added to patient's inventory, removed from your stock |
| 1 | **Install on Patient** | Same as sell but can be free (price = 0). Used for comped installs or staff-directed operations |
| 2 | **Manage Store** | Create clinic, change name, view stock, transfer ownership, close clinic |
| 2 | **Manage Employees** | Add employee, remove employee, view employees |
| 3 | **Checkup** | Select a patient -> removes their cyberware checkup role, resets their medication streak to 0. Logs to ripperdoc log channel |

### Fixer Hub

**Channel:** `#fixer-hub` (config: `FIXER_HUB_CHANNEL_ID`)
**Posted by:** `!fixer` (administrator only)
**Who can use it:** Fixers (`FIXER_ROLE_ID`) and administrators

Top-level buttons open sub-menus:

| Button | Sub-menu |
|--------|----------|
| **Player** | View Inventory (pick a player), Add Item, Remove Item, Reassign Item, Start LOA (for a player), End LOA (for a player) |
| **Store** | View Gun Store, View Ripperdoc Store, View Stock, Add Gun (to store), Add Cyberware (to clinic), Remove Gun, Remove Cyberware |
| **Wholesaler** | Remove Gun Lot, Remove CW Lot |

**Add Item flow:** Select player (user picker) -> enter item name -> select item type (gun/cyberware/gear/misc) -> select restriction (basic/controlled/restricted) -> enter quantity -> select character -> items created with unique UUIDs

**Remove Item flow:** Select player -> see their inventory -> select item -> confirm removal

**Reassign Item flow:** Select player -> see their inventory -> select item -> select new character -> item moved

**Item History:** Select source (Player Item or Store Item) -> select player/store -> select item -> see full audit trail (created, traded, given, sold, installed)

### Admin Panel

**Channel:** `#admin-panel` (config: `ADMIN_HUB_CHANNEL_ID`)
**Posted by:** `!admin` (administrator only)
**Who can use it:** Fixers (`FIXER_ROLE_ID`) and administrators

| Row | Button | What it does |
|-----|--------|-------------|
| 0 | **Item History** | Look up the full audit trail for any item |
| 0 | **Player Inventory** | View any player's inventory |
| 1 | **Wholesale Stock** | View current gun and CW wholesale lots |
| 2 | **Restock Gun Wholesale** | Force a fresh weekly gun rotation |
| 2 | **Clear Gun Wholesale** | Remove all gun wholesale lots (with confirmation) |
| 3 | **Restock CW Wholesale** | Force a fresh weekly cyberware rotation |
| 3 | **Clear CW Wholesale** | Remove all CW wholesale lots (with confirmation) |
| 4 | **Set Gun Sheet** | Set the Google Sheet URL for the gun catalog |
| 4 | **Set CW Sheet** | Set the Google Sheet URL for the cyberware catalog |
| 4 | **Reload Sheets** | Refresh both catalogs from their configured sheets |

---

## Player Inventory System

Every item in the game is tracked as a row in the `player_inventory` database table.

**Item fields:**
- `item_id` -- unique UUID
- `owner_id` -- Discord user ID of the owner
- `character_name` -- which character owns this item
- `item_type` -- `gun`, `cyberware`, `gear`, or `misc`
- `name` -- item display name
- `restriction` -- `basic`, `controlled`, or `restricted`
- `description` -- optional text
- `price_paid` -- what was paid for this item
- `seller_id` -- who sold it
- `seller_name` -- seller's display name at time of sale
- `acquired_at` -- when the item was obtained

**Key behaviors:**
- Items are grouped by character in the inventory display
- When you have multiples of the same item, they're collapsed into one row with a count
- FIFO ordering: the oldest item in a group is traded/given first
- Controlled and restricted items cannot be traded player-to-player
- Every item event (created, traded, given, sold to store, installed) is recorded in the `item_history` table

---

## Character System

Characters live in the `characters` database table.

**Fields:** `character_id` (UUID), `discord_user_id`, `character_name`, `normalized_character_name` (lowercase, for uniqueness), `status` (active/inactive), `created_at`, `updated_at`, `deactivated_at`, `reactivated_at`

**Uniqueness:** Character names are unique per player (case-insensitive). Two different players can have characters with the same name.

**Max name length:** 64 characters

**Lifecycle:**
1. **Create** -- via Player Hub -> Manage Characters -> Create Character
2. **Active** -- character can own items, be selected in trade/give flows
3. **Deactivate** -- character is retired; items are preserved but the character won't appear in selection dropdowns for new transactions
4. **Reactivate** -- character returns to active status

---

## Gun Store System

### Standard Stores

**Supply chain:** Google Sheet (master gun list) -> Gun catalog DB table -> Weekly wholesale rotation -> Store owners buy from wholesale -> Store owners sell to players

**Weekly rotation:** Every Monday (triggered by the cyberware weekly process), a fresh set of guns is randomly selected from the catalog. Configurable settings control lot counts and quantity ranges per tier (L/M/H).

**Store structure:** Each store is identified by `{guild_id}:{owner_id}`. Stores have: owner, lots (inventory), employees, controlled_buyers list, store_type (standard or black_market), and optional nickname.

**Sell flow:**
1. Store owner clicks Sell to Customer
2. Selects a gun from their stock
3. Selects the customer (user picker)
4. Selects the customer's character
5. Enters the price
6. If the gun is controlled/restricted, the system checks if the customer's character is on the approved buyer list. If not, an inline approval flow is presented to the store owner
7. Customer receives a DM with Accept/Decline buttons
8. On accept: customer is debited, store owner is credited, gun is removed from store stock, item is added to customer's inventory, receipt posted to audit channel

### Black Market

Designated operators (listed in `config.BLACK_MARKET_OWNER_IDS`) or stores with `store_type == "black_market"` get a different wholesale experience:

- Instead of the weekly rotation, they see the **full gun catalog** filtered to only controlled and restricted weapons
- Prices are multiplied by `config.BLACK_MARKET_PRICE_MULTIPLIER`
- Synthetic lots with high availability (99 per item) -- no scarcity
- Purchases do NOT decrement the normal wholesale lots
- Logged as `black_market_buy` events

### Controlled-Weapon Approvals

- Approval is **per-character**, not per-player
- Flow: Manage Buyers -> Approve Buyer -> select player (user picker) -> select which of their characters to approve
- Unapprove: shows only currently-approved characters in the dropdown
- Backward compatible with legacy per-player approval entries
- When selling a controlled/restricted weapon, the system checks if the buyer's specific character is approved

### Employee System (Gun Stores)

- **Owner role:** `WHOLESALER_STORE_ROLE_IDS` (can be a single ID or a list)
- **Employee role:** `GUN_STORE_EMPLOYEE_ROLE_ID`
- Employees are listed per-store in the `employees` array
- **Employees can:** view inventory, sell to customers
- **Employees cannot:** buy from wholesale, manage store settings, manage employees, manage buyer approvals
- Add/remove employees from the Manage Employees button

---

## Cyberware Shop System

### Ripperdoc Stores

**Supply chain:** Google Sheet (cyberware catalog) -> CW catalog DB table -> Weekly wholesale rotation -> Ripperdocs buy from wholesale -> Ripperdocs sell/install to patients

**Store structure:** Each clinic is identified by `rd:{guild_id}:{owner_id}`. Clinics have: owner, employees, stock (JSON file per Ripperdoc), and optional nickname.

**Sell flow:**
1. Ripperdoc clicks Sell to Patient
2. Selects item from their stock (grouped, FIFO)
3. Selects the patient
4. Selects the patient's character
5. Enters the price
6. Patient receives a DM with Accept/Decline
7. On accept: patient debited, Ripperdoc credited, item removed from Ripperdoc stock, added to patient's inventory

**Install flow:** Same as sell, but price can be 0 (free install).

**Checkup flow:**
1. Ripperdoc clicks Checkup
2. Selects a patient (user picker)
3. If the patient has the checkup role, it's removed and their medication streak resets to 0
4. Logged to the Ripperdoc log channel

### Employee System (Ripperdoc)

- **Owner role:** `RIPPERDOC_OWNER_ROLE_ID`
- **Employee role:** `RIPPERDOC_EMPLOYEE_ROLE_ID`
- The general `RIPPERDOC_ROLE_ID` also grants menu access (but not all operations)
- **Employees can:** view inventory, sell/install to patients
- **Employees cannot:** buy from wholesale, manage clinic settings, manage employees

---

## Economy System

### Rent Collection

**Automatic:** Runs at midnight (server timezone) on the 1st of each month when `auto_collect_rent` is enabled.

**Startup catch-up:** If the bot was down on the 1st and comes back within 3 days, it runs automatically. After day 3, the admin (`REPORT_USER_ID`) is notified via DM.

**What's charged (per member):**
1. Baseline living cost ($500 default) -- skipped if on LOA
2. Housing rent (by tier) -- skipped if on LOA
3. Business rent (by tier) -- always charged
4. Trauma Team subscription -- skipped if on LOA

**Deduction order:** Cash first, then bank for the remainder.

**Double-charge protection:** Uses calendar-month boundaries via the `payment_labels` table. If any collection label (`collect_rent_after`, `collect_housing_after`, `collect_business_after`, `collect_trauma_after`) was recorded this calendar month, the member is skipped.

**Cyberware meds** are NOT included in rent collection -- they're handled separately by the weekly cyberware process.

**Manual commands:** `!collect_rent`, `!collect_housing @user`, `!collect_business @user`, `!collect_trauma @user`. All support `-force` to override the monthly guard and `-v` for verbose output.

### Passive Income

Business owners earn passive income based on their tier and how many times they opened their shop this month:

- **Tier 0:** Flat scale ($150/$250/$350/$500 for 1-4 opens)
- **Tiers 1-3:** Percentage of base rent (25%/40%/60%/80% for 1-4 opens)

Income is paid immediately when `!open_shop` is used (or the Open Shop button is clicked).

### Attendance

- Available on Sundays and during active Fixer events
- Reward: $250 (configurable via `bot_config`)
- Can be done via `!attend` command or the Attend button on the Player Hub
- Logged in the `attendance_log` table

### Leave of Absence

- Toggle via `!start_loa` / `!end_loa` or the Player Hub buttons
- Adds/removes the LOA role
- While on LOA: baseline living cost, housing rent, and Trauma Team subscription are skipped during rent collection
- Business rent is still charged

### Trauma Team

- `!call_trauma` pings the Trauma Team notification channel
- Trauma Team subscriptions (Silver/Gold/Plat/Diamond) are charged during rent collection
- Costs are configurable via `bot_config`

### Cyberware Medication

- Weekly process runs every Monday at midnight (server timezone)
- Members with a cyberware role (Medium/High/Extreme) who haven't had a checkup get charged
- Cost formula: `base_factor * 2^(weeks-1)`, capped at the tier maximum
- Members who had a checkup (checkup role removed by a Ripperdoc) get their streak reset
- Members on LOA or with the Ripperdoc role are exempt

---

## Roles and Permissions

| Role | Config Key | What it grants |
|------|-----------|----------------|
| Fixer | `FIXER_ROLE_ID` | Fixer Hub access, player management, item add/remove/reassign, RP management, DM relay, character sheet management |
| Administrator | Discord permission | Everything Fixers can do plus system control, config editing, panel posting, rent collection |
| Gun Store Owner | `WHOLESALER_STORE_ROLE_IDS` | Gun Store Hub access, wholesale buying, selling, store management |
| Gun Store Employee | `GUN_STORE_EMPLOYEE_ROLE_ID` | Gun Store Hub access (sell and view only, no wholesale buying or store management) |
| Ripperdoc Owner | `RIPPERDOC_OWNER_ROLE_ID` | Ripperdoc Hub full access |
| Ripperdoc Employee | `RIPPERDOC_EMPLOYEE_ROLE_ID` | Ripperdoc Hub access (sell/install and view only) |
| Ripperdoc | `RIPPERDOC_ROLE_ID` | Ripperdoc Hub menu access, checkup commands, cyberware status commands |
| CS Approver | `CS_APPROVER_ROLE_ID` | Character sheet search |
| Verified | `VERIFIED_ROLE_ID` | Required for attendance |
| Approved | `APPROVED_ROLE_ID` | Required for rent collection eligibility |
| LOA | `LOA_ROLE_ID` | Exempts from baseline/housing/Trauma fees |
| NPC | `NPC_ROLE_ID` | NPC role button |
| Trauma Team | `TRAUMA_TEAM_ROLE_ID` | Trauma Team pings |
| Cyberware (Medium/High/Extreme) | `CYBER_MEDIUM_ROLE_ID`, `CYBER_HIGH_ROLE_ID`, `CYBER_EXTREME_ROLE_ID` | Subject to weekly medication costs |
| Cyberware Checkup | `CYBER_CHECKUP_ROLE_ID` | Indicates a checkup is due |

---

## System Control Flags

Toggled via `!enable_system <name>` / `!disable_system <name>` (administrator only).
View all with `!system_status`.

| Flag | Default | Controls |
|------|---------|----------|
| `cyberware` | ON | Weekly cyberware medication process |
| `cyberware_shop` | ON | Ripperdoc marketplace |
| `gun_shop` | ON | Gun store system |
| `player_inventory` | **OFF** | Player inventory system |
| `attend` | ON | Attendance logging |
| `open_shop` | ON | Business opening logging |
| `loa` | ON | Leave of absence |
| `housing_rent` | ON | Housing rent collection |
| `business_rent` | ON | Business rent collection |
| `trauma_team` | ON | Trauma Team features |
| `dm` | ON | DM relay system |
| `auto_collect_rent` | **OFF** | Automatic monthly rent collection |

---

## Text Commands Reference

### Player Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `!roll [XdY+Z]` | | Roll dice (e.g., `!roll 2d6+3`) |
| `!due` | | Show estimated monthly costs |
| `!paydue` | `!pay_due` | Pay monthly obligations early |
| `!last_payment` | | View your last automated payment |
| `!attend` | | Log attendance (Sundays/events) |
| `!open_shop` | `!openshop`, `!os` | Log business opening (Sundays/events) |
| `!start_loa` | `!loa`, `!goloa` | Start Leave of Absence |
| `!end_loa` | `!endloa`, `!backloa` | End Leave of Absence |
| `!call_trauma` | `!trauma`, `!tt` | Ping Trauma Team |
| `!paycyberware` | `!pay_cyberware` | Pay cyberware meds manually |
| `!helpme` | | Player help |
| `!helpplayer` | | Player Hub guide |

### Fixer Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!dm @user [message]` | Send anonymous DM | Fixer |
| `!start_rp @user1 @user2...` | Create private RP channel | Fixer or Admin |
| `!end_rp` | End RP and archive to log forum | Fixer or Admin |
| `!event_start` | Enable attend/open_shop for 4 hours | Fixer |
| `!search_characters <keyword>` | Search character sheets | Fixer or CS Approver |
| `!retire` | Move retired character threads | Fixer |
| `!move_npcs` | Move NPC threads | Fixer |
| `!unretire <thread_id>` | Restore retired thread | Fixer |
| `!export_threads #channel` | Export threads to HTML | Fixer |
| `!backup_sheets` | Backup character sheets | Fixer |
| `!item_history` | Look up item audit trail | Fixer or Admin |
| `!helpfixer` | Fixer help | Anyone |
| `!helpguns` | Gun store guide | Store Owner, Fixer, or Admin |
| `!helpcyberware` | Ripperdoc guide | Ripperdoc, Fixer, or Admin |

### Ripperdoc Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!checkup @member` | Remove checkup role | Ripperdoc |
| `!collect_cyberware @member` | Manually charge meds | Ripperdoc, Fixer, or Admin |
| `!simulate_cyberware [@member] [week]` | Preview med costs | Ripperdoc, Fixer, or Admin |
| `!cyberware_status` | Show all cyberware users | Ripperdoc, Fixer, or Admin |
| `!checkup_report` | Weekly checkup/paid/unpaid report | Ripperdoc, Fixer, or Admin |
| `!give_checkup_role [@member]` | Assign checkup role | Ripperdoc, Fixer, or Admin |
| `!weeks_without_checkup @member` | Show streak | Ripperdoc or Fixer |
| `!manual_cyberware_log @member <weeks>` | Manually set streak | Ripperdoc, Fixer, or Admin |

### Admin Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!collect_rent [@user] [-v] [-force]` | Run rent collection | Admin |
| `!collect_housing @user [-v] [-force]` | Collect housing rent | Admin |
| `!collect_business @user [-v] [-force]` | Collect business rent | Admin |
| `!collect_trauma @user [-v] [-force]` | Collect Trauma Team sub | Admin |
| `!simulate_rent [@user] [-v]` | Dry-run rent collection | Admin |
| `!simulate_all [@user]` | Simulate rent + cyberware | Admin |
| `!trigger_auto_rent` | Force auto rent cycle | Admin |
| `!mark_paid @user [note]` | Mark user as paid | Admin |
| `!list_deficits` | List underfunded members | Admin |
| `!backup_balances` | Snapshot all balances | Admin |
| `!backup_balance @user` | Snapshot one balance | Admin |
| `!restore_balances <file>` | Restore from snapshot | Admin |
| `!restore_balance @user [file]` | Restore one balance | Admin |
| `!enable_system <name>` | Enable subsystem | Admin |
| `!disable_system <name>` | Disable subsystem | Admin |
| `!system_status` | Show all system flags | Admin |
| `!config list/get/set` | View/edit economy config | Admin |
| `!reload_config` | Reload config from DB | Admin |
| `!db_health` | Database status | Admin |
| `!migrate_json_store` | One-time data migration | Admin |
| `!reindex_tickets [limit]` | Rebuild ticket search index | Admin |
| `!search_tickets <query>` | Search ticket index | Admin |
| `!shutdown_bot` | Clean shutdown | Admin |
| `!backup_now` | Database backup | Fixer |
| `!backup_status` | Backup info | Fixer |
| `!restore_db` | Database restore | Fixer |
| `!helpadmin` | Admin help | Anyone |

---

## Configuration

### Environment Variables (secrets)

| Variable | Purpose |
|----------|---------|
| `TOKEN` | Discord bot token |
| `UNBELIEVABOAT_API_TOKEN` | UnbelievaBoat economy API |
| `DATABASE_URL` | Dev PostgreSQL connection string |
| `PROD_DATABASE_URL` | Production PostgreSQL (takes priority) |
| `GDRIVE_BACKUP_FOLDER_ID` | Google Drive backup folder |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | Google service account credentials |
| `BACKUP_RETENTION_DAYS` | How long to keep backups (default: 30) |
| `CYBERWARE_SHOP_SHEET_URL` | CW catalog Google Sheet URL |
| `DISABLE_KEEP_ALIVE` | Disable the Flask health server |
| `PORT` | Flask health server port (default: 5000) |

### Config Module (config.py)

All IDs, paths, and feature settings. Key groups:

**Guild & Roles:** `GUILD_ID`, `FIXER_ROLE_ID`, `FIXER_ROLE_NAME`, `RIPPERDOC_ROLE_ID`, `RIPPERDOC_OWNER_ROLE_ID`, `RIPPERDOC_EMPLOYEE_ROLE_ID`, `WHOLESALER_STORE_ROLE_IDS`, `GUN_STORE_EMPLOYEE_ROLE_ID`, `CS_APPROVER_ROLE_ID`, `VERIFIED_ROLE_ID`, `APPROVED_ROLE_ID`, `NPC_ROLE_ID`, `LOA_ROLE_ID`, `TRAUMA_TEAM_ROLE_ID`, `CYBER_CHECKUP_ROLE_ID`, `CYBER_MEDIUM_ROLE_ID`, `CYBER_HIGH_ROLE_ID`, `CYBER_EXTREME_ROLE_ID`

**Hub Channels:** `PLAYER_HUB_CHANNEL_ID`, `GUN_HUB_CHANNEL_ID`, `RIPPERDOC_HUB_CHANNEL_ID`, `FIXER_HUB_CHANNEL_ID`, `ADMIN_HUB_CHANNEL_ID`

**Log Channels:** `AUDIT_LOG_CHANNEL_ID`, `NIGHTCITYBOT_LOG_CHANNEL_ID`, `GUN_LOG_CHANNEL_ID`, `CYBERWARE_LOG_CHANNEL_ID`, `GEAR_MISC_LOG_CHANNEL_ID`, `RIPPERDOC_LOG_CHANNEL_ID`, `RENT_LOG_CHANNEL_ID`, `EVICTION_CHANNEL_ID`, `TICKETY_LOG_CHANNEL_ID`, `DM_INBOX_CHANNEL_ID`, `GUN_APPROVALS_CHANNEL_ID`, `ATTENDANCE_CHANNEL_ID`, `BUSINESS_ACTIVITY_CHANNEL_ID`, `TRAUMA_NOTIFICATIONS_CHANNEL_ID`, `GROUP_AUDIT_LOG_CHANNEL_ID`

**Forum Channels:** `RP_LOG_FORUM_CHANNEL_ID`, `TRAUMA_FORUM_CHANNEL_ID`, `CHARACTER_SHEETS_CHANNEL_ID`, `RETIRED_SHEETS_CHANNEL_ID`, `NPC_SHEETS_CHANNEL_ID`, `RP_IC_CATEGORY_ID`

**Black Market:** `BLACK_MARKET_OWNER_IDS` (list of user IDs), `BLACK_MARKET_PRICE_MULTIPLIER`

**Economy Timing:** `TIMEZONE`, `RENT_COLLECTION_HOUR`, `RENT_COLLECTION_MINUTE`

**Paths:** `BASE_DIR`, `BALANCE_BACKUP_DIR`, `CHARACTER_BACKUP_DIR`, `RENT_AUDIT_DIR`, `WHOLESALER_DATA_DIR`, `CYBERWARE_SHOP_DATA_DIR`

### DB-Backed Economy Config (editable at runtime via `!config set`)

| Key | Default | Description |
|-----|---------|-------------|
| `baseline_living_cost` | 500 | Monthly baseline fee |
| `business_tier_0_rent` | 0 | Tier 0 business rent |
| `business_tier_1_rent` | 2000 | Tier 1 business rent |
| `business_tier_2_rent` | 3000 | Tier 2 business rent |
| `business_tier_3_rent` | 5000 | Tier 3 business rent |
| `housing_tier_1_rent` | 1000 | Tier 1 housing rent |
| `housing_tier_2_rent` | 2000 | Tier 2 housing rent |
| `housing_tier_3_rent` | 3000 | Tier 3 housing rent |
| `trauma_silver_cost` | 1000 | Trauma Team Silver |
| `trauma_gold_cost` | 2000 | Trauma Team Gold |
| `trauma_plat_cost` | 4000 | Trauma Team Plat |
| `trauma_diamond_cost` | 10000 | Trauma Team Diamond |
| `tier0_income_1_open` | 150 | Tier 0 income for 1 open/month |
| `tier0_income_2_open` | 250 | Tier 0 income for 2 opens/month |
| `tier0_income_3_open` | 350 | Tier 0 income for 3 opens/month |
| `tier0_income_4_open` | 500 | Tier 0 income for 4 opens/month |
| `open_percent_1` | 0.25 | Income % of rent for 1 open |
| `open_percent_2` | 0.40 | Income % of rent for 2 opens |
| `open_percent_3` | 0.60 | Income % of rent for 3 opens |
| `open_percent_4` | 0.80 | Income % of rent for 4 opens |
| `attend_reward` | 250 | Attendance reward |
| `cyber_max_cost_medium` | 2000 | Max weekly med cost (medium) |
| `cyber_max_cost_high` | 5000 | Max weekly med cost (high) |
| `cyber_max_cost_extreme` | 10000 | Max weekly med cost (extreme) |

---

## Database Tables

25 tables in PostgreSQL:

| Table | Purpose |
|-------|---------|
| `json_store` | Legacy KV blob store (migration source only) |
| `attendance_log` | Attendance records (user_id + timestamp) |
| `ticket_index` | Searchable ticket index |
| `business_open_log` | Shop opening events |
| `last_payment` | Most recent rent payment summary per user |
| `rent_runs` | Rent collection run history |
| `system_settings` | Subsystem enable/disable flags |
| `cyberware_status` | Per-user medication streak tracking |
| `cyberware_meta` | Cyberware system metadata |
| `cyberware_weekly_runs` | Weekly cyberware run results |
| `dm_threads` | User-to-thread DM mappings |
| `wholesale_lots` | Gun wholesale inventory |
| `wholesaler_stores` | Store metadata |
| `wholesaler_shops` | Shop registry |
| `wholesaler_pending_payouts` | Failed seller payouts awaiting retry |
| `wholesaler_settings` | Wholesaler configuration |
| `wholesaler_transactions` | Transaction audit log |
| `bot_config` | Runtime-editable economy constants |
| `payment_labels` | Double-charge protection timestamps |
| `cyberware_catalog` | CW master item list |
| `gun_catalog` | Gun master item list |
| `characters` | Player character roster |
| `player_inventory` | All player-owned items |
| `pending_transfers` | Failed trade/sale recovery records |
| `item_history` | Full item audit trail |

---

## Infrastructure

**Hosting:** Replit VM with Flask health endpoints (`/`, `/healthz`, `/readyz`)

**Database:** PostgreSQL via `asyncpg` with connection pooling (1-5 connections), automatic retry on transient errors, and write-failure alerting to the audit channel

**API:** UnbelievaBoat REST API with 5-concurrent-request semaphore and exponential backoff on 429 rate limits

**Backups:** Automated Google Drive backups with configurable retention. Full database export/import via gzipped JSON.

**Testing:** 950+ tests across 105+ files, 66% coverage floor

**Instance locking:** File lock (`fcntl`) prevents duplicate bot instances

**Startup validation:** Verifies all configured roles exist, all channels exist and are the correct type (especially ForumChannels), bot has required permissions, rent collection config is valid, and Google Drive backup config is present

**Error handling:** All Views extend `SafeView` (catches unhandled exceptions, sends ephemeral error message). All DM confirmation views enforce `interaction_check`. Expired interaction tokens are caught gracefully. Timed-out views auto-delete or show a timeout message.
