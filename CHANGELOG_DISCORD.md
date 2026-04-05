# 📋 NightCityBot — Changelog (March 29 – April 5, 2026)

---

## 🏪 Store Management

- **Black Market** — Added a second gun store type for Shyzuki, operating alongside the standard gun store with its own wholesale stock, employees, and buyer approvals.
- **Store Picker** — Gun store and Ripperdoc "View Inventory" buttons now show a store picker dropdown so you can browse any store's stock, not just your own.
- **Manage Store Menus** — Both Gun Store and Ripperdoc hubs now have full Manage Store sub-menus: Create Store, Rename Store, Transfer Ownership (with DM confirmation), and Close Store.
- **Employee System** — Owners can add/remove employees from their store. Employees receive their role automatically on hire and lose it on removal. Hiring sends a DM confirmation to the employee before finalizing.
- **Buyer Approvals Per Character** — Controlled-weapon buyer approvals now target specific characters, not just players. The approve flow is a two-step picker (player → character). Unapprove shows only currently-approved characters.

---

## 🔫 Gun & Cyberware Displays

- **Guns Grouped by Class** — Wholesale lists, store inventories, and player inventories now display guns grouped under headers (Pistols, SMGs, Shotguns, etc.) with damage type and class shown per item.
- **Cyberware Grouped by Slot** — All cyberware displays are now organized by body slot in a fixed order (Neural → Ocular → Auditory → … → Miscellaneous) with CWP and price shown per item. Row numbers stay consistent between the display and the action you take.
- **Item Details Everywhere** — Gun class, damage type, and level now propagate through wholesale buy, store add, player sale, trade, give, and admin commands. Cyberware CWP and slot are shown in all inventory views.

---

## 💰 Economy & Transactions

- **Custom Item Costs** — Guns and cyberware can now have custom wholesale and sale prices set per item, overriding the default cost tables.
- **Refund on Failure** — If a sale or install fails after money has been deducted (save error, out-of-stock race condition, etc.), the buyer is automatically refunded.
- **Race Condition Guards** — Trade and sell flows now lock properly to prevent double-spending or duplicate item transfers when buttons are clicked quickly.
- **Restriction Retry Loop** — When adding a restricted weapon to your store, you can now retry the restriction confirmation as many times as needed without restarting the whole flow.

---

## 🖥️ Hub Panel Improvements

- **Guide Embeds** — Every hub panel (Player, Gun Store, Ripperdoc, Fixer, Admin) now shows a detailed "How It Works" guide explaining each button, with the action buttons attached directly below. No more double-embed clutter.
- **Player Hub Layout** — Reorganized into sub-view menus. Added Attend, Open Shop, and View Due buttons (row 3). Added Checkup button to Ripperdoc panel.
- **LOA Buttons** — Leave of Absence request and cancel buttons added to relevant panels.
- **All Stores Visible** — "View gun stores" now shows every store in the database, not just stores belonging to users with a specific role.
- **Panel Messaging** — You can now send messages directly from panel interactions where needed.

---

## ✨ Quality-of-Life

- **Ephemeral Auto-Delete** — All bot responses to button clicks now auto-delete after 5 minutes, keeping channels clean. No more stale messages with broken buttons.
- **5-Minute Interaction Timeout** — All interactive menus (sell setup, DM confirmations, store management, etc.) now give you 5 minutes to complete your action, up from 30–60 seconds.
- **Sell Flow UX** — Gun and cyberware sell flows now show your current selections (character, item, price) inline on the main message as you pick them, instead of popping up separate confirmation messages.
- **All Error Messages Private** — Decline notices, balance errors, refund confirmations, out-of-stock warnings, and save failures during sell/install flows are now ephemeral (only you see them).
- **DM Confirmations** — All sell and trade operations now send a DM to the buyer/patient with Accept/Decline buttons. No more public channel confirmations.
- **No More Startup Spam** — The wholesale inventory snapshot that was flooding #gun-logs on every bot restart has been removed. Snapshots now only fire on manual shutdown.

---

## 🛡️ Bug Fixes

- Fixed CW wholesale buy "Stock depleted" error when multiple lots share the same name.
- Fixed PriceSelectView hanging when a custom price was entered.
- Fixed fixer hub defer ordering that caused interaction failures.
- Fixed rent sentinel timing and RentResult typo.
- Fixed Ripperdoc store lock not releasing properly.
- Fixed stale character_id persisting across different flows.
- Fixed inventory restore not handling new item fields.
- Fixed views not stopping cleanly on timeout (broken buttons left behind).
- Fixed financial calculations rounding incorrectly in several transaction types.
- Fixed security gaps allowing unauthorized store management actions.
- Fixed several commands crashing when used in DMs instead of server channels.
- Fixed role logging and timezone handling for accurate audit data.

---

## 📊 Under the Hood

- **982 tests**, all passing
- Full bot documentation written and maintained
- Dead code cleanup: removed unused imports, hardcoded role IDs, and orphaned functions
- Standardized all help commands to reflect current bot functionality
- Log channel routing verified and corrected across all cogs
