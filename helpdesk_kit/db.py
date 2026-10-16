"""SQLite store. One file, WAL mode, every stage reads and writes here.

Append-only on purpose: `events` (what every stage did, in order), `attempts` (a write to a
system is tried once per ticket per record, never looped) and `fix_ledger` (a record is
auto-fixed at most once, ever; a trigger refuses deletes and updates, so the rule lives in
the database and not in a prompt).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    thread_json TEXT NOT NULL DEFAULT '[]',
    requester_name TEXT NOT NULL DEFAULT '',
    requester_email TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    vision_reasons_json TEXT NOT NULL DEFAULT '[]',
    images_json TEXT NOT NULL DEFAULT '[]',
    screenshot_json TEXT NOT NULL DEFAULT '{}',
    skipped_reason TEXT,
    duplicate_of TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
    ticket_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS enrichments (
    ticket_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS findings (
    ticket_id TEXT NOT NULL,
    field TEXT NOT NULL,
    claimed TEXT,
    actual TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS proposals (
    ticket_id TEXT PRIMARY KEY,
    rule_id TEXT,
    action TEXT,
    state TEXT,
    confidence TEXT,
    route_to_human INTEGER NOT NULL DEFAULT 1,
    route_reasons_json TEXT NOT NULL DEFAULT '[]',
    cc_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    reply TEXT,
    review_verdict TEXT,
    review_notes TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticket_id TEXT,
    stage TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_leases (
    lease_key TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    ticket_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    action TEXT NOT NULL,
    ts REAL NOT NULL,
    outcome TEXT,
    PRIMARY KEY (ticket_id, record_id, action)
);
CREATE TABLE IF NOT EXISTS rule_stats (
    rule_id TEXT PRIMARY KEY,
    times_solved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fix_ledger (
    system TEXT NOT NULL,
    record_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    action TEXT NOT NULL,
    ts REAL NOT NULL,
    outcome TEXT,
    PRIMARY KEY (system, record_id)
);
CREATE TRIGGER IF NOT EXISTS fix_ledger_no_delete BEFORE DELETE ON fix_ledger
BEGIN SELECT RAISE(ABORT, 'fix_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fix_ledger_no_rewrite BEFORE UPDATE OF system, record_id, ticket_id, action, ts
ON fix_ledger BEGIN SELECT RAISE(ABORT, 'fix_ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS row_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    unit INTEGER NOT NULL,
    system TEXT NOT NULL,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_results (
    ticket_id TEXT NOT NULL,
    unit INTEGER NOT NULL,
    issue INTEGER NOT NULL DEFAULT 0,
    ticket_type TEXT,
    state TEXT,
    rule_id TEXT,
    action TEXT,
    status TEXT,
    record_id TEXT,
    route_reasons_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    facts_json TEXT NOT NULL DEFAULT '{}',
    findings_json TEXT NOT NULL DEFAULT '[]',
    reply TEXT,
    PRIMARY KEY (ticket_id, unit)
);
CREATE TABLE IF NOT EXISTS rule_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    draft_yaml TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    tickets_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS ticket_approvals (
    ticket_id TEXT PRIMARY KEY,
    approved_by TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS waiting (
    ticket_id TEXT PRIMARY KEY,
    asked_at REAL NOT NULL,
    last_nudge_at REAL,
    reminders INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'WAITING'
);
CREATE TABLE IF NOT EXISTS queue_runs (
    run_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_jobs (
    run_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    ticket_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    result TEXT,
    error TEXT,
    updated_at REAL,
    PRIMARY KEY (run_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS seen_tickets (
    ticket_id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    comment_count INTEGER NOT NULL DEFAULT 0,
    outcome TEXT
);
"""

# Columns added after v1 shipped. A v1 database gets them on connect.
_ADDED_COLUMNS = {
    "proposals": [("reply_edited", "INTEGER NOT NULL DEFAULT 0"), ("sent_at", "REAL"),
                  ("revisions", "INTEGER NOT NULL DEFAULT 0"), ("issue_count", "INTEGER NOT NULL DEFAULT 1")],
}


def _migrate(conn) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def dumps(value: Any) -> str:
    """JSON for a *_json column. Never NULL: an absent value is an empty container."""
    if value is None:
        value = {}
    return json.dumps(value, sort_keys=True, default=str)


def log_event(conn, stage: str, ok: bool, ticket_id: str | None = None, **detail) -> None:
    conn.execute("INSERT INTO events (ts, ticket_id, stage, ok, detail_json) VALUES (?,?,?,?,?)",
                 (time.time(), ticket_id, stage, int(ok), dumps(detail)))


