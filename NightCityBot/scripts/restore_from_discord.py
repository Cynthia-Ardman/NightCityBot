"""
Discord history restoration script.

Fetches up to a full year of message history from Discord channels and
backfills the database with attendance, shop openings, rent runs, cyberware
weekly runs, DM thread mappings, and trauma team payments.

Usage:
    python -m NightCityBot.scripts.restore_from_discord [options]

    --section SECTION   Only run one section: attendance, open_shop, rent,
                        cyberware, dm_threads, trauma_team
    --dry-run           Fetch and parse but write nothing to the database
    --limit N           Cap pages fetched per channel (100 msgs/page); for
                        quick smoke tests use --limit 2

The script saves a checkpoint file (.restore_checkpoint.json) after every
page so it can resume exactly where it left off if interrupted. Delete the
checkpoint file to start over from the beginning.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import asyncpg

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"

CHANNEL_IDS = {
    "attendance":   1384001117125345280,
    "open_shop":    1379941898772414464,
    "rent":         1379942591721902152,
    "cyberware":    1389028820463521802,
    "dm_threads":   1379222007513874523,
    "trauma_team":  1351070651313557545,
}

CHECKPOINT_FILE = Path(".restore_checkpoint.json")

ALL_SECTIONS = list(CHANNEL_IDS.keys())

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

async def discord_get(session: aiohttp.ClientSession, url: str, params: dict = None) -> Any:
    """GET a Discord endpoint, retrying forever on 429."""
    while True:
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                data = await resp.json()
                wait = float(data.get("retry_after", 1.0))
                print(f"    [rate-limited] sleeping {wait:.2f}s …")
                await asyncio.sleep(wait + 0.1)
                continue
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"Discord API {resp.status}: {text[:200]}")
            return await resp.json()


async def fetch_channel_messages(
    session: aiohttp.ClientSession,
    channel_id: int,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
) -> list[dict]:
    """
    Fetch all messages from a channel, oldest-first.

    Uses the checkpoint to resume after interruption. Returns the full
    chronological list of messages.
    """
    key = f"ch_{channel_id}"
    cp_entry = checkpoint.get(key, {})
    if cp_entry.get("done"):
        print(f"  [{label}] already complete (checkpoint) — skipping fetch")
        return cp_entry.get("messages", [])

    before_cursor = cp_entry.get("cursor")
    pages: list[list[dict]] = cp_entry.get("pages", [])
    page_num = cp_entry.get("page_num", 0)

    url = f"{DISCORD_API}/channels/{channel_id}/messages"

    while True:
        params = {"limit": 100}
        if before_cursor:
            params["before"] = before_cursor

        page = await discord_get(session, url, params)

        if not page:
            break

        page_num += 1
        pages.append(page)

        oldest_id = page[-1]["id"]
        before_cursor = oldest_id

        print(f"  [{label}] page {page_num}: {len(page)} messages  cursor={before_cursor}")

        checkpoint[key] = {
            "cursor": before_cursor,
            "page_num": page_num,
            "pages": pages,
            "done": False,
        }
        save_checkpoint(checkpoint)

        if len(page) < 100:
            break
        if limit_pages and page_num >= limit_pages:
            print(f"  [{label}] --limit {limit_pages} reached, stopping early")
            break

    # Mark complete, flatten chronological
    messages = []
    for p in reversed(pages):
        messages.extend(reversed(p))

    checkpoint[key] = {"done": True, "messages": messages}
    save_checkpoint(checkpoint)

    print(f"  [{label}] total messages fetched: {len(messages)}")
    return messages


async def fetch_threads_for_channel(
    session: aiohttp.ClientSession,
    channel_id: int,
    checkpoint: dict,
    limit_pages: Optional[int],
    label: str,
) -> list[dict]:
    """
    Enumerate all threads (active + archived) for a channel or forum.
    Returns list of thread objects.
    """
    key = f"threads_{channel_id}"
    cp_entry = checkpoint.get(key, {})
    if cp_entry.get("done"):
        print(f"  [{label}] threads already fetched (checkpoint)")
        return cp_entry.get("threads", [])

    threads: list[dict] = []

    # Active threads
    active_url = f"{DISCORD_API}/channels/{channel_id}/threads/active"
    active_data = await discord_get(session, active_url)
    active = active_data.get("threads", [])
    threads.extend(active)
    print(f"  [{label}] active threads: {len(active)}")

    # Archived public threads (paginated)
    arch_url = f"{DISCORD_API}/channels/{channel_id}/threads/archived/public"
    before_ts = None
    arch_page = 0
    while True:
        params = {"limit": 100}
        if before_ts:
            params["before"] = before_ts
        data = await discord_get(session, arch_url, params)
        batch = data.get("threads", [])
        threads.extend(batch)
        arch_page += 1
        print(f"  [{label}] archived-public page {arch_page}: {len(batch)} threads")
        if not data.get("has_more") or not batch:
            break
        if limit_pages and arch_page >= limit_pages:
            break
        before_ts = batch[-1].get("thread_metadata", {}).get("archive_timestamp")

    # Archived private threads
    arch_priv_url = f"{DISCORD_API}/channels/{channel_id}/threads/archived/private"
    try:
        priv_data = await discord_get(session, arch_priv_url, {"limit": 100})
        priv = priv_data.get("threads", [])
        threads.extend(priv)
        print(f"  [{label}] archived-private threads: {len(priv)}")
    except RuntimeError:
        # Bot may not have MANAGE_THREADS; skip silently
        pass

    print(f"  [{label}] total threads: {len(threads)}")
    checkpoint[key] = {"done": True, "threads": threads}
    save_checkpoint(checkpoint)
    return threads

# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

MENTION_RE = re.compile(r"<@!?(\d+)>")

def parse_ts(iso: str) -> datetime:
    """Parse Discord ISO timestamp to UTC datetime."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)

