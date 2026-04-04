# NightCityBot v3.0 — Complete Documentation

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
8. [DM Relay System](#dm-relay-system)
9. [RP Channel Management](#rp-channel-management)
10. [Ticket Indexing System](#ticket-indexing-system)
11. [Backup & Restore System](#backup--restore-system)
12. [NPC Management](#npc-management)
13. [Roles and Permissions](#roles-and-permissions)
14. [System Control Flags](#system-control-flags)
15. [Text Commands Reference](#text-commands-reference)
16. [Configuration](#configuration)
17. [Database Tables](#database-tables)
18. [Infrastructure](#infrastructure)

---

## Overview

NightCityBot is a Discord bot for the NCRP (Night City Roleplay) server. It manages the RP economy, player inventories, gun and cyberware shops, rent collection, dice rolls, DM relays, and character management.

The primary interface is a set of **persistent hub panels** — permanent embeds in dedicated channels with buttons that players click. Each role (player, gun store owner, Ripperdoc, Fixer, admin) has its own hub channel. All hub responses are ephemeral (only visible to the person who clicked).

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
| **Sell to Player** | Select item -> select buyer (user picker) -> select buyer's character -> enter price -> buyer gets DM to Accept/Decline -> buyer's balance checked for affordability -> payment processed (cash first, then bank) -> item transferred. If the buyer's character was deactivated between confirmation and transfer, the trade is cancelled and payments are refunded |
| **Give Item** | Select item -> select recipient -> select recipient's character -> enter your character name -> item transferred (no payment). If item is cyberware and recipient is a Ripperdoc, item goes to their clinic stock instead. If the recipient's character was deactivated between confirmation and transfer, the give is cancelled |
| **Sell to Store** | Select gun -> select store owner -> select your selling character -> enter price -> store owner gets DM to Accept/Decline -> payment processed -> item moves to store inventory. If the store save fails, payments are refunded |

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
| 0 | **Buy from Wholesale** | Browse this week's gun rotation (or Black Market catalog if applicable), select a gun, pick quantity, pay from your balance. **Owner only** — employees cannot buy wholesale |
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

**Add Item flow:** Select player (user picker) -> enter item name -> select item type (gun/cyberware/gear/misc) -> select restriction (basic/controlled/restricted) -> enter quantity -> select character -> enter custom cost (or 0 for free) -> player confirms total cost via button -> payment deducted (cash first, then bank) -> items created with unique UUIDs. Gun items additionally require Power Level (low/medium/high) and Type (power/smart/tech). Cyberware items additionally require CWP (integer) and Slot (body location). If payment fails or items cannot be saved, the player is automatically refunded

**Add Gun to Store flow:** Select store owner -> select gun from wholesale catalog -> enter quantity -> enter custom cost -> store owner confirms total cost via DM -> payment deducted -> guns added to store stock. Refunded automatically if store save fails

**Add Cyberware to Store flow:** Select Ripperdoc -> select cyberware from wholesale catalog -> enter quantity -> enter custom cost -> Ripperdoc confirms total cost via DM -> payment deducted -> items added to clinic stock. Refunded automatically if inventory save fails

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
- `item_id` — unique UUID
- `owner_id` — Discord user ID of the owner
- `character_name` — which character owns this item
- `character_id` — UUID linking to the `characters` table
- `item_type` — `gun`, `cyberware`, `gear`, or `misc`
- `name` — item display name
- `restriction` — `basic`, `controlled`, or `restricted`
- `description` — optional text
- `price_paid` — what was paid for this item
- `seller_id` — who sold it
- `seller_name` — seller's display name at time of sale
- `acquired_at` — when the item was obtained
- `power_level` — (guns only) `low`, `medium`, or `high`
- `weapon_subtype` — (guns only) `power`, `smart`, or `tech`
- `cwp` — (cyberware only) integer Cyberware Points value
- `slot` — (cyberware only) body location, one of: Skeleton & Torso Musculature, Arms & Arm Attachments, Miscellaneous, Integumentary System, Neural, Universal Muscular (Arms/Legs/Tail), Hands & Feet, Ocular System, Legs & Mobility, Auditory System, Circulatory & Immune Systems

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
1. **Create** — via Player Hub -> Manage Characters -> Create Character
2. **Active** — character can own items, be selected in trade/give flows
3. **Deactivate** — character is retired; items are preserved but the character won't appear in selection dropdowns for new transactions
4. **Reactivate** — character returns to active status

**Active character guard:** When an item is traded or given, the bot re-verifies that the receiving character is still active immediately before transferring ownership. If the character was deactivated between the initial selection and the final transfer (e.g., during the DM confirmation wait), the operation is cancelled and any payments are refunded. This prevents items from being assigned to retired characters.

---

## Gun Store System

### Standard Stores

**Supply chain:** Google Sheet (master gun list) -> Gun catalog DB table -> Weekly wholesale rotation -> Store owners buy from wholesale -> Store owners sell to players

**Gun properties:** Each gun in the catalog includes a Power Level (low/medium/high) and Type (power/smart/tech). These fields are read directly from the spreadsheet and carried through wholesale lots into player inventory.

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
8. On accept: customer's balance is checked for affordability (cash + bank), then debited (cash first, remainder from bank). Store owner is credited. Gun is removed from store stock and added to customer's inventory. Receipt posted to audit channel
9. If the store save fails after payment, the customer is automatically refunded and the seller credit is reversed. If the inventory write fails, the gun is restored to store stock and payments are refunded

### Black Market

Designated operators (listed in `config.BLACK_MARKET_OWNER_IDS`) or stores with `store_type == "black_market"` get a different wholesale experience:

- Instead of the weekly rotation, they see the **full gun catalog** filtered to only controlled and restricted weapons
- Prices are multiplied by `config.BLACK_MARKET_PRICE_MULTIPLIER`
- Synthetic lots with high availability (99 per item) — no scarcity
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

**Cyberware properties:** Each cyberware item in the catalog includes CWP (Cyberware Points, integer) and Slot (body location). The 11 valid slots are: Skeleton & Torso Musculature, Arms & Arm Attachments, Miscellaneous, Integumentary System, Neural, Universal Muscular (Arms/Legs/Tail), Hands & Feet, Ocular System, Legs & Mobility, Auditory System, Circulatory & Immune Systems. These fields are read from the spreadsheet and carried through to player inventory.

**Store structure:** Each clinic is identified by `rd:{guild_id}:{owner_id}`. Clinics have: owner, employees, stock (JSON file per Ripperdoc), and optional nickname.

**Sell flow:**
1. Ripperdoc clicks Sell to Patient
2. Selects item from their stock (grouped, FIFO)
3. Selects the patient
4. Selects the patient's character
5. Enters the price
6. Patient receives a DM with Accept/Decline
7. On accept: patient's balance is checked for affordability (cash + bank), then debited (cash first, remainder from bank). Ripperdoc is credited. Item removed from Ripperdoc stock, added to patient's inventory
8. If the inventory save fails after payment, the patient is automatically refunded, seller credit is reversed, and the item is restored to stock

**Install flow:** Same as sell, but price can be 0 (free install). Same refund protections apply.

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
1. Baseline living cost ($500 default) — skipped if on LOA
2. Housing rent (by tier) — skipped if on LOA
3. Business rent (by tier) — always charged
4. Trauma Team subscription — skipped if on LOA

**Deduction order:** Cash first, then bank for the remainder. This applies to all payment flows across the bot — rent, trades, store purchases, wholesale buying, and Fixer add-item. The bot always checks total affordability (cash + bank) before attempting any deduction.

**Double-charge protection:** Uses calendar-month boundaries via the `payment_labels` table. If any collection label (`collect_rent_after`, `collect_housing_after`, `collect_business_after`, `collect_trauma_after`) was recorded this calendar month, the member is skipped.

**Cyberware meds** are NOT included in rent collection — they're handled separately by the weekly cyberware process.

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

## DM Relay System

The DM relay lets Fixers communicate anonymously with players through the bot. It also logs all incoming DMs from server members for staff visibility.

### How it works

**Sending a DM (Fixer -> Player):**

A Fixer uses `!dm @user <message>` in any server channel. The bot:
1. Delivers the message to the player's Discord DMs — the player sees it as coming from the bot, with no indication of which Fixer sent it
2. Creates (or reuses) a private thread in the DM Inbox channel named after the player
3. Logs the outgoing message in that thread with the Fixer's name for staff records
4. Deletes the original `!dm` command message to keep the server channel clean
5. Supports file attachments — attach files to the `!dm` command and their URLs are forwarded to the player as links

**Receiving a DM (Player -> Bot):**

When a player sends a DM to the bot:
1. The bot creates a private thread in the DM Inbox channel (or reuses an existing one) named `username-userid`
2. The message content and any attachments are logged in that thread
3. If the thread was previously archived, it is automatically unarchived

**Replying from a thread (two-way relay):**

Fixers can reply directly in a player's DM Inbox thread:
1. Type a normal message in the thread — it is forwarded to the player as a DM
2. The bot logs the outgoing message in the thread and deletes the Fixer's original message for anonymity
3. Only members with the Fixer role can send outgoing messages through threads
4. Files up to 8 MB can be forwarded; larger attachments are rejected with a warning

**Special commands in DM threads:**

Fixers can also execute bot commands directly from within a DM Inbox thread:
- `!roll <dice>` — rolls dice as the player and sends the result to their DMs, logged in the thread
- `!start-rp @user1 @user2...` — creates an RP channel (if no users are specified, defaults to the thread's player)
- Any other `!command` — executed normally, then the command message is deleted from the thread

**Anonymity protections:**
- All command messages sent in DM Inbox threads are auto-deleted after execution
- Deletions are recorded in the audit log for accountability
- The player never sees which Fixer sent the message

**System control:** The entire DM relay system can be toggled on or off using the `dm` system flag.

---

## RP Channel Management

The RP system allows Fixers and admins to create temporary private channels for roleplay sessions and archive them when complete.

### Starting an RP session

Use `!start_rp @user1 @user2 ...` (aliases: `!startrp`, `!rp_start`, `!rpstart`). The bot:

1. Creates a new text channel named `text-rp-<usernames>` in the configured RP category (`RP_IC_CATEGORY_ID`)
2. Sets permission overwrites so that **only** the mentioned players, the invoking Fixer, and anyone with the Fixer role can see and type in the channel. The `@everyone` role is denied access
3. The bot itself is granted read and send permissions
4. Posts a welcome message mentioning all participants and the Fixer role
5. Deletes the original `!start_rp` command from the channel where it was typed

### During an RP session

- The channel functions as a normal text channel for the permitted participants
- Any bot command typed in an RP channel (messages starting with `!`) is auto-deleted after a brief delay to keep the RP channel clean. The command still executes before deletion
- If a command deletes the channel itself (like `!end_rp`), the cleanup handles the already-deleted message gracefully

### Ending an RP session

Use `!end_rp` inside the RP channel (aliases: `!endrp`, `!rp_end`, `!rpend`). The bot:

1. Verifies the channel name starts with `text-rp-` — the command only works in RP channels
2. Creates a new thread in the RP Log Forum channel (`RP_LOG_FORUM_CHANNEL_ID`) named `GroupRP-<participants>`
3. Reads the **entire message history** of the RP channel (oldest to newest)
4. Formats each message as a timestamped log entry with the author's display name, message content, and any attachment URLs
5. Posts the formatted transcript into the log thread, batching messages to stay within Discord's character limits
6. Deletes the RP channel after the transcript is fully posted
7. If the RP Log Forum is not configured or is the wrong channel type, the bot warns staff and **does not delete the channel** to prevent data loss

---

## Ticket Indexing System

The bot automatically indexes ticket transcripts from the Tickety bot for fast searching.

### Automatic indexing

Whenever a message appears in the Tickety log channel (`TICKETY_LOG_CHANNEL_ID`), the bot checks if it looks like a ticket event. It identifies Tickety messages by:
- Checking if the message author or webhook name contains "tickety" or "ticket"
- Scanning all embed text (title, description, fields, footer, author) for ticket-related keywords

Matching messages are stored in the `ticket_index` database table with the message ID, jump URL, timestamp, title, and full searchable text extracted from all embeds.

### Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!search_tickets <query>` | Instantly search the ticket index by name, user, ID, or any text that appears in ticket embeds. Results are displayed newest-first with clickable links | Admin |
| `!reindex_tickets [limit]` | Scan the Tickety log channel history and rebuild the index. Use this once to seed the index from existing messages. Pass `0` for no limit (full history sweep). Progress updates are posted every 10,000 messages | Admin |
| `!ticket_debug [index]` | Show the raw stored text for a ticket index entry (0 = most recent). Useful for diagnosing why searches aren't matching | Admin |
| `!ticket_channel_preview [count]` | Show embed info for the most recent messages in the ticket log channel, indicating whether each would be indexed | Admin |
| `!ticket_scan [limit]` | Scan back through the log channel and show the first embed from each unique bot/author to help identify the Tickety embed format | Admin |

The index is loaded from the database on bot startup and stays in sync as new tickets arrive.

---

## Backup & Restore System

### Automated daily backups

The bot runs an automated backup every day at a configurable time (default: 4:00 AM UTC, adjustable via `BACKUP_HOUR` and `BACKUP_MINUTE` environment variables). Each backup:

1. **Exports all database tables** — every table in PostgreSQL is dumped to JSON with row counts and metadata
2. **Bundles local files** — balance backup files, character sheet backups, and rent audit files are collected and included
3. **Compresses everything** — the full bundle is gzipped into a single file named `nightcitybot_backup_YYYYMMDD_HHMMSS.json.gz`
4. **Uploads to Google Drive** — the compressed file is uploaded to the configured Google Drive folder using a service account
5. **Rotates old backups** — backups older than the retention period (default: 30 days, configurable via `BACKUP_RETENTION_DAYS`) are automatically deleted from Drive
6. **Posts to the audit log** — a summary with file name, size, table count, row count, and number of local files bundled is posted to the audit channel

### Manual commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!backup_now` | Trigger an immediate full backup to Google Drive. Shows file name, size, table/row counts, local files bundled, and a Drive link when complete | Fixer |
| `!backup_status` | Show the last successful backup time, file name, size, and Drive link. Also shows the latest backup found on Drive | Fixer |
| `!restore_db` | Without arguments: lists the 10 most recent backups on Google Drive with their IDs, sizes, and creation dates. With an ID: initiates a restore from that specific backup | Fixer |

### Restore flow

1. Run `!restore_db` to see available backups
2. Run `!restore_db <id>` with the backup's Google Drive file ID
3. The bot warns that this will **overwrite all current database data** and asks for confirmation
4. Type `CONFIRM` within 30 seconds to proceed (any other response cancels)
5. The bot downloads the backup, decompresses it, and imports all tables
6. A summary with table count and total rows restored is posted
7. The restore is logged in the audit channel

### Google Drive setup

Backups require two environment variables:
- `GDRIVE_BACKUP_FOLDER_ID` — the Google Drive folder ID where backups are stored
- `GDRIVE_SERVICE_ACCOUNT_JSON` — the full JSON credentials for a Google service account with access to that folder

Both regular Drive folders and Shared Drives are supported.

---

## NPC Management

### NPC role button

The `!npc_button` command (Fixer only) posts a persistent button in the current channel. Any server member can click it to self-assign the NPC role (`NPC_ROLE_ID`). The button survives bot restarts. If a member already has the role, they are told so. The assignment is logged in the audit channel.

### Moving NPC threads

The `!move_npcs` command (Fixer only) moves NPC character sheet threads to the designated NPC sheets forum channel (`NPC_SHEETS_CHANNEL_ID`). This is used to organize character sheets when a character transitions to NPC status.

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

Supports `!enable_system all` and `!disable_system all` to toggle everything at once.

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
| `!roll [XdY+Z]` | | Roll dice (e.g., `!roll 2d6+3`). Mention a user to roll for them |
| `!due` | | Show estimated monthly costs |
| `!paydue` | `!pay_due` | Pay monthly obligations early |
| `!last_payment` | | View your last automated payment |
| `!attend` | | Log attendance (Sundays/events) |
| `!open_shop` | `!openshop`, `!os` | Log business opening (Sundays/events) |
| `!start_loa` | `!startloa`, `!loa_start`, `!loastart` | Start Leave of Absence |
| `!end_loa` | `!endloa`, `!loa_end`, `!loaend` | End Leave of Absence |
| `!call_trauma` | `!calltrauma`, `!trauma` | Ping Trauma Team |
| `!paycyberware` | `!pay_cyberware` | Pay cyberware meds manually |
| `!helpme` | | Player help |
| `!helpplayer` | | Player Hub guide |

### Fixer Commands

| Command | Aliases | Description | Permission |
|---------|---------|-------------|------------|
| `!dm @user [message]` | | Send anonymous DM | Fixer |
| `!post <destination> [message]` | | Send a message to a text channel or thread as the bot. Supports channel mentions, names, or IDs. Can also execute commands in the target channel by prefixing the message with `!`. Attachments are forwarded | Fixer |
| `!start_rp @user1 @user2...` | `!startrp`, `!rp_start`, `!rpstart` | Create private RP channel | Fixer or Admin |
| `!end_rp` | `!endrp`, `!rp_end`, `!rpend` | End RP and archive to log forum | Fixer or Admin |
| `!event_start` | `!eventstart`, `!open_event`, `!start_event` | Enable attend/open_shop for 4 hours | Fixer |
| `!search_characters <keyword>` | `!sheet_search`, `!search_sheets` | Search character sheets | Fixer or CS Approver |
| `!retire` | | Move retired character threads | Fixer |
| `!move_npcs` | | Move NPC threads to the NPC sheets channel | Fixer |
| `!unretire <thread_id>` | | Restore retired thread | Fixer |
| `!npc_button` | | Post a persistent NPC role assignment button | Fixer |
| `!export_threads #channel` | `!exportthreads` | Export threads to an HTML file with search and collapsible views | Fixer |
| `!backup_sheets` | | Backup character sheets | Fixer |
| `!item_history` | | Look up item audit trail | Fixer or Admin |
| `!helpfixer` | | Fixer help | Anyone |
| `!helpguns` | `!helpbusiness`, `!helpshop`, `!helpstore` | Gun store guide | Store Owner, Fixer, or Admin |
| `!helpcyberware` | `!helpcw`, `!helpripper`, `!helpripperdoc` | Ripperdoc guide | Ripperdoc, Fixer, or Admin |

### Ripperdoc Commands

| Command | Aliases | Description | Permission |
|---------|---------|-------------|------------|
| `!checkup @member` | `!check-up`, `!check_up`, `!cu`, `!cup` | Remove checkup role | Ripperdoc |
| `!collect_cyberware @member` | | Manually charge meds | Ripperdoc, Fixer, or Admin |
| `!simulate_cyberware [@member] [week]` | | Preview med costs | Ripperdoc, Fixer, or Admin |
| `!cyberware_status` | | Show all cyberware users | Ripperdoc, Fixer, or Admin |
| `!checkup_report` | | Weekly checkup/paid/unpaid report | Ripperdoc, Fixer, or Admin |
| `!give_checkup_role [@member]` | | Assign checkup role to a member or all CW users if no member specified | Ripperdoc, Fixer, or Admin |
| `!weeks_without_checkup @member` | `!weekswithoutcheckup`, `!wwocup`, `!wwc` | Show streak | Ripperdoc or Fixer |
| `!manual_cyberware_log @member <weeks>` | | Manually set streak | Ripperdoc, Fixer, or Admin |

### Admin Commands

| Command | Aliases | Description | Permission |
|---------|---------|-------------|------------|
| `!collect_rent [@user] [-v] [-force]` | `!collectrent` | Run rent collection | Admin |
| `!collect_housing @user [-v] [-force]` | `!collecthousing` | Collect housing rent | Admin |
| `!collect_business @user [-v] [-force]` | `!collectbusiness` | Collect business rent | Admin |
| `!collect_trauma @user [-v] [-force]` | `!collecttrauma` | Collect Trauma Team sub | Admin |
| `!simulate_rent [@user] [-v]` | `!simulaterent` | Dry-run rent collection | Admin |
| `!simulate_all [@user]` | | Simulate rent + cyberware | Admin |
| `!trigger_auto_rent` | | Force auto rent cycle | Admin |
| `!mark_paid @user [note]` | | Mark user as paid | Admin |
| `!list_deficits` | | List underfunded members | Admin |
| `!backup_balances` | | Snapshot all balances | Admin |
| `!backup_balance @user` | | Snapshot one balance | Admin |
| `!restore_balances <file>` | | Restore from snapshot | Admin |
| `!restore_balance @user [file]` | | Restore one balance | Admin |
| `!backup_now` | | Trigger immediate Google Drive backup | Fixer |
| `!backup_status` | | Show last backup time and status | Fixer |
| `!restore_db [backup_id]` | | List backups or restore from one | Fixer |
| `!enable_system <name>` | `!enablesystem`, `!es`, `!systemenable` | Enable subsystem | Admin |
| `!disable_system <name>` | `!disablesystem`, `!ds`, `!systemdisable` | Disable subsystem | Admin |
| `!system_status` | `!systemstatus` | Show all system flags | Admin |
| `!config list/get/set/reload` | | View/edit economy config | Admin |
| `!reload_config` | | Reload config from DB | Admin |
| `!db_health` | | Database status — ping time, pool stats, failure count | Admin |
| `!shutdown_bot` | `!shutdownbot`, `!forceshutdown` | Clean bot shutdown with audit logging | Admin |
| `!backfill_logs [limit]` | | Rebuild attendance/business logs from message history | Admin |
| `!reindex_tickets [limit]` | `!reindextickets` | Rebuild the ticket search index from channel history | Admin |
| `!search_tickets <query>` | `!searchtickets`, `!ticketsearch` | Search tickets by name, user, ID, or text | Admin |
| `!ticket_debug [index]` | `!ticketdebug` | Show raw stored text for a ticket index entry | Admin |
| `!ticket_channel_preview [count]` | `!ticketchannelpreview` | Preview recent messages in ticket log channel | Admin |
| `!ticket_scan [limit]` | `!ticketscan` | Identify embed authors in ticket log channel | Admin |
| `!migrate_json_store` | | One-time data migration from legacy JSON store | Admin |
| `!helpadmin` | | Admin help | Anyone |

---

## Configuration

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
| `ticket_index` | Searchable ticket index built from Tickety log channel embeds |
| `business_open_log` | Shop opening events |
| `last_payment` | Most recent rent payment summary per user |
| `rent_runs` | Rent collection run history |
| `system_settings` | Subsystem enable/disable flags |
| `cyberware_status` | Per-user medication streak tracking |
| `cyberware_meta` | Cyberware system metadata |
| `cyberware_weekly_runs` | Weekly cyberware run results |
| `dm_threads` | User-to-thread DM relay mappings |
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
| `player_inventory` | All player-owned items (includes `power_level`, `weapon_subtype` for guns; `cwp`, `slot` for cyberware) |
| `pending_transfers` | Failed trade/sale recovery records |
| `item_history` | Full item audit trail |

---

## Infrastructure

**Hosting:** Replit VM with Flask health endpoints (`/`, `/healthz`, `/readyz`)

**Database:** PostgreSQL via `asyncpg` with connection pooling (1-5 connections), automatic retry on transient errors, and write-failure alerting to the audit channel

**API:** UnbelievaBoat REST API with 5-concurrent-request semaphore and exponential backoff on 429 rate limits

**Backups:** Automated daily Google Drive backups bundling database exports, balance snapshots, character sheet backups, and rent audit files. Compressed as gzipped JSON. Configurable retention with automatic rotation of old backups. Full in-Discord restore flow with confirmation safeguards.

**Testing:** 951 tests across 105+ files, 66% coverage floor

**Instance locking:** File lock (`fcntl`) prevents duplicate bot instances

**Startup validation:** On boot, the bot runs a comprehensive validation sequence:
1. **Config verification** — checks that all required config values are populated (role IDs, channel IDs, paths, timing settings)
2. **Role verification** — confirms every configured role ID resolves to an actual role in the guild
3. **Channel verification** — confirms every configured channel ID resolves to an actual channel, and that channels expected to be Forum Channels are the correct type
4. **Permission check** — verifies the bot has the required Discord permissions: send messages, manage messages, manage channels, manage roles, attach files, embed links
5. **Rent config validation** — checks that `RENT_COLLECTION_HOUR` and `RENT_COLLECTION_MINUTE` are valid integers in range
6. **Database health check** — pings the database with `SELECT 1` and alerts the audit channel if it fails
7. **UnbelievaBoat connection test** — fetches a test balance to confirm the economy API is reachable
8. **Google Drive config check** — validates that `GDRIVE_BACKUP_FOLDER_ID` and `GDRIVE_SERVICE_ACCOUNT_JSON` are set and the JSON is parseable
9. **Log cleanup** — removes entries for members who have left the server from attendance, open shop, and other log files
10. **Startup audit log** — posts a confirmation message to the audit channel when all checks pass

**Error handling:** All interactive Views (button panels, dropdowns, modals) extend `SafeView`, a base class that catches any unhandled exception inside a button or select callback. When an error occurs, SafeView logs the full traceback (including the view type, user, and item that triggered it) and sends an ephemeral error message to the user so they know something went wrong. This prevents one user's error from crashing the panel for everyone else. When a View times out, SafeView attempts to delete the message or, if that fails, edits it to show a timeout notice. All DM confirmation views enforce `interaction_check` to ensure only the intended recipient can click Accept/Decline. Expired interaction tokens are caught gracefully without raising errors.