def events(conn, ticket_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id", (ticket_id,))
    return [dict(r) | {"detail": json.loads(r["detail_json"])} for r in rows]


# ---- run leases --------------------------------------------------------------------------
# One live run per ticket AND one per browser worker, across processes. A second run on the
# same worker would share one Chrome and click over the first run's tabs.

class LeaseHeld(RuntimeError):
    pass


def acquire_lease(conn, key: str, holder: str, ttl: float = 900) -> None:
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT holder, expires FROM run_leases WHERE lease_key=?", (key,)).fetchone()
        if row and row["expires"] > now and row["holder"] != holder:
            raise LeaseHeld(f"{key} is held by {row['holder']}")
        conn.execute("INSERT OR REPLACE INTO run_leases (lease_key, holder, expires) VALUES (?,?,?)",
                     (key, holder, now + ttl))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def release_lease(conn, key: str, holder: str) -> None:
    conn.execute("DELETE FROM run_leases WHERE lease_key=? AND holder=?", (key, holder))


@contextmanager
def leases(conn, keys: list[str]) -> Iterator[str]:
    holder = uuid.uuid4().hex[:12]
    taken: list[str] = []
    try:
        for key in keys:
            acquire_lease(conn, key, holder)
            taken.append(key)
        yield holder
    finally:
        for key in taken:
            release_lease(conn, key, holder)


# ---- one-attempt ledger --------------------------------------------------------------------

def claim_attempt(conn, ticket_id: str, record_id: str, action: str) -> bool:
    """True the first time; False if this ticket already tried this action on this record."""
    try:
        conn.execute("INSERT INTO attempts (ticket_id, record_id, action, ts) VALUES (?,?,?,?)",
                     (ticket_id, record_id, action, time.time()))
        return True
    except sqlite3.IntegrityError:
        return False


def record_attempt_outcome(conn, ticket_id: str, record_id: str, action: str, outcome: str) -> None:
    conn.execute("UPDATE attempts SET outcome=? WHERE ticket_id=? AND record_id=? AND action=?",
                 (outcome, ticket_id, record_id, action))


# ---- rule stats (feeds the auto-execute gate) ------------------------------------------------

def times_solved(conn, rule_id: str) -> int:
    row = conn.execute("SELECT times_solved FROM rule_stats WHERE rule_id=?", (rule_id,)).fetchone()
    return row["times_solved"] if row else 0


def mark_solved(conn, rule_id: str) -> None:
    conn.execute("INSERT INTO rule_stats (rule_id, times_solved) VALUES (?, 1) "
                 "ON CONFLICT(rule_id) DO UPDATE SET times_solved = times_solved + 1", (rule_id,))


# ---- simple row helpers ----------------------------------------------------------------------

def upsert(conn, table: str, row: dict, key: str) -> None:
    cols = list(row)
    keys = [k.strip() for k in key.split(",")]          # a composite key is "a, b"
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c not in keys)}",
        [row[c] for c in cols])


def get(conn, table: str, key: str, value: str) -> dict | None:
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    return dict(row) if row else None


def write_findings(conn, ticket_id: str, findings: list[dict]) -> None:
    """Delete + insert, so re-running analyze is safe."""
    conn.execute("DELETE FROM findings WHERE ticket_id=?", (ticket_id,))
    for f in findings:
        extra = {k: v for k, v in f.items() if k not in ("field", "claimed", "actual")}
        conn.execute("INSERT INTO findings (ticket_id, field, claimed, actual, extra_json) VALUES (?,?,?,?,?)",
                     (ticket_id, f["field"], f.get("claimed"), f.get("actual"), dumps(extra)))


# ---- fix ledger: a record is fixed by the agent at most once, enforced by the primary key ----

def claim_fix(conn, system: str, record_id: str, ticket_id: str, action: str) -> bool:
    """True the first time anyone claims this record; False forever after, for any ticket."""
    try:
        conn.execute("INSERT INTO fix_ledger (system, record_id, ticket_id, action, ts) VALUES (?,?,?,?,?)",
                     (system, record_id, ticket_id, action, time.time()))
        return True
    except sqlite3.IntegrityError:
        return False


def record_fix_outcome(conn, system: str, record_id: str, outcome: str) -> None:
    conn.execute("UPDATE fix_ledger SET outcome=? WHERE system=? AND record_id=?", (outcome, system, record_id))


def record_skip(conn, ticket_id: str, unit: int, system: str, record_id: str, reason: str) -> None:
    conn.execute("INSERT INTO row_skips (ticket_id, unit, system, record_id, reason, ts) VALUES (?,?,?,?,?,?)",
                 (ticket_id, unit, system, record_id, reason, time.time()))


def skips(conn, ticket_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM row_skips WHERE ticket_id=? ORDER BY id", (ticket_id,))]


# ---- per-ticket human approval (set from the dashboard, read by the pipeline) ------------------

def approve_ticket(conn, ticket_id: str, approved_by: str) -> None:
    conn.execute("INSERT OR REPLACE INTO ticket_approvals (ticket_id, approved_by, ts) VALUES (?,?,?)",
                 (ticket_id, approved_by, time.time()))


def is_ticket_approved(conn, ticket_id: str) -> bool:
    return conn.execute("SELECT 1 FROM ticket_approvals WHERE ticket_id=?", (ticket_id,)).fetchone() is not None
