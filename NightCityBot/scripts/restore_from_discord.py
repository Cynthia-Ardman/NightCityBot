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

Implementation notes:
    • Pages are fetched using the standard `before=` (newest-first) cursor so
      that snapshot behaviour is stable on active channels.
    • Each page is parsed immediately on arrival.  Only lightweight extracted
      events (timestamps, user IDs, short strings) are kept in memory and
      written to the checkpoint — raw Discord message objects are never stored.
    • Final clustering/matching is deferred to `get_results()` which sorts the
      collected events and applies time-window rules in chronological order.
    • The checkpoint file (.restore_checkpoint.json) stores cursor + parser
      state after every page; interrupted runs resume exactly from the last
      cursor without re-fetching.
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
_PAIR_WINDOW_SECS  = 60   # max seconds between user command and bot ACK

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
    """GET a Discord endpoint, retrying on 429 with the exact retry_after sleep."""
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
    Async generator that streams channel message pages using the standard
    backward (newest-first) `before=` cursor.

    After every page the checkpoint is updated with the new cursor and the
    caller's accumulated parser state.  Raw Discord message objects are
    never written to the checkpoint file.
    """
    cp_entry = checkpoint.get(cp_key, {})
    if cp_entry.get("done"):
        return  # Already fully fetched; caller loads state from checkpoint

    before_cursor: Optional[str] = cp_entry.get("before_cursor")  # None = start from newest
    page_num: int = cp_entry.get("page_num", 0)
    url = f"{DISCORD_API}/channels/{channel_id}/messages"

    while True:
        params: dict = {"limit": 100}
        if before_cursor:
            params["before"] = before_cursor

        page: list = await discord_get(session, url, params)

        if not page:
            checkpoint[cp_key] = {**checkpoint.get(cp_key, {}), "done": True}
            save_checkpoint(checkpoint)
            return

        # Discord returns messages newest-first; page[-1] is the oldest in this batch
        page_num += 1
        before_cursor = page[-1]["id"]   # cursor for the next (older) page

        print(f"  [{label}] page {page_num}: {len(page)} messages  cursor={before_cursor}")

        # Update cursor in checkpoint (caller merges parser_state after yield)
        cp_entry = {**cp_entry, "before_cursor": before_cursor, "page_num": page_num, "done": False}
        checkpoint[cp_key] = cp_entry

        yield page
        # After yield, run_channel_section has merged parser_state into checkpoint[cp_key]

        if len(page) < 100:
            # Merge with current entry so parser_state written by caller is preserved
            checkpoint[cp_key] = {**checkpoint.get(cp_key, {}), "done": True}
            save_checkpoint(checkpoint)
            return

        if limit_pages and page_num >= limit_pages:
            print(f"  [{label}] --limit {limit_pages} reached, stopping")
            # Not marking done — a full run should resume from before_cursor
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
# Stateful per-page parsers — collect-and-sort design
#
# Each parser:
#   • Accumulates lightweight parsed events (timestamps + IDs) as pages arrive.
#     Raw message objects are never stored.
#   • Defers clustering/matching to get_results(), where events are sorted
#     into chronological order before window rules are applied.
#   • State is JSON-serialisable for checkpoint persistence.
#
# Interface:
#   __init__(state)     — restore from checkpoint state dict (or None)
#   process_page(page)  — extract events from one (newest-first) page
#   to_state()          — return JSON-serialisable dict for checkpoint
#   get_results()       — return final parsed records (called once, at end)
# ---------------------------------------------------------------------------

def _strict_adjacent_pairs(
    cmds: list[list],
    acks: list[str],
) -> list[tuple[str, datetime]]:
    """
    Match user commands to bot ACKs using strict adjacent chronological pairing.

    Algorithm (O(n log n)):
      1. Sort both lists by timestamp.
      2. Walk through commands in order; for each command, advance an ACK pointer
         to the first ACK whose timestamp is >= the command's timestamp.
      3. If that ACK is within _PAIR_WINDOW_SECS, consume it as the pair.
         Each ACK may only be consumed once (strictly one-to-one).

    This mirrors how the bot behaves in real-time: it responds to each command
    in order, so the immediately-following ACK is the correct match even when
    multiple users issue commands in quick succession.
    """
    sorted_cmds = sorted(cmds, key=lambda x: x[1])
    sorted_acks = sorted(acks)
    results: list[tuple[str, datetime]] = []
    ack_idx = 0
    for uid, cmd_ts_iso in sorted_cmds:
        cmd_dt = _parse_ts(cmd_ts_iso)
        # Advance pointer past ACKs that predate this command
        while ack_idx < len(sorted_acks) and _parse_ts(sorted_acks[ack_idx]) < cmd_dt:
            ack_idx += 1
        if ack_idx < len(sorted_acks):
            ack_dt = _parse_ts(sorted_acks[ack_idx])
            if (ack_dt - cmd_dt).total_seconds() <= _PAIR_WINDOW_SECS:
                results.append((uid, ack_dt))
                ack_idx += 1  # consume this ACK; each ACK is used at most once
    return results


class AttendanceParser:
    """
    Collects "Attendance logged" bot-ACK timestamps and !attend user commands
    separately, then matches them with strict adjacent chronological pairing
    in get_results().
    """

    def __init__(self, state: Optional[dict] = None) -> None:
        s = state or {}
        # [ts_iso, ...]  — timestamps of bot "Attendance logged" messages
        self._bot_acks: list[str] = s.get("bot_acks", [])
        # [[uid, ts_iso], ...]  — !attend commands
        self._cmds: list[list] = s.get("cmds", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if _is_bot(msg):
                if "Attendance logged" in msg.get("content", ""):
                    self._bot_acks.append(msg["timestamp"])
            else:
                if msg.get("content", "").strip().lower().startswith("!attend"):
                    self._cmds.append([str(msg["author"]["id"]), msg["timestamp"]])

    def to_state(self) -> dict:
        return {"bot_acks": self._bot_acks, "cmds": self._cmds}

    def get_results(self) -> list[tuple[str, datetime]]:
        return _strict_adjacent_pairs(self._cmds, self._bot_acks)


class OpenShopParser:
    """
    Same collect-and-match pattern as AttendanceParser, for !open_shop commands.
    Uses strict adjacent chronological pairing in get_results().
    """

    def __init__(self, state: Optional[dict] = None) -> None:
        s = state or {}
        self._bot_acks: list[str] = s.get("bot_acks", [])
        self._cmds: list[list] = s.get("cmds", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if _is_bot(msg):
                if "Business opening logged" in msg.get("content", ""):
                    self._bot_acks.append(msg["timestamp"])
            else:
                content = msg.get("content", "").strip().lower()
                if content.startswith(("!open_shop", "!openshop", "!os")):
                    self._cmds.append([str(msg["author"]["id"]), msg["timestamp"]])

    def to_state(self) -> dict:
        return {"bot_acks": self._bot_acks, "cmds": self._cmds}

    def get_results(self) -> list[tuple[str, datetime]]:
        return _strict_adjacent_pairs(self._cmds, self._bot_acks)


class RentParser:
    """
    Collects rent payment events per page (newest-first order fine since we
    sort in get_results before clustering).
    """

    PAY_RE = re.compile(r"(Housing|Business) Rent paid")

    def __init__(self, state: Optional[dict] = None) -> None:
        s = state or {}
        # [[ts_iso, uid, line], ...]
        self._events: list[list] = s.get("events", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if not _is_bot(msg):
                continue
            content = msg.get("content", "")
            if not self.PAY_RE.search(content):
                continue
            ts = msg["timestamp"]
            for uid in _mentions(content):
                self._events.append([ts, uid, content.strip()])

    def to_state(self) -> dict:
        return {"events": self._events}

    def get_results(self) -> tuple[list[dict], dict[str, str], dict[str, str]]:
        """
        Returns:
          (runs, last_payment_summary, last_payment_ts)

        ``last_payment_summary[uid]`` is the most recent rent summary line.
        ``last_payment_ts[uid]`` is its ISO timestamp (for recency comparison
        when merging with other payment sources).
        """
        if not self._events:
            return [], {}, {}
        # Sort chronologically before clustering
        sorted_evs = sorted(self._events, key=lambda x: x[0])
        runs: list[dict] = []
        last_payment: dict[str, str] = {}
        last_payment_ts: dict[str, str] = {}
        cluster: list[list] = []

        def flush() -> None:
            if not cluster:
                return
            per_user: dict[str, str] = {}
            run_at_iso = cluster[0][0]
            for ts_iso, uid, line in cluster:
                per_user[uid] = line
                # Track most-recent payment per user across all clusters
                if uid not in last_payment_ts or ts_iso > last_payment_ts[uid]:
                    last_payment[uid]    = line
                    last_payment_ts[uid] = ts_iso
            runs.append({"run_at": run_at_iso, "initiated_by": "restored",
                         "per_user": per_user})
            cluster.clear()

        for ev in sorted_evs:
            if cluster:
                prev_ts = _parse_ts(cluster[-1][0])
                if (_parse_ts(ev[0]) - prev_ts) > _CLUSTER_GAP_RENT:
                    flush()
            cluster.append(ev)
        flush()
        return runs, last_payment, last_payment_ts


class CyberwareParser:
    """
    Collects cyberware events per page; sorts and clusters in get_results().
    """

    def __init__(self, state: Optional[dict] = None) -> None:
        s = state or {}
        # [[ts_iso, type, uid, weeks_or_null], ...]
        self._events: list[list] = s.get("events", [])

    def process_page(self, page: list[dict]) -> None:
        for msg in page:
            if not _is_bot(msg):
                continue
            content = msg.get("content", "")
            ts = msg["timestamp"]
            m = _CY_PAID_OLD.search(content)
            if m:
                self._events.append([ts, "paid", m.group(1), int(m.group(2))])
                continue
            m = _CY_PAID_NEW.search(content)
            if m:
                self._events.append([ts, "paid_noweek", m.group(1), None])
                continue
            m = _CY_UNPA_OLD.search(content)
            if m:
                self._events.append([ts, "unpaid", m.group(1), None])
                continue
            m = _CY_UNPA_NEW.search(content)
            if m:
                self._events.append([ts, "unpaid", m.group(1), None])
                continue
            m = _CY_CHECKUP.search(content)
            if m:
                self._events.append([ts, "checkup", m.group(1), None])

    def to_state(self) -> dict:
        return {"events": self._events}

    def get_results(self) -> tuple[list[dict], dict[str, tuple[int, datetime]]]:
        if not self._events:
            return [], {}
        sorted_evs = sorted(self._events, key=lambda x: x[0])
        runs: list[dict] = []
        cyber_status: dict[str, list] = {}  # {uid: [weeks, ts_iso]}
        run_buf: list[list] = []

        def flush_run() -> None:
            if not run_buf:
                return
            run_at = run_buf[0][0]
            paid, unpaid, checkup = [], [], []
            for ts_iso, kind, uid, weeks in run_buf:
                if kind == "paid":
                    paid.append({"user_id": uid, "weeks": weeks})
                    existing = cyber_status.get(uid)
                    if not existing or ts_iso > existing[1]:
                        cyber_status[uid] = [weeks, ts_iso]
                elif kind == "paid_noweek":
                    paid.append({"user_id": uid, "weeks": None})
                    existing = cyber_status.get(uid)
                    if not existing or ts_iso > existing[1]:
                        prev_weeks = existing[0] if existing else 0
                        cyber_status[uid] = [prev_weeks, ts_iso]
                elif kind == "unpaid":
                    unpaid.append(uid)
                elif kind == "checkup":
                    checkup.append(uid)
            runs.append({"run_at": run_at, "paid": paid, "unpaid": unpaid, "checkup": checkup})
            run_buf.clear()

        for ev in sorted_evs:
            if run_buf:
                prev_ts = _parse_ts(run_buf[-1][0])
                if (_parse_ts(ev[0]) - prev_ts) > _RUN_GAP_CYBER:
                    flush_run()
            run_buf.append(ev)
        flush_run()

        status: dict[str, tuple[int, datetime]] = {
            uid: (v[0], _parse_ts(v[1])) for uid, v in cyber_status.items()
        }
        return runs, status

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
    channel, saving the checkpoint after every pagination batch.

    `public_done`, `private_done`, and `all_done` are only set when the API
    signals exhaustion (``has_more=false`` or empty batch).  They are NOT set
    when the loop stops due to ``--limit``, so a later full run will resume
    from the saved cursor.
    """
    cp = checkpoint.get(cp_key, {})
    if cp.get("all_done"):
        print(f"  [{label}] thread enumeration already complete (checkpoint)")
        return cp.get("threads", [])

    threads: list[dict] = list(cp.get("threads", []))

    # ── Active threads (single request, no pagination) ─────────────────────
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
        pub_page: int = cp.get("public_page", 0)
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
            cp = {**cp, "threads": threads, "public_cursor": before_ts, "public_page": pub_page}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)  # checkpoint after every batch
            if not data.get("has_more") or not batch:
                pub_exhausted = True
                break
            if limit_pages and pub_page >= limit_pages:
                break  # stopped by --limit; NOT exhausted

        if pub_exhausted:
            cp = {**cp, "public_done": True}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)

    # ── Archived private threads (paginated) ───────────────────────────────
    if not cp.get("private_done"):
        priv_url = f"{DISCORD_API}/channels/{channel_id}/threads/archived/private"
        before_ts = cp.get("private_cursor")
        priv_page: int = cp.get("private_page", 0)
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
                cp = {**cp, "threads": threads, "private_cursor": before_ts, "private_page": priv_page}
                checkpoint[cp_key] = cp
                save_checkpoint(checkpoint)
                if not data.get("has_more") or not batch:
                    priv_exhausted = True
                    break
                if limit_pages and priv_page >= limit_pages:
                    break  # stopped by --limit; NOT exhausted
        except RuntimeError:
            priv_exhausted = True  # Bot lacks MANAGE_THREADS; skip silently

        if priv_exhausted:
            all_done = cp.get("public_done", False) and priv_exhausted
            cp = {**cp, "private_done": True, "all_done": all_done, "threads": threads}
            checkpoint[cp_key] = cp
            save_checkpoint(checkpoint)

    print(f"  [{label}] total threads: {len(threads)}")
    return threads

