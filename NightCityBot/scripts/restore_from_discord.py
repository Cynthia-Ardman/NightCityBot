"""
Discord history restoration script.

Fetches up to a full year of message history from Discord channels and
backfills the database with attendance, shop openings, rent runs, cyberware
weekly runs, DM thread mappings, and trauma team payments.

Usage:
    python -m NightCityBot.scripts.restore_from_discord [options]

    --section SECTION   Only run one section: attendance, open_shop, rent,
                        cyberware, dm_threads, trauma_team
    --dry-run           Fetch and parse but write nothing to the database;
                        prints every record that would be written
    --limit N           Cap pages fetched per channel (100 msgs/page); use
                        --limit 2 for a 200-message smoke test

The script saves a checkpoint file (.restore_checkpoint.json) after every
page. If interrupted, the next run resumes exactly from the last cursor.
Checkpoint stores only the cursor and accumulated parsed records — no raw
Discord message objects are ever written to it.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import asyncpg

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
CHECKPOINT_FILE = Path(".restore_checkpoint.json")

CHANNEL_IDS = {
    "attendance":   1384001117125345280,
    "open_shop":    1379941898772414464,
    "rent":         1379942591721902152,
    "cyberware":    1389028820463521802,
    "dm_threads":   1379222007513874523,
    "trauma_team":  1351070651313557545,
}

ALL_SECTIONS = list(CHANNEL_IDS.keys())

_CLUSTER_GAP_RENT  = timedelta(minutes=5)
_RUN_GAP_CYBER     = timedelta(minutes=30)

# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoint(cp: dict) -> None:
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2))


def delete_checkpoint() -> None:
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()

# ---------------------------------------------------------------------------
# Discord API helpers
# ---------------------------------------------------------------------------

async def discord_get(session: aiohttp.ClientSession, url: str,
                      params: Optional[dict] = None) -> Any:
    """GET a Discord endpoint, retrying forever on 429 with exact sleep."""
    while True:
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                data = await resp.json()
                wait = float(data.get("retry_after", 1.0))
                print(f"    [rate-limited] sleeping {wait:.3f}s …")
                await asyncio.sleep(wait)
                continue
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"Discord API {resp.status}: {text[:200]}")
            return await resp.json()


async def stream_pages(
    session: aiohttp.ClientSession,
    channel_id: int,
    cp_key: str,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
):
    """
    Async generator that yields pages of messages in chronological order
    (oldest-first) using the Discord ``after`` cursor.

    After every page the checkpoint is updated with the new cursor and the
    caller's accumulated state — raw message objects are never stored in
    the checkpoint file.
    """
    cp_entry = checkpoint.get(cp_key, {})
    if cp_entry.get("done"):
        return  # Already fully fetched; caller loads its state from checkpoint

    after_cursor: str = cp_entry.get("after_cursor", "0")
    page_num: int = cp_entry.get("page_num", 0)
    url = f"{DISCORD_API}/channels/{channel_id}/messages"

    while True:
        params: dict = {"limit": 100, "after": after_cursor}
        page: list = await discord_get(session, url, params)

        if not page:
            # Merge with current checkpoint so parser_state is not lost
            checkpoint[cp_key] = {**checkpoint.get(cp_key, {}), "done": True}
            save_checkpoint(checkpoint)
            return

        # Discord returns messages in ascending (oldest-first) order for after=
        page_num += 1
        after_cursor = page[-1]["id"]   # newest message in this page

        print(f"  [{label}] page {page_num}: {len(page)} messages  cursor={after_cursor}")

        # Update cursor in checkpoint entry (caller merges parser_state after yield)
        cp_entry = {**cp_entry, "after_cursor": after_cursor, "page_num": page_num, "done": False}
        checkpoint[cp_key] = cp_entry

        yield page
        # After yield, run_channel_section has already merged parser_state into
        # checkpoint[cp_key].  Any further writes below must preserve that.

        if len(page) < 100:
            # Merge with current state so parser_state written by caller is kept
            checkpoint[cp_key] = {**checkpoint.get(cp_key, {}), "done": True}
            save_checkpoint(checkpoint)
            return

        if limit_pages and page_num >= limit_pages:
            print(f"  [{label}] --limit {limit_pages} reached, stopping")
            save_checkpoint(checkpoint)
            return

# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

MENTION_RE = re.compile(r"<@!?(\d+)>")

def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)

def _is_bot(msg: dict) -> bool:
    return msg.get("author", {}).get("bot", False)

def _mentions(content: str) -> list[str]:
    return MENTION_RE.findall(content)

# Cyberware patterns — covers both old and current bot message formats
_CY_PAID_OLD  = re.compile(r"Deducted \$[\d,]+ for cyberware meds from <@!?(\d+)> \(week (\d+)\)")
_CY_PAID_NEW  = re.compile(r"Deducted \$[\d,]+ from <@!?(\d+)> for cyberware meds")
_CY_UNPA_OLD  = re.compile(r"<@!?(\d+)> cannot pay \$[\d,]+ for immunosuppressants")
_CY_UNPA_NEW  = re.compile(r"Could not deduct \$[\d,]+ from <@!?(\d+)> for cyberware meds")
_CY_CHECKUP   = re.compile(r"checkup on <@!?(\d+)>")

# ---------------------------------------------------------------------------
# Stateful per-page parsers
# Each parser class:
#   • __init__(state)     — restore from checkpoint state dict (or None)
#   • process_page(page)  — consume one chronological page, update state
#   • to_state()          — return JSON-serialisable dict for checkpoint
#   • get_results()       — return final parsed records
# ---------------------------------------------------------------------------

class AttendanceParser:
    def __init__(self, state: Optional[dict] = None):
        s = state or {}
        self._pending: Optional[str] = s.get("pending_user")
        self._records: list[list] = s.get("records", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if _is_bot(msg):
                if self._pending and "Attendance logged" in msg.get("content", ""):
                    self._records.append([self._pending, msg["timestamp"]])
                self._pending = None
            else:
                content = msg.get("content", "").strip()
                if content.lower().startswith("!attend"):
                    self._pending = str(msg["author"]["id"])
                else:
                    self._pending = None

    def to_state(self) -> dict:
        return {"pending_user": self._pending, "records": self._records}

    def get_results(self) -> list[tuple[str, datetime]]:
        return [(_r[0], _parse_ts(_r[1])) for _r in self._records]


class OpenShopParser:
    def __init__(self, state: Optional[dict] = None):
        s = state or {}
        self._pending: Optional[str] = s.get("pending_user")
        self._records: list[list] = s.get("records", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if _is_bot(msg):
                if self._pending and "Business opening logged" in msg.get("content", ""):
                    self._records.append([self._pending, msg["timestamp"]])
                self._pending = None
            else:
                content = msg.get("content", "").strip().lower()
                if content.startswith(("!open_shop", "!openshop", "!os")):
                    self._pending = str(msg["author"]["id"])
                else:
                    self._pending = None

    def to_state(self) -> dict:
        return {"pending_user": self._pending, "records": self._records}

    def get_results(self) -> list[tuple[str, datetime]]:
        return [(_r[0], _parse_ts(_r[1])) for _r in self._records]


class RentParser:
    PAY_RE = re.compile(r"(Housing|Business) Rent paid")

    def __init__(self, state: Optional[dict] = None):
        s = state or {}
        # current_cluster: list of [ts_iso, uid, line]
        self._cluster: list[list] = s.get("current_cluster", [])
        self._runs: list[dict] = s.get("runs", [])
        self._last_payment: dict[str, str] = s.get("last_payment", {})

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if not _is_bot(msg):
                continue
            content = msg.get("content", "")
            if not self.PAY_RE.search(content):
                continue
            ts = msg["timestamp"]
            for uid in _mentions(content):
                self._add_event(ts, uid, content.strip())

    def _add_event(self, ts_iso: str, uid: str, line: str) -> None:
        ts = _parse_ts(ts_iso)
        if self._cluster:
            last_ts = _parse_ts(self._cluster[-1][0])
            if (ts - last_ts) > _CLUSTER_GAP_RENT:
                self._flush_cluster()
        self._cluster.append([ts_iso, uid, line])

    def _flush_cluster(self) -> None:
        if not self._cluster:
            return
        run_at = self._cluster[0][0]
        per_user: dict[str, str] = {}
        for ts_iso, uid, line in self._cluster:
            per_user[uid] = line
            self._last_payment[uid] = line
        self._runs.append({"run_at": run_at, "initiated_by": "restored",
                           "per_user": per_user})
        self._cluster = []

    def to_state(self) -> dict:
        return {
            "current_cluster": self._cluster,
            "runs": self._runs,
            "last_payment": self._last_payment,
        }

    def get_results(self) -> tuple[list[dict], dict[str, str]]:
        # Flush any open cluster before returning
        self._flush_cluster()
        return self._runs, self._last_payment


class CyberwareParser:
    def __init__(self, state: Optional[dict] = None):
        s = state or {}
        # current_run: list of event dicts with JSON-serialisable values
        self._run: list[dict] = s.get("current_run", [])
        self._runs: list[dict] = s.get("runs", [])
        # cyber_status: {uid: [weeks_int, last_processed_iso]}
        self._status: dict[str, list] = s.get("cyber_status", {})

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if not _is_bot(msg):
                continue
            content = msg.get("content", "")
            ts_iso = msg["timestamp"]

            m = _CY_PAID_OLD.search(content)
            if m:
                self._add(ts_iso, "paid", m.group(1), int(m.group(2)))
                continue
            m = _CY_PAID_NEW.search(content)
            if m:
                self._add(ts_iso, "paid_noweek", m.group(1), None)
                continue
            m = _CY_UNPA_OLD.search(content)
            if m:
                self._add(ts_iso, "unpaid", m.group(1), None)
                continue
            m = _CY_UNPA_NEW.search(content)
            if m:
                self._add(ts_iso, "unpaid", m.group(1), None)
                continue
            m = _CY_CHECKUP.search(content)
            if m:
                self._add(ts_iso, "checkup", m.group(1), None)

    def _add(self, ts_iso: str, kind: str, uid: str, weeks: Optional[int]) -> None:
        ts = _parse_ts(ts_iso)
        if self._run:
            last_ts = _parse_ts(self._run[-1]["ts"])
            if (ts - last_ts) > _RUN_GAP_CYBER:
                self._flush_run()
        self._run.append({"ts": ts_iso, "type": kind, "user_id": uid, "weeks": weeks})

    def _flush_run(self) -> None:
        if not self._run:
            return
        run_at = self._run[0]["ts"]
        paid, unpaid, checkup = [], [], []
        for ev in self._run:
            uid, kind, weeks = ev["user_id"], ev["type"], ev["weeks"]
            if kind == "paid":
                paid.append({"user_id": uid, "weeks": weeks})
                existing = self._status.get(uid)
                if not existing or _parse_ts(run_at) > _parse_ts(existing[1]):
                    self._status[uid] = [weeks, run_at]
            elif kind == "paid_noweek":
                paid.append({"user_id": uid, "weeks": None})
                existing = self._status.get(uid)
                if not existing or _parse_ts(run_at) > _parse_ts(existing[1]):
                    self._status[uid] = [existing[0] if existing else 0, run_at]
            elif kind == "unpaid":
                unpaid.append(uid)
            elif kind == "checkup":
                checkup.append(uid)
        self._runs.append({"run_at": run_at, "paid": paid, "unpaid": unpaid, "checkup": checkup})
        self._run = []

    def to_state(self) -> dict:
        return {"current_run": self._run, "runs": self._runs, "cyber_status": self._status}

    def get_results(self) -> tuple[list[dict], dict[str, tuple[int, datetime]]]:
        self._flush_run()
        status: dict[str, tuple[int, datetime]] = {
            uid: (v[0], _parse_ts(v[1])) for uid, v in self._status.items()
        }
        return self._runs, status

# ---------------------------------------------------------------------------
# Thread enumeration — with per-batch checkpointing
# ---------------------------------------------------------------------------

async def fetch_threads_checkpointed(
    session: aiohttp.ClientSession,
    channel_id: int,
    cp_key: str,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
) -> list[dict]:
    """
    Enumerate all threads (active + archived public + archived private) for a
    channel, saving the checkpoint after every batch to allow resume.
    Returns the complete list of thread objects.
    """
    cp = checkpoint.get(cp_key, {})
    if cp.get("all_done"):
        print(f"  [{label}] thread enumeration already complete (checkpoint)")
        return cp.get("threads", [])

    threads: list[dict] = list(cp.get("threads", []))

    # ── Active threads ─────────────────────────────────────────────────────
    if not cp.get("active_done"):
        active_data = await discord_get(
            session, f"{DISCORD_API}/channels/{channel_id}/threads/active"
        )
        active = active_data.get("threads", [])
        threads.extend(active)
        print(f"  [{label}] active threads: {len(active)}")
        cp = {**cp, "active_done": True, "threads": threads}
        checkpoint[cp_key] = cp
        save_checkpoint(checkpoint)

    # ── Archived public threads (paginated) ────────────────────────────────
    if not cp.get("public_done"):
        arch_url = f"{DISCORD_API}/channels/{channel_id}/threads/archived/public"
        before_ts: Optional[str] = cp.get("public_cursor")
        pub_page = cp.get("public_page", 0)
        pub_exhausted = False

        while True:
            params: dict = {"limit": 100}
            if before_ts:
                params["before"] = before_ts
            data = await discord_get(session, arch_url, params)
            batch = data.get("threads", [])
            threads.extend(batch)
            pub_page += 1
            print(f"  [{label}] archived-public page {pub_page}: {len(batch)} threads")
            before_ts = (
                batch[-1].get("thread_metadata", {}).get("archive_timestamp")
                if batch else None
            )
            cp = {**cp, "threads": threads, "public_cursor": before_ts,
                  "public_page": pub_page}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)  # checkpoint after every batch
            if not data.get("has_more") or not batch:
                pub_exhausted = True
                break
            if limit_pages and pub_page >= limit_pages:
                break  # stopped by --limit; NOT exhausted

        # Only mark done when API is truly exhausted, not when stopped by --limit
        if pub_exhausted:
            cp = {**cp, "public_done": True}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)

    # ── Archived private threads (paginated) ───────────────────────────────
    if not cp.get("private_done"):
        priv_url = f"{DISCORD_API}/channels/{channel_id}/threads/archived/private"
        before_ts = cp.get("private_cursor")
        priv_page = cp.get("private_page", 0)
        priv_exhausted = False
        try:
            while True:
                params = {"limit": 100}
                if before_ts:
                    params["before"] = before_ts
                data = await discord_get(session, priv_url, params)
                batch = data.get("threads", [])
                threads.extend(batch)
                priv_page += 1
                print(f"  [{label}] archived-private page {priv_page}: {len(batch)} threads")
                before_ts = (
                    batch[-1].get("thread_metadata", {}).get("archive_timestamp")
                    if batch else None
                )
                cp = {**cp, "threads": threads, "private_cursor": before_ts,
                      "private_page": priv_page}
                checkpoint[cp_key] = cp
                save_checkpoint(checkpoint)
                if not data.get("has_more") or not batch:
                    priv_exhausted = True
                    break
                if limit_pages and priv_page >= limit_pages:
                    break  # stopped by --limit; NOT exhausted
        except RuntimeError:
            priv_exhausted = True  # Bot lacks MANAGE_THREADS; nothing more to fetch

        # Only mark private_done (and all_done) when truly exhausted
        if priv_exhausted:
            all_done = cp.get("public_done", False) and priv_exhausted
            cp = {**cp, "private_done": True, "all_done": all_done, "threads": threads}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)

    print(f"  [{label}] total threads: {len(threads)}")
    return threads

# ---------------------------------------------------------------------------
# DM thread parser — name-based + relay-message fallback
# ---------------------------------------------------------------------------

# Relay message patterns written by dm_handling.py into each thread:
#   📥 **Received from DisplayName (USER_ID)**:  (incoming user DM)
#   📤 **Sent to DisplayName (USER_ID) by …:**   (outgoing fixer DM)
_RELAY_UID_RE = re.compile(
    r"(?:Received from|Sent to)\s+.{0,80}\((\d{15,20})\)"
)


async def build_dm_thread_map(
    session: aiohttp.ClientSession,
    threads: list[dict],
    checkpoint: dict,
    limit_pages: Optional[int],
) -> dict[str, int]:
    """
    Build user_id → thread_id mapping via two strategies:

    1. Name-based (primary): thread name ends with the numeric user ID,
       e.g. ``username-123456789012345678``.
    2. Relay-message fallback: for threads whose names cannot be parsed,
       stream the thread's messages and extract the user ID from the first
       relay log line (``Received from … (ID)`` or ``Sent to … (ID)``).

    Both strategies are idempotent and checkpointed.  The mapping stored
    in the checkpoint is used on resume to avoid re-reading threads whose
    user ID was already resolved.
    """
    CP_KEY = "dm_thread_map_cache"
    # Load any previously resolved mappings from checkpoint
    mapping: dict[str, int] = dict(checkpoint.get(CP_KEY, {}))

    # Build reverse lookup: thread_id → user_id (already resolved)
    resolved_tids: set[int] = {int(tid_str) for tid_str in
                                {str(v) for v in mapping.values()}}

    name_hits = 0
    relay_hits = 0

    for t in threads:
        tid = int(t["id"])
        if tid in resolved_tids:
            continue  # Already resolved on a previous run

        # ── Strategy 1: name suffix ────────────────────────────────────
        m = re.search(r"(\d{15,20})$", t.get("name", ""))
        if m:
            uid = m.group(1)
            mapping[uid] = tid
            resolved_tids.add(tid)
            name_hits += 1
            continue

        # ── Strategy 2: relay message scan ────────────────────────────
        cp_key = f"dm_scan_{tid}"
        cp_entry = checkpoint.get(cp_key, {})
        found_uid: Optional[str] = cp_entry.get("found_uid")

        if not cp_entry.get("done") and found_uid is None:
            label = f"dm-scan/{t.get('name', tid)[:28]}"
            async for page in stream_pages(
                session, tid, cp_key, checkpoint, limit_pages, label
            ):
                for msg in page:
                    if not _is_bot(msg):
                        continue
                    rm = _RELAY_UID_RE.search(msg.get("content", ""))
                    if rm:
                        found_uid = rm.group(1)
                        break
                # Save per-page in case we're interrupted
                checkpoint[cp_key] = {
                    **checkpoint.get(cp_key, {}),
                    "found_uid": found_uid,
                }
                save_checkpoint(checkpoint)
                if found_uid:
                    break  # No need to read further messages in this thread

        if found_uid:
            mapping[found_uid] = tid
            resolved_tids.add(tid)
            relay_hits += 1

    print(f"  [dm_threads] name-based: {name_hits}  relay-fallback: {relay_hits}  "
          f"total: {len(mapping)}")
    # Persist resolved mapping to checkpoint
    checkpoint[CP_KEY] = {uid: str(tid) for uid, tid in mapping.items()}
    save_checkpoint(checkpoint)
    return mapping


# ---------------------------------------------------------------------------
# Trauma team parser (forum threads) — cross-thread global best_ts
# ---------------------------------------------------------------------------

async def fetch_and_parse_trauma_team(
    session: aiohttp.ClientSession,
    threads: list[dict],
    checkpoint: dict,
    limit_pages: Optional[int],
) -> dict[str, str]:
    """
    For each forum thread, stream messages and find payment confirmations.
    Thread title format: 'Character Name - userid'.

    Tracks the most recent payment per user ACROSS ALL THREADS using a
    global checkpoint key so that, when a user has multiple threads, only
    the latest payment is returned regardless of iteration order.

    Returns {user_id: summary_string}.
    """
    PAYMENT_RE = re.compile(r"Payment Successful")
    # Global per-user best: {user_id: [best_ts_iso, best_summary]}
    # Loaded from checkpoint so progress survives interruption.
    global_best: dict[str, list] = checkpoint.get("tt_global_best", {})

    for t in threads:
        m = re.search(r"\s*-\s*(\d{15,20})\s*$", t.get("name", ""))
        if not m:
            continue
        user_id = m.group(1)
        tid = int(t["id"])
        cp_key = f"tt_{tid}"
        label = f"trauma/{t.get('name', tid)[:28]}"

        cp_entry = checkpoint.get(cp_key, {})
        # Per-thread best (within this thread only)
        thread_best_ts: Optional[str]      = cp_entry.get("best_ts")
        thread_best_summary: Optional[str] = cp_entry.get("best_summary")

        if not cp_entry.get("done"):
            async for page in stream_pages(
                session, tid, cp_key, checkpoint, limit_pages, label
            ):
                for msg in page:
                    if not _is_bot(msg):
                        continue
                    content = msg.get("content", "")
                    if PAYMENT_RE.search(content):
                        ts = msg["timestamp"]
                        if thread_best_ts is None or ts > thread_best_ts:
                            thread_best_ts     = ts
                            thread_best_summary = content.strip()
                # Save per-thread state after every page
                checkpoint[cp_key] = {
                    **checkpoint.get(cp_key, {}),
                    "best_ts": thread_best_ts,
                    "best_summary": thread_best_summary,
                }
                save_checkpoint(checkpoint)

        # Update the global (cross-thread) best for this user
        if thread_best_ts is not None and thread_best_summary is not None:
            existing = global_best.get(user_id)
            if existing is None or thread_best_ts > existing[0]:
                global_best[user_id] = [thread_best_ts, thread_best_summary]
                checkpoint["tt_global_best"] = global_best
                save_checkpoint(checkpoint)

    return {uid: v[1] for uid, v in global_best.items()}

# ---------------------------------------------------------------------------
# Generic channel fetch-and-parse driver
# ---------------------------------------------------------------------------

async def run_channel_section(
    session: aiohttp.ClientSession,
    channel_id: int,
    cp_key: str,
    parser,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
) -> None:
    """
    Stream channel pages and feed them to a stateful parser, saving the
    parser's state to checkpoint after every page.
    Loads existing state from checkpoint on startup (for resume).
    """
    cp_entry = checkpoint.get(cp_key, {})
    # Restore parser state from checkpoint
    parser_state = cp_entry.get("parser_state")
    parser.__init__(parser_state)  # re-initialize with saved state

    if cp_entry.get("done"):
        print(f"  [{label}] already complete (checkpoint) — skipping fetch")
        return

    async for page in stream_pages(session, channel_id, cp_key, checkpoint, limit_pages, label):
        parser.process_page(page)
        # After every page, flush parser state to checkpoint (no raw messages)
        checkpoint[cp_key] = {
            **checkpoint.get(cp_key, {}),
            "parser_state": parser.to_state(),
        }
        save_checkpoint(checkpoint)

# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

async def write_to_db(
    pool: asyncpg.Pool,
    attendance: list[tuple[str, datetime]],
    open_shop: list[tuple[str, datetime]],
    rent_runs: list[dict],
    last_payment_rent: dict[str, str],
    cyber_runs: list[dict],
    cyber_status: dict[str, tuple[int, datetime]],
    dm_threads: dict[str, int],
    last_payment_trauma: dict[str, str],
) -> dict[str, dict]:
    summary: dict[str, dict] = {
        "attendance_log":        {"found": 0, "inserted": 0, "skipped": 0},
        "business_open_log":     {"found": 0, "inserted": 0, "skipped": 0},
        "rent_runs":             {"found": 0, "inserted": 0, "skipped": 0},
        "last_payment":          {"found": 0, "inserted": 0, "skipped": 0},
        "cyberware_weekly_runs": {"found": 0, "inserted": 0, "skipped": 0},
        "cyberware_status":      {"found": 0, "inserted": 0, "skipped": 0},
        "dm_threads":            {"found": 0, "inserted": 0, "skipped": 0},
    }

    def _tally(tag: str, res: str) -> None:
        n = int(res.split()[-1])
        summary[tag]["found"]    += 1
        summary[tag]["inserted"] += n
        summary[tag]["skipped"]  += 1 - n

    async with pool.acquire() as conn:
        for uid, ts in attendance:
            res = await conn.execute(
                "INSERT INTO attendance_log (user_id, logged_at) VALUES ($1,$2)"
                " ON CONFLICT DO NOTHING",
                str(uid), ts,
            )
            _tally("attendance_log", res)

        for uid, ts in open_shop:
            res = await conn.execute(
                "INSERT INTO business_open_log (user_id, opened_at) VALUES ($1,$2)"
                " ON CONFLICT DO NOTHING",
                str(uid), ts,
            )
            _tally("business_open_log", res)

        for run in rent_runs:
            run_at = _parse_ts(run["run_at"]) if isinstance(run["run_at"], str) else run["run_at"]
            summary["rent_runs"]["found"] += 1
            exists = await conn.fetchval(
                "SELECT id FROM rent_runs WHERE run_at BETWEEN $1 AND $2 LIMIT 1",
                run_at - timedelta(minutes=5), run_at + timedelta(minutes=5),
            )
            if exists:
                summary["rent_runs"]["skipped"] += 1
            else:
                await conn.execute(
                    "INSERT INTO rent_runs (run_at, initiated_by) VALUES ($1,$2)",
                    run_at, run["initiated_by"],
                )
                summary["rent_runs"]["inserted"] += 1

        # Merge last_payment: rent first, trauma_team overwrites where present
        all_lp: dict[str, str] = {**last_payment_rent, **last_payment_trauma}
        for uid, lp_summary in all_lp.items():
            res = await conn.execute(
                "INSERT INTO last_payment (user_id, summary) VALUES ($1,$2)"
                " ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary",
                str(uid), lp_summary,
            )
            _tally("last_payment", res)

        for run in cyber_runs:
            run_at = _parse_ts(run["run_at"]) if isinstance(run["run_at"], str) else run["run_at"]
            summary["cyberware_weekly_runs"]["found"] += 1
            exists = await conn.fetchval(
                "SELECT id FROM cyberware_weekly_runs WHERE run_at BETWEEN $1 AND $2 LIMIT 1",
                run_at - timedelta(minutes=30), run_at + timedelta(minutes=30),
            )
            if exists:
                summary["cyberware_weekly_runs"]["skipped"] += 1
            else:
                paid_ids = [p["user_id"] for p in run["paid"]]
                await conn.execute(
                    "INSERT INTO cyberware_weekly_runs (run_at, paid_ids, unpaid_ids, checkup_ids)"
                    " VALUES ($1,$2,$3,$4)",
                    run_at, paid_ids, run["unpaid"], run["checkup"],
                )
                summary["cyberware_weekly_runs"]["inserted"] += 1

        for uid, (weeks, last_processed) in cyber_status.items():
            res = await conn.execute(
                """INSERT INTO cyberware_status (user_id, weeks, last_processed)
                   VALUES ($1,$2,$3)
                   ON CONFLICT (user_id) DO UPDATE
                     SET weeks = EXCLUDED.weeks,
                         last_processed = EXCLUDED.last_processed
                   WHERE cyberware_status.last_processed IS NULL
                      OR EXCLUDED.last_processed > cyberware_status.last_processed""",
                str(uid), weeks, last_processed,
            )
            _tally("cyberware_status", res)

        for uid, thread_id in dm_threads.items():
            res = await conn.execute(
                "INSERT INTO dm_threads (user_id, thread_id) VALUES ($1,$2)"
                " ON CONFLICT (user_id) DO UPDATE SET thread_id = EXCLUDED.thread_id",
                str(uid), thread_id,
            )
            _tally("dm_threads", res)

    return summary

# ---------------------------------------------------------------------------
# Dry-run preview — every record, no truncation
# ---------------------------------------------------------------------------

def _print_dry_run(
    attendance: list,
    open_shop: list,
    rent_runs: list,
    last_payment_rent: dict,
    cyber_runs: list,
    cyber_status: dict,
    dm_threads: dict,
    last_payment_trauma: dict,
) -> None:
    sep = "=" * 64
    print(f"\n{sep}\nDRY RUN PREVIEW — nothing will be written\n{sep}")

    print(f"\n[attendance_log] {len(attendance)} record(s)")
    for uid, ts in attendance:
        print(f"  user={uid}  logged_at={ts.isoformat()}")

    print(f"\n[business_open_log] {len(open_shop)} record(s)")
    for uid, ts in open_shop:
        print(f"  user={uid}  opened_at={ts.isoformat()}")

    print(f"\n[rent_runs] {len(rent_runs)} run(s)")
    for run in rent_runs:
        print(f"  run_at={run['run_at']}  users={len(run['per_user'])}")

    print(f"\n[last_payment — rent] {len(last_payment_rent)} user(s)")
    for uid, s in last_payment_rent.items():
        print(f"  user={uid}  summary={s[:120]}")

    print(f"\n[cyberware_weekly_runs] {len(cyber_runs)} run(s)")
    for run in cyber_runs:
        print(f"  run_at={run['run_at']}  paid={len(run['paid'])}  "
              f"unpaid={len(run['unpaid'])}  checkup={len(run['checkup'])}")

    print(f"\n[cyberware_status] {len(cyber_status)} user(s)")
    for uid, (weeks, ts) in cyber_status.items():
        print(f"  user={uid}  weeks={weeks}  last_processed={ts.isoformat()}")

    print(f"\n[dm_threads] {len(dm_threads)} mapping(s)")
    for uid, tid in dm_threads.items():
        print(f"  user={uid}  thread={tid}")

    print(f"\n[last_payment — trauma_team] {len(last_payment_trauma)} user(s)")
    for uid, s in last_payment_trauma.items():
        print(f"  user={uid}  summary={s[:120]}")

    print(f"\n{sep}\nDRY RUN — nothing written\n{sep}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore historical data from Discord channels into the database."
    )
    parser.add_argument(
        "--section",
        choices=ALL_SECTIONS,
        help="Run only this section (omit to run all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but do not write to the database",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap pages per channel at N (100 msgs/page); for smoke tests",
    )
    args = parser.parse_args()

    sections: list[str] = [args.section] if args.section else ALL_SECTIONS
    dry_run: bool = args.dry_run

    token = os.environ.get("TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            import config as _cfg
            token = getattr(_cfg, "TOKEN", None)
        except Exception:
            pass
    if not token:
        print("ERROR: Discord TOKEN not found in environment or config.py")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    checkpoint = load_checkpoint()

    print("\nNightCityBot — Discord history restoration")
    print(f"Sections : {', '.join(sections)}")
    print(f"Dry run  : {dry_run}")
    print(f"Limit    : {args.limit} page(s) per channel" if args.limit else "Limit    : none (full history)")
    if checkpoint:
        print(f"Checkpoint: resuming from {CHECKPOINT_FILE}")
    print()

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    # --- Result accumulators -------------------------------------------------
    attendance_parser  = AttendanceParser()
    open_shop_parser   = OpenShopParser()
    rent_parser        = RentParser()
    cyber_parser       = CyberwareParser()
    dm_thread_map:     dict[str, int] = {}
    last_pay_trauma:   dict[str, str] = {}

    async with aiohttp.ClientSession(headers=headers) as session:

        if "attendance" in sections:
            print("--- Section: attendance ---")
            await run_channel_section(
                session, CHANNEL_IDS["attendance"], "ch_attendance",
                attendance_parser, checkpoint, args.limit, "attendance",
            )
            results = attendance_parser.get_results()
            print(f"  Parsed {len(results)} attendance records")

        if "open_shop" in sections:
            print("\n--- Section: open_shop ---")
            await run_channel_section(
                session, CHANNEL_IDS["open_shop"], "ch_open_shop",
                open_shop_parser, checkpoint, args.limit, "open_shop",
            )
            results = open_shop_parser.get_results()
            print(f"  Parsed {len(results)} shop-opening records")

        if "rent" in sections:
            print("\n--- Section: rent ---")
            await run_channel_section(
                session, CHANNEL_IDS["rent"], "ch_rent",
                rent_parser, checkpoint, args.limit, "rent",
            )
            rr, lp = rent_parser.get_results()
            print(f"  Parsed {len(rr)} rent runs, {len(lp)} last_payment entries")

        if "cyberware" in sections:
            print("\n--- Section: cyberware ---")
            await run_channel_section(
                session, CHANNEL_IDS["cyberware"], "ch_cyberware",
                cyber_parser, checkpoint, args.limit, "cyberware",
            )
            cr, cs = cyber_parser.get_results()
            print(f"  Parsed {len(cr)} cyberware runs, {len(cs)} unique users")

        if "dm_threads" in sections:
            print("\n--- Section: dm_threads ---")
            threads = await fetch_threads_checkpointed(
                session, CHANNEL_IDS["dm_threads"], "th_dm_threads",
                checkpoint, args.limit, "dm_threads",
            )
            dm_thread_map = await build_dm_thread_map(
                session, threads, checkpoint, args.limit,
            )

        if "trauma_team" in sections:
            print("\n--- Section: trauma_team ---")
            threads = await fetch_threads_checkpointed(
                session, CHANNEL_IDS["trauma_team"], "th_trauma_team",
                checkpoint, args.limit, "trauma_team",
            )
            last_pay_trauma = await fetch_and_parse_trauma_team(
                session, threads, checkpoint, args.limit,
            )
            print(f"  Found {len(last_pay_trauma)} trauma team payment records")

    # Collect final results from parsers
    attendance_results = attendance_parser.get_results()
    open_shop_results  = open_shop_parser.get_results()
    rent_runs, last_pay_rent = rent_parser.get_results()
    cyber_runs, cyber_status = cyber_parser.get_results()

    # --- Write pass (or dry-run preview) -------------------------------------
    if dry_run:
        print("\n--- Dry-run preview ---")
        _print_dry_run(
            attendance_results, open_shop_results,
            rent_runs, last_pay_rent,
            cyber_runs, cyber_status,
            dm_thread_map, last_pay_trauma,
        )
        return

    print("\n--- Writing to database ---")
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    try:
        db_summary = await write_to_db(
            pool,
            attendance_results, open_shop_results,
            rent_runs, last_pay_rent,
            cyber_runs, cyber_status,
            dm_thread_map, last_pay_trauma,
        )
    finally:
        await pool.close()

    print("\n=== Summary ===")
    for table, counts in db_summary.items():
        if counts["found"] > 0:
            print(f"  {table:<30} found={counts['found']:>5}  "
                  f"inserted={counts['inserted']:>5}  skipped={counts['skipped']:>5}")
    print("\nSafe to re-run — all inserts use ON CONFLICT DO NOTHING or upsert.")

    if not args.section:
        delete_checkpoint()
        print("Checkpoint file deleted.")


if __name__ == "__main__":
    asyncio.run(main())