def is_bot(msg: dict) -> bool:
    return msg.get("author", {}).get("bot", False)

def extract_mentions(content: str) -> list[str]:
    return MENTION_RE.findall(content)

# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def parse_attendance(messages: list[dict]) -> list[tuple[str, datetime]]:
    """
    Find successful !attend pairs: [user command, bot '✅ Attendance logged!'].
    Returns list of (user_id, logged_at).
    """
    records: list[tuple[str, datetime]] = []
    pending_user: Optional[str] = None

    for msg in messages:
        if is_bot(msg):
            if pending_user and "Attendance logged" in msg.get("content", ""):
                records.append((pending_user, parse_ts(msg["timestamp"])))
            pending_user = None
        else:
            content = msg.get("content", "").strip()
            if content.lower().startswith("!attend"):
                pending_user = str(msg["author"]["id"])
            else:
                pending_user = None

    return records


def parse_open_shop(messages: list[dict]) -> list[tuple[str, datetime]]:
    """
    Find successful !open_shop pairs: [user command, bot '✅ Business opening logged!'].
    Returns list of (user_id, opened_at).
    """
    records: list[tuple[str, datetime]] = []
    pending_user: Optional[str] = None

    for msg in messages:
        if is_bot(msg):
            if pending_user and "Business opening logged" in msg.get("content", ""):
                records.append((pending_user, parse_ts(msg["timestamp"])))
            pending_user = None
        else:
            content = msg.get("content", "").strip()
            if content.lower().startswith("!open_shop") or content.lower().startswith("!openshop"):
                pending_user = str(msg["author"]["id"])
            else:
                pending_user = None

    return records