# ---------------------------------------------------------------------------
# DM thread mapping — name-based (primary) + relay-message fallback
# ---------------------------------------------------------------------------

# Relay log patterns written by dm_handling.py into each thread:
#   📥 **Received from DisplayName (USER_ID)**:   (incoming DM)
#   📤 **Sent to DisplayName (USER_ID) by …:**    (outgoing DM via !dm)
#   📤 **Sent to DisplayName (USER_ID) by …:**    (outgoing via thread reply)
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
    Build user_id → thread_id mapping with two strategies:

    1. **Name-based (primary)**: thread name ends with the numeric user ID
       (``username-123456789012345678``).  When multiple threads match the
       same user_id, the one with the higher ``last_message_id`` (snowflake =
       chronologically most recent) wins.

    2. **Relay-message fallback**: for threads whose names cannot be parsed,
       stream thread messages and match the first bot relay log line.  Again,
       the most-recently-active thread wins for a given user.

    All resolved mappings are persisted in the checkpoint (``dm_thread_map_cache``)
    so resumed runs skip already-resolved threads.  Thread IDs are stored and
    reloaded as integers.
    """
    CP_KEY = "dm_thread_map_cache"
    # Load previously resolved mappings; values stored as ints
    raw_cache: dict = checkpoint.get(CP_KEY, {})
    # mapping: {uid_str: (thread_id_int, last_msg_id_str)}
    mapping: dict[str, tuple[int, str]] = {
        uid: (int(v[0]), str(v[1])) for uid, v in raw_cache.items()
    }

    resolved_tids: set[int] = {t for t, _ in mapping.values()}

    name_hits = 0
    relay_hits = 0

    for t in threads:
        tid = int(t["id"])
        last_msg = t.get("last_message_id") or "0"

        # ── Strategy 1: name suffix ────────────────────────────────────────
        m = re.search(r"(\d{15,20})$", t.get("name", ""))
        if m:
            uid = m.group(1)
            existing = mapping.get(uid)
            # Keep the thread with the higher last_message_id (more recent)
            if existing is None or last_msg > existing[1]:
                mapping[uid] = (tid, last_msg)
                name_hits += 1
            resolved_tids.add(tid)
            continue

        # ── Strategy 2: relay message scan ────────────────────────────────
        if tid in resolved_tids:
            continue  # Already resolved via a previous fallback pass

        cp_key = f"dm_scan_{tid}"
        cp_entry = checkpoint.get(cp_key, {})
        found_uid: Optional[str] = cp_entry.get("found_uid")
        found_ts:  Optional[str] = cp_entry.get("found_ts")

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
                        # Take the newest (highest-timestamp) relay match
                        candidate_ts = msg["timestamp"]
                        if found_ts is None or candidate_ts > found_ts:
                            found_uid = rm.group(1)
                            found_ts  = candidate_ts
                # Persist per-page so interruption doesn't lose progress
                checkpoint[cp_key] = {
                    **checkpoint.get(cp_key, {}),
                    "found_uid": found_uid,
                    "found_ts":  found_ts,
                }
                save_checkpoint(checkpoint)

        if found_uid:
            existing = mapping.get(found_uid)
            # Prefer most-recently-active thread (highest last_message_id)
            if existing is None or last_msg > existing[1]:
                mapping[found_uid] = (tid, last_msg)
                relay_hits += 1
            resolved_tids.add(tid)

    print(f"  [dm_threads] name-based: {name_hits}  relay-fallback: {relay_hits}  "
          f"total: {len(mapping)}")

    # Persist resolved mapping to checkpoint (store as [int, str] pairs for JSON)
    checkpoint[CP_KEY] = {uid: [tid, lm] for uid, (tid, lm) in mapping.items()}
    save_checkpoint(checkpoint)

    # Return {uid: thread_id (int)} — the form needed by the DB writer
    return {uid: tid for uid, (tid, _lm) in mapping.items()}

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
    Thread title format: ``Character Name - userid``.

    Maintains a **global** per-user best (``tt_global_best``) across all threads
    so that users with multiple threads always get their most recent payment
    regardless of thread iteration order.

    Returns ``{user_id: summary_string}``.
    """
    PAYMENT_RE = re.compile(r"Payment Successful")
    # {user_id: [best_ts_iso, best_summary]}  — persisted in checkpoint
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
        thread_best_ts:      Optional[str] = cp_entry.get("best_ts")
        thread_best_summary: Optional[str] = cp_entry.get("best_summary")

        if not cp_entry.get("done"):
            async for page in stream_pages(
                session, tid, cp_key, checkpoint, limit_pages, label
            ):
                for msg in page:
                    if not _is_bot(msg):
                        continue
                    if PAYMENT_RE.search(msg.get("content", "")):
                        ts = msg["timestamp"]
                        if thread_best_ts is None or ts > thread_best_ts:
                            thread_best_ts     = ts
                            thread_best_summary = msg["content"].strip()
                # Save per-thread best after every page
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
    parser: Any,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
) -> None:
    """
    Stream channel pages (newest-first, backward cursor) and feed them to a
    stateful parser, saving the parser's state to checkpoint after every page.
    Restores parser state from checkpoint on entry to support seamless resume.
    """
    cp_entry = checkpoint.get(cp_key, {})
    parser_state = cp_entry.get("parser_state")
    parser.__init__(parser_state)  # re-initialise with saved state (or fresh)

    if cp_entry.get("done"):
        print(f"  [{label}] already complete (checkpoint) — skipping fetch")
        return

    async for page in stream_pages(session, channel_id, cp_key, checkpoint, limit_pages, label):
        parser.process_page(page)
        # Merge parser state into checkpoint after every page (no raw messages)
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
    last_payment: dict[str, str],
    cyber_runs: list[dict],
    cyber_status: dict[str, tuple[int, datetime]],
    dm_threads: dict[str, int],
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

        for uid, lp_summary in last_payment.items():
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
                str(uid), int(thread_id),  # always ensure int
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
    last_payment: dict,
    cyber_runs: list,
    cyber_status: dict,
    dm_threads: dict,
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
        print(f"  run_at={run['run_at']}  users={len(run.get('per_user',{}))}")

    print(f"\n[last_payment (merged — most-recent-wins)] {len(last_payment)} user(s)")
    for uid, s in last_payment.items():
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

    # --- Result accumulators ------------------------------------------------
    attendance_parser = AttendanceParser()
    open_shop_parser  = OpenShopParser()
    rent_parser       = RentParser()
    cyber_parser      = CyberwareParser()
    dm_thread_map:    dict[str, int] = {}
    last_pay_trauma:  dict[str, str] = {}

    async with aiohttp.ClientSession(headers=headers) as session:

        if "attendance" in sections:
            print("--- Section: attendance ---")
            await run_channel_section(
                session, CHANNEL_IDS["attendance"], "ch_attendance",
                attendance_parser, checkpoint, args.limit, "attendance",
            )
            print(f"  Collected {len(attendance_parser._bot_acks)} bot-acks "
                  f"and {len(attendance_parser._cmds)} commands")

        if "open_shop" in sections:
            print("\n--- Section: open_shop ---")
            await run_channel_section(
                session, CHANNEL_IDS["open_shop"], "ch_open_shop",
                open_shop_parser, checkpoint, args.limit, "open_shop",
            )
            print(f"  Collected {len(open_shop_parser._bot_acks)} bot-acks "
                  f"and {len(open_shop_parser._cmds)} commands")

        if "rent" in sections:
            print("\n--- Section: rent ---")
            await run_channel_section(
                session, CHANNEL_IDS["rent"], "ch_rent",
                rent_parser, checkpoint, args.limit, "rent",
            )
            print(f"  Collected {len(rent_parser._events)} rent events")

        if "cyberware" in sections:
            print("\n--- Section: cyberware ---")
            await run_channel_section(
                session, CHANNEL_IDS["cyberware"], "ch_cyberware",
                cyber_parser, checkpoint, args.limit, "cyberware",
            )
            print(f"  Collected {len(cyber_parser._events)} cyberware events")

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

    # Derive final results (clustering/matching happens here)
    attendance_results = attendance_parser.get_results()
    open_shop_results  = open_shop_parser.get_results()
    rent_runs, last_pay_rent, last_pay_rent_ts = rent_parser.get_results()
    cyber_runs, cyber_status = cyber_parser.get_results()

    # Merge last_payment by recency: for each user take whichever source has
    # the later timestamp (ISO strings compare lexicographically = chronologically)
    trauma_global_best: dict[str, list] = checkpoint.get("tt_global_best", {})
    merged_last_payment: dict[str, str] = {}
    all_uids = set(last_pay_rent.keys()) | set(last_pay_trauma.keys())
    for uid in all_uids:
        rent_ts     = last_pay_rent_ts.get(uid, "")
        trauma_ts   = trauma_global_best.get(uid, ["", ""])[0]
        if uid in last_pay_rent and uid in last_pay_trauma:
            if trauma_ts > rent_ts:
                merged_last_payment[uid] = last_pay_trauma[uid]
            else:
                merged_last_payment[uid] = last_pay_rent[uid]
        elif uid in last_pay_rent:
            merged_last_payment[uid] = last_pay_rent[uid]
        else:
            merged_last_payment[uid] = last_pay_trauma[uid]

    print(f"\nParsed: {len(attendance_results)} attendance, "
          f"{len(open_shop_results)} shop-opens, "
          f"{len(rent_runs)} rent-runs, "
          f"{len(cyber_runs)} cyber-runs, "
          f"{len(dm_thread_map)} DM-threads, "
          f"{len(last_pay_trauma)} TT-payments, "
          f"{len(merged_last_payment)} last-payment entries")

    # --- Write pass (or dry-run preview) ------------------------------------
    if dry_run:
        print("\n--- Dry-run preview ---")
        _print_dry_run(
            attendance_results, open_shop_results,
            rent_runs, merged_last_payment,
            cyber_runs, cyber_status,
            dm_thread_map,
        )
        return

    print("\n--- Writing to database ---")
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    try:
        db_summary = await write_to_db(
            pool,
            attendance_results, open_shop_results,
            rent_runs, merged_last_payment,
            cyber_runs, cyber_status,
            dm_thread_map,
        )
    finally:
        await pool.close()

    print("\n=== Summary ===")
    for table, counts in db_summary.items():
        if counts["found"] > 0:
            print(f"  {table:<30} found={counts['found']:>5}  "
                  f"inserted={counts['inserted']:>5}  skipped={counts['skipped']:>5}")
    print("\nSafe to re-run — all inserts use ON CONFLICT DO NOTHING or upsert.")

    # Only delete checkpoint when a full (all-section, no --limit) run is
    # confirmed exhausted.  A --limit run or --section run may still need to
    # resume, so we leave the checkpoint in place.
    full_run = not args.section and not args.limit
    if full_run:
        ch_done = all(
            checkpoint.get(f"ch_{s}", {}).get("done", False)
            for s in ["attendance", "open_shop", "rent", "cyberware"]
        )
        th_done = (
            checkpoint.get("th_dm_threads", {}).get("all_done", False)
            and checkpoint.get("th_trauma_team", {}).get("all_done", False)
        )
        if ch_done and th_done:
            delete_checkpoint()
            print("Checkpoint file deleted (all sections fully exhausted).")


if __name__ == "__main__":
    asyncio.run(main())
