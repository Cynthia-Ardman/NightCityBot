# NightCityBot v3.0 Update

Hey everyone — big update dropping today. We've rebuilt how you interact with the bot from the ground up. Here's what's new:

---

## Hub Panels

Every role now has its own dedicated channel with a permanent panel you click to do everything. No more memorizing text commands.

- **Player Hub** — Manage your characters and inventory, trade and sell items, attend events, open your shop, view dues, and handle Leave of Absence — all from one panel.
- **Gun Store Hub** — Buy wholesale, browse wholesale stock, sell to customers, view your store inventory, manage your store, manage employees, and manage controlled-weapon buyer approvals.
- **Ripperdoc Hub** — Buy cyberware wholesale, browse wholesale stock, sell or install cyberware to patients, view your stock, manage your store, manage employees, and run patient checkups.
- **Fixer Hub** — Full management panel for player inventory, store stock, wholesaler stock, item history, and Leave of Absence — organized into Player, Store, and Wholesaler sub-menus.
- **Admin Panel** — Add, remove, reassign items, view item history, and check any player's inventory.

Each panel has a guide at the top explaining what every button does. All responses are private — only you can see them.

---

## Characters

- **Create characters** directly from the Player Hub — give them a name and they're ready to go.
- Each character has their **own separate inventory**. When you view your inventory, trade, sell, or receive items, you pick which character is involved.
- You can have **multiple characters** — the bot will ask you to choose which one whenever it matters.
- **Deactivate** characters you're no longer playing, and **reactivate** them later if you come back to them.
- **View your characters** at any time to see who you've got and their status.

---

## Inventory

- Every character's inventory now shows **full item details** — gun class, damage type, and level for weapons; CWP and body slot for cyberware.
- Guns are **grouped by weapon class** (Pistols, SMGs, Shotguns, etc.) and cyberware is **grouped by body slot** (Neural, Ocular, Auditory, Hands & Feet, Arms, Legs, etc.).
- Every item has a **unique ID** and a full **history trail** — Fixers and admins can see when it was created, who bought it wholesale, who sold it, who it was traded to, etc.

---

## Trading & Selling

- **Sell to Player** — sell an item from your character's inventory to another player for a price you set.
- **Give Item** — transfer an item to another player for free.
- **Sell to Store** — sell guns back to a gun store or cyberware back to a ripperdoc store.
- **All trades and sales** go through DM confirmation — the other person has to accept before anything happens. Both sides pick which character is involved.
- The sell flow shows your selections (character, item, price) **inline as you pick them** so you always know where you are.
- If anything goes wrong mid-transaction, the buyer gets an **automatic refund**.

---

## Gun Stores

- Store owners can **create, rename, transfer, and close** their gun store directly from the panel.
- **Buy from wholesale** to stock your store, then **sell to customers** through an interactive flow — pick the buyer, pick the gun, set the price.
- **Employee system** — hire and fire employees who can sell from your store on your behalf. They get their role automatically.
- **Controlled-weapon buyer approvals** — approve or unapprove specific characters for restricted weapons.
- **Black Market** — a separate store type with its own stock and rules.

---

## Ripperdoc Stores

- Same store management as gun stores — **create, rename, transfer, close**, and **hire/fire employees**.
- **Buy cyberware wholesale**, then **sell or install** to patients through a guided flow.
- Patients get a **DM confirmation** before any transaction goes through.

---

## Other New Features

- **Leave of Absence** — request and cancel LOA directly from the Player Hub. While on LOA your weekly dues are paused.
- **Attend** — log your event attendance from the Player Hub.
- **Open Shop** — log a business opening for a cash payout from the Player Hub.
- **View Due** — see your estimated monthly costs breakdown.
- **Checkup** — Ripperdocs can run patient checkups from their panel.
- **Manage Businesses** — view the businesses you own and where you're employed.

---

## Channel & Command Cleanup

- Most old text commands have been moved into the hub panels. The panels are the primary way to interact with the bot now.
- Each hub has its own dedicated channel — no more commands scattered everywhere.
- All menus give you **5 minutes** to finish what you're doing, and bot responses **auto-clean** after 5 minutes to keep channels tidy.
- Help commands have been updated to reflect the new layout.

---

## Gun Properties Guide

New reference doc explaining how guns work in the bot:

- **Power Level** (Low / Medium / High) — quality tiers that affect wholesale availability and pricing. Low-tier guns are the most common, high-tier are rare.
- **Type** (Power / Smart / Tech) — weapon damage categories for RP flavor.
- **Restriction** (Basic / Controlled / Restricted) — determines what approval is needed before a store owner can sell to a customer. Basic is open, Controlled requires per-character buyer approval, and Restricted needs real-time Fixer/Admin sign-off.

Full guide: https://github.com/Cynthia-Ardman/NightCityBot/blob/main/docs/gun_properties_guide.md

---

## Admin Updates

- **Seed Ripperdoc Stores** — new button on the Admin Shop panel that fills empty ripperdoc stores with 10 random starter cyberware items. Stores that already have stock are skipped.

---

## Full Documentation

For a complete breakdown of every system, command, and feature, check out the full docs:
https://github.com/Cynthia-Ardman/NightCityBot/blob/main/docs/DOCUMENTATION.md