def parse_rent(messages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """
    Find rent run clusters from bot messages.
    Returns:
        rent_runs: list of {run_at, initiated_by, users}
        last_payment: {user_id: summary_str}  (most recent payment per user)
    """
    CLUSTER_GAP = timedelta(minutes=5)
    PAY_RE = re.compile(r"(Housing|Business) Rent paid")

    rent_events: list[tuple[datetime, str, str]] = []  # (ts, user_id, raw_line)
    for msg in messages:
        if not is_bot(msg):
            continue
        content = msg.get("content", "")
        if not PAY_RE.search(content):
            continue
        ts = parse_ts(msg["timestamp"])
        for uid in extract_mentions(content):
            rent_events.append((ts, uid, content.strip()))

    if not rent_events:
        return [], {}

    # Cluster by 5-minute gaps
    rent_runs: list[dict] = []
    current_run: list[tuple[datetime, str, str]] = []
    for event in rent_events:
        if current_run and (event[0] - current_run[-1][0]) > CLUSTER_GAP:
            rent_runs.append(_finalize_rent_run(current_run))
            current_run = []
        current_run.append(event)
    if current_run:
        rent_runs.append(_finalize_rent_run(current_run))

    # Build last_payment: most recent cluster per user
    last_payment: dict[str, str] = {}
    for run in rent_runs:
        for uid, summary in run["per_user"].items():
            last_payment[uid] = summary  # later runs overwrite earlier

    return rent_runs, last_payment


def _finalize_rent_run(events: list[tuple[datetime, str, str]]) -> dict:
    run_at = events[0][0]
    per_user: dict[str, str] = {}
    for ts, uid, line in events:
        per_user[uid] = line
    return {"run_at": run_at, "initiated_by": "restored", "per_user": per_user}


# Cyberware message patterns — old format and current format
_CYBER_OLD_RE = re.compile(
    r"Deducted \$[\d,]+ for cyberware meds from <@!?(\d+)> \(week (\d+)\)"
)
_CYBER_NEW_RE = re.compile(
    r"Deducted \$[\d,]+ from <@!?(\d+)> for cyberware meds"
)
_CYBER_UNPAID_RE = re.compile(
    r"<@!?(\d+)> cannot pay \$[\d,]+ for immunosuppressants"
)
_CYBER_CHECKUP_RE = re.compile(
    r"checkup on <@!?(\d+)>"
)


def parse_cyberware(
    messages: list[dict],
) -> tuple[list[dict], dict[str, tuple[int, datetime]]]:
    """
    Parse weekly cyberware runs from the ripperdoc-checkups channel.
    Returns:
        weekly_runs: list of {run_at, paid, unpaid, checkup}
            paid: list of {user_id, weeks}
            unpaid: list of user_id
            checkup: list of user_id
        cyberware_status: {user_id: (weeks, last_processed_dt)}
            — only the most recent entry per user across all runs
    """
    RUN_GAP = timedelta(minutes=30)

    # Collect raw events from bot messages
    events: list[dict] = []
    for msg in messages:
        if not is_bot(msg):
            continue
        content = msg.get("content", "")
        ts = parse_ts(msg["timestamp"])

        m = _CYBER_OLD_RE.search(content)
        if m:
            events.append({"ts": ts, "type": "paid", "user_id": m.group(1), "weeks": int(m.group(2))})
            continue

        m = _CYBER_NEW_RE.search(content)
        if m:
            events.append({"ts": ts, "type": "paid_noweek", "user_id": m.group(1), "weeks": None})
            continue

        m = _CYBER_UNPAID_RE.search(content)
        if m:
            events.append({"ts": ts, "type": "unpaid", "user_id": m.group(1)})
            continue

        m = _CYBER_CHECKUP_RE.search(content)
        if m:
            events.append({"ts": ts, "type": "checkup", "user_id": m.group(1)})
            continue

    if not events:
        return [], {}

    # Cluster into runs by 30-minute gaps
    runs: list[list[dict]] = []
    current: list[dict] = []
    for ev in events:
        if current and (ev["ts"] - current[-1]["ts"]) > RUN_GAP:
            runs.append(current)
            current = []
        current.append(ev)
    if current:
        runs.append(current)

    weekly_runs = []
    # Track highest week count seen per user (from most recent run)
    cyber_status: dict[str, tuple[int, datetime]] = {}

    for run_events in runs:
        run_at = run_events[0]["ts"]
        paid: list[dict] = []
        unpaid: list[str] = []
        checkup: list[str] = []

        for ev in run_events:
            uid = ev["user_id"]
            if ev["type"] == "paid":
                paid.append({"user_id": uid, "weeks": ev["weeks"]})
                # Track per-user: take the highest week count from most recent run
                existing = cyber_status.get(uid)
                if existing is None or run_at > existing[1]:
                    cyber_status[uid] = (ev["weeks"], run_at)
            elif ev["type"] == "paid_noweek":
                paid.append({"user_id": uid, "weeks": None})
                # Can't determine week count from new format — only update ts
                existing = cyber_status.get(uid)
                if existing is None or run_at > existing[1]:
                    cyber_status[uid] = (existing[0] if existing else 0, run_at)
            elif ev["type"] == "unpaid":
                unpaid.append(uid)
            elif ev["type"] == "checkup":
                checkup.append(uid)

        weekly_runs.append({
            "run_at": run_at,
            "paid": paid,
            "unpaid": unpaid,
            "checkup": checkup,
        })

    return weekly_runs, cyber_status


def parse_dm_threads(threads: list[dict]) -> dict[str, int]:
    """
    Extract user_id → thread_id from thread name suffix.
    Thread names follow the pattern: 'anything-{user_id}'
    """
    mapping: dict[str, int] = {}
    for t in threads:
        name = t.get("name", "")
        m = re.search(r"(\d{15,20})$", name)
        if m:
            user_id = m.group(1)
            thread_id = int(t["id"])
            mapping[user_id] = thread_id
    return mapping


def parse_trauma_team(
    threads: list[dict],
    thread_messages: dict[int, list[dict]],
) -> dict[str, str]:
    """
    For each forum thread, find the most recent "✅ Payment Successful" bot message.
    Thread title format: "Character Name - userid"
    Returns: {user_id: summary_string}
    """
    PAYMENT_RE = re.compile(r"Payment Successful")
    result: dict[str, str] = {}

    for t in threads:
        name = t.get("name", "")
        m = re.search(r"\s*-\s*(\d{15,20})\s*$", name)
        if not m:
            continue
        user_id = m.group(1)
        thread_id = int(t["id"])
        msgs = thread_messages.get(thread_id, [])

        # Find the most recent bot payment message
        latest_ts: Optional[datetime] = None
        latest_summary: Optional[str] = None
        for msg in msgs:
            if not is_bot(msg):
                continue
            content = msg.get("content", "")
            if PAYMENT_RE.search(content):
                ts = parse_ts(msg["timestamp"])
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
                    latest_summary = content.strip()

        if latest_summary:
            result[user_id] = latest_summary

    return result

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
    dry_run: bool,
) -> dict[str, dict]:
    summary = {
        "attendance_log": {"found": 0, "inserted": 0, "skipped": 0},
        "business_open_log": {"found": 0, "inserted": 0, "skipped": 0},
        "rent_runs": {"found": 0, "inserted": 0, "skipped": 0},
        "last_payment": {"found": 0, "inserted": 0, "skipped": 0},
        "cyberware_weekly_runs": {"found": 0, "inserted": 0, "skipped": 0},
        "cyberware_status": {"found": 0, "inserted": 0, "skipped": 0},
        "dm_threads": {"found": 0, "inserted": 0, "skipped": 0},
    }

    def count(res: str, s: dict) -> None:
        n = int(res.split()[-1])
        s["found"] += 1
        s["inserted"] += n
        s["skipped"] += 1 - n

    if dry_run:
        _print_dry_run(
            attendance, open_shop, rent_runs, last_payment_rent,
            cyber_runs, cyber_status, dm_threads, last_payment_trauma,
        )
        return summary

    async with pool.acquire() as conn:
        # attendance_log
        for user_id, logged_at in attendance:
            summary["attendance_log"]["found"] += 1
            res = await conn.execute(
                "INSERT INTO attendance_log (user_id, logged_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                str(user_id), logged_at,
            )
            n = int(res.split()[-1])
            summary["attendance_log"]["inserted"] += n
            summary["attendance_log"]["skipped"] += 1 - n

        # business_open_log
        for user_id, opened_at in open_shop:
            summary["business_open_log"]["found"] += 1
            res = await conn.execute(
                "INSERT INTO business_open_log (user_id, opened_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                str(user_id), opened_at,
            )
            n = int(res.split()[-1])
            summary["business_open_log"]["inserted"] += n
            summary["business_open_log"]["skipped"] += 1 - n

        # rent_runs (check for existing run within 5-min window to avoid duplicates)
        for run in rent_runs:
            summary["rent_runs"]["found"] += 1
            existing = await conn.fetchval(
                "SELECT id FROM rent_runs WHERE run_at BETWEEN $1 AND $2 LIMIT 1",
                run["run_at"] - timedelta(minutes=5),
                run["run_at"] + timedelta(minutes=5),
            )
            if existing:
                summary["rent_runs"]["skipped"] += 1
            else:
                await conn.execute(
                    "INSERT INTO rent_runs (run_at, initiated_by) VALUES ($1, $2)",
                    run["run_at"], run["initiated_by"],
                )
                summary["rent_runs"]["inserted"] += 1

        # last_payment from rent (upsert — trauma team can overwrite later if more recent)
        all_last_payment: dict[str, str] = dict(last_payment_rent)
        # Trauma team last_payment — only overwrite if more recent
        for uid, summary_str in last_payment_trauma.items():
            all_last_payment[uid] = summary_str  # trauma team entries are used as-is

        for user_id, lp_summary in all_last_payment.items():
            summary["last_payment"]["found"] += 1
            res = await conn.execute(
                """INSERT INTO last_payment (user_id, summary)
                   VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary""",
                str(user_id), lp_summary,
            )
            n = int(res.split()[-1])
            summary["last_payment"]["inserted"] += n
            summary["last_payment"]["skipped"] += 1 - n

        # cyberware_weekly_runs
        for run in cyber_runs:
            summary["cyberware_weekly_runs"]["found"] += 1
            existing = await conn.fetchval(
                "SELECT id FROM cyberware_weekly_runs WHERE run_at BETWEEN $1 AND $2 LIMIT 1",
                run["run_at"] - timedelta(minutes=30),
                run["run_at"] + timedelta(minutes=30),
            )
            if existing:
                summary["cyberware_weekly_runs"]["skipped"] += 1
            else:
                paid_ids = [p["user_id"] for p in run["paid"]]
                await conn.execute(
                    """INSERT INTO cyberware_weekly_runs (run_at, paid_ids, unpaid_ids, checkup_ids)
                       VALUES ($1, $2, $3, $4)""",
                    run["run_at"], paid_ids, run["unpaid"], run["checkup"],
                )
                summary["cyberware_weekly_runs"]["inserted"] += 1

        # cyberware_status
        for user_id, (weeks, last_processed) in cyber_status.items():
            summary["cyberware_status"]["found"] += 1
            res = await conn.execute(
                """INSERT INTO cyberware_status (user_id, weeks, last_processed)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id) DO UPDATE
                     SET weeks = EXCLUDED.weeks,
                         last_processed = EXCLUDED.last_processed
                   WHERE cyberware_status.last_processed IS NULL
                      OR EXCLUDED.last_processed > cyberware_status.last_processed""",
                str(user_id), weeks, last_processed,
            )
            n = int(res.split()[-1])
            summary["cyberware_status"]["inserted"] += n
            summary["cyberware_status"]["skipped"] += 1 - n

        # dm_threads
        for user_id, thread_id in dm_threads.items():
            summary["dm_threads"]["found"] += 1
            res = await conn.execute(
                """INSERT INTO dm_threads (user_id, thread_id)
                   VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET thread_id = EXCLUDED.thread_id""",
                str(user_id), thread_id,
            )
            n = int(res.split()[-1])
            summary["dm_threads"]["inserted"] += n
            summary["dm_threads"]["skipped"] += 1 - n

    return summary


def _print_dry_run(
    attendance, open_shop, rent_runs, last_payment_rent,
    cyber_runs, cyber_status, dm_threads, last_payment_trauma,
) -> None:
    print("\n" + "=" * 60)
    print("DRY RUN PREVIEW — nothing will be written")
    print("=" * 60)

    print(f"\n[attendance_log] {len(attendance)} records")
    for uid, ts in attendance[:10]:
        print(f"  user={uid}  logged_at={ts.isoformat()}")
    if len(attendance) > 10:
        print(f"  … and {len(attendance) - 10} more")

    print(f"\n[business_open_log] {len(open_shop)} records")
    for uid, ts in open_shop[:10]:
        print(f"  user={uid}  opened_at={ts.isoformat()}")
    if len(open_shop) > 10:
        print(f"  … and {len(open_shop) - 10} more")

    print(f"\n[rent_runs] {len(rent_runs)} runs")
    for run in rent_runs[:5]:
        print(f"  run_at={run['run_at'].isoformat()}  users={len(run['per_user'])}")
    if len(rent_runs) > 5:
        print(f"  … and {len(rent_runs) - 5} more")

    print(f"\n[last_payment from rent] {len(last_payment_rent)} users")
    for uid, s in list(last_payment_rent.items())[:5]:
        print(f"  user={uid}  summary={s[:80]}")
    if len(last_payment_rent) > 5:
        print(f"  … and {len(last_payment_rent) - 5} more")

    print(f"\n[cyberware_weekly_runs] {len(cyber_runs)} runs")
    for run in cyber_runs[:5]:
        print(f"  run_at={run['run_at'].isoformat()}  paid={len(run['paid'])}  unpaid={len(run['unpaid'])}  checkup={len(run['checkup'])}")
    if len(cyber_runs) > 5:
        print(f"  … and {len(cyber_runs) - 5} more")

    print(f"\n[cyberware_status] {len(cyber_status)} users")
    for uid, (weeks, ts) in list(cyber_status.items())[:10]:
        print(f"  user={uid}  weeks={weeks}  last_processed={ts.isoformat()}")
    if len(cyber_status) > 10:
        print(f"  … and {len(cyber_status) - 10} more")

    print(f"\n[dm_threads] {len(dm_threads)} mappings")
    for uid, tid in list(dm_threads.items())[:10]:
        print(f"  user={uid}  thread={tid}")
    if len(dm_threads) > 10:
        print(f"  … and {len(dm_threads) - 10} more")

    print(f"\n[last_payment from trauma_team] {len(last_payment_trauma)} users")
    for uid, s in list(last_payment_trauma.items())[:5]:
        print(f"  user={uid}  summary={s[:80]}")
    if len(last_payment_trauma) > 5:
        print(f"  … and {len(last_payment_trauma) - 5} more")

    print("\n" + "=" * 60)
    print("DRY RUN — nothing written")
    print("=" * 60)

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

    sections = [args.section] if args.section else ALL_SECTIONS
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

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    checkpoint = load_checkpoint()

    print(f"\nNightCityBot — Discord history restoration")
    print(f"Sections: {', '.join(sections)}")
    print(f"Dry run:  {dry_run}")
    print(f"Limit:    {args.limit} page(s) per channel" if args.limit else "Limit:    none (full history)")
    if checkpoint:
        print(f"Checkpoint: resuming from {CHECKPOINT_FILE}")
    print()

    # Collect results
    attendance_records: list[tuple[str, datetime]] = []
    open_shop_records: list[tuple[str, datetime]] = []
    rent_run_list: list[dict] = []
    last_payment_rent: dict[str, str] = {}
    cyber_run_list: list[dict] = []
    cyber_status_map: dict[str, tuple[int, datetime]] = {}
    dm_thread_map: dict[str, int] = {}
    last_payment_trauma: dict[str, str] = {}

    async with aiohttp.ClientSession(headers=headers) as session:

        if "attendance" in sections:
            print("--- Section: attendance ---")
            msgs = await fetch_channel_messages(
                session, CHANNEL_IDS["attendance"], checkpoint,
                args.limit, "attendance",
            )
            attendance_records = parse_attendance(msgs)
            print(f"  Parsed {len(attendance_records)} successful attendance records")

        if "open_shop" in sections:
            print("\n--- Section: open_shop ---")
            msgs = await fetch_channel_messages(
                session, CHANNEL_IDS["open_shop"], checkpoint,
                args.limit, "open_shop",
            )
            open_shop_records = parse_open_shop(msgs)
            print(f"  Parsed {len(open_shop_records)} successful shop-opening records")

        if "rent" in sections:
            print("\n--- Section: rent ---")
            msgs = await fetch_channel_messages(
                session, CHANNEL_IDS["rent"], checkpoint,
                args.limit, "rent",
            )
            rent_run_list, last_payment_rent = parse_rent(msgs)
            print(f"  Parsed {len(rent_run_list)} rent runs, {len(last_payment_rent)} users with last_payment")

        if "cyberware" in sections:
            print("\n--- Section: cyberware ---")
            msgs = await fetch_channel_messages(
                session, CHANNEL_IDS["cyberware"], checkpoint,
                args.limit, "cyberware",
            )
            cyber_run_list, cyber_status_map = parse_cyberware(msgs)
            print(f"  Parsed {len(cyber_run_list)} weekly runs, {len(cyber_status_map)} unique users")

        if "dm_threads" in sections:
            print("\n--- Section: dm_threads ---")
            threads = await fetch_threads_for_channel(
                session, CHANNEL_IDS["dm_threads"], checkpoint,
                args.limit, "dm_threads",
            )
            dm_thread_map = parse_dm_threads(threads)
            print(f"  Found {len(dm_thread_map)} user→thread mappings")

        if "trauma_team" in sections:
            print("\n--- Section: trauma_team ---")
            threads = await fetch_threads_for_channel(
                session, CHANNEL_IDS["trauma_team"], checkpoint,
                args.limit, "trauma_team",
            )
            # Fetch messages for each thread
            thread_messages: dict[int, list[dict]] = {}
            for t in threads:
                tid = int(t["id"])
                tmsgs = await fetch_channel_messages(
                    session, tid, checkpoint, args.limit,
                    f"trauma/{t.get('name', tid)[:30]}",
                )
                thread_messages[tid] = tmsgs
            last_payment_trauma = parse_trauma_team(threads, thread_messages)
            print(f"  Found {len(last_payment_trauma)} trauma team payment records")

    # Database write pass
    print("\n--- Writing to database ---" if not dry_run else "\n--- Dry run preview ---")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    try:
        summary = await write_to_db(
            pool,
            attendance_records,
            open_shop_records,
            rent_run_list,
            last_payment_rent,
            cyber_run_list,
            cyber_status_map,
            dm_thread_map,
            last_payment_trauma,
            dry_run=dry_run,
        )
    finally:
        await pool.close()

    if not dry_run:
        print("\n=== Summary ===")
        for table, counts in summary.items():
            if counts["found"] > 0:
                print(f"  {table:<30} found={counts['found']:>5}  inserted={counts['inserted']:>5}  skipped={counts['skipped']:>5}")
        print("\nSafe to re-run — all inserts use ON CONFLICT DO NOTHING or upsert.")
        if not args.section:
            delete_checkpoint()
            print(f"Checkpoint file deleted.")


if __name__ == "__main__":
    asyncio.run(main())
