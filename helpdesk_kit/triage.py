"""Queue triage: harvest the queue, put it in a sensible order, and never do a ticket twice.

* harvest   reads every open ticket in the view (cheap fields only, no model)
* order     urgency first (the ticket's own priority, bumped by words like "urgent" or "today"),
            then type (things that block someone before things that can wait), then age (oldest first)
* jobs      a run is a set of rows in `queue_jobs`. A crashed or interrupted run is resumed
            with its run id: finished jobs stay finished, anything RUNNING or PENDING is redone
* seen      `seen_tickets` remembers every ticket the agent has processed and how many comments
            it had then. A ticket is only picked up again if it has new comments (the requester
            answered) since. `run --ticket N` ignores the registry on purpose.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .classify import KEYWORDS, TicketType

PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3, None: 2}
URGENT_WORDS = ("urgent", "asap", "today", "right now", "deadline", "can't work", "cannot work", "blocked")
# Lower = handled first. Blocking problems before questions before hardware.
TYPE_RANK = {TicketType.ACCOUNT_SUSPENDED: 0, TicketType.LOGIN_FAILED: 0, TicketType.SESSION_REJECTED: 1,
             TicketType.ACCESS_PENDING: 1, TicketType.LICENSE_NOT_ASSIGNED: 1, TicketType.BILLING_QUESTION: 2,
             TicketType.POLICY_QUESTION: 3, TicketType.HARDWARE: 4}


@dataclass(frozen=True)
class QueueItem:
    ticket_id: str
    created_at: str
    priority: str | None
    urgency: int
    kind: TicketType
    comments: int

    def age_minutes(self, now: datetime) -> float:
        try:
            return (now - datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            return 0.0


def quick_kind(text: str) -> TicketType:
    """A cheap keyword guess at the ticket type, only used to order the queue. The real
    classification happens later and never trusts this."""
    from .classify import SESSION_WORDS
    low = text.lower()
    for t, words in KEYWORDS:
        if t is TicketType.SESSION_REJECTED:
            if SESSION_WORDS.search(low):
                return t
        elif any(w in low for w in words):
            return t
    return TicketType.AMBIGUOUS


def urgency(priority: str | None, text: str) -> int:
    """0 = most urgent. A stated priority sets the level; urgent wording bumps it up one."""
    level = PRIORITY_RANK.get((priority or "").lower() or None, 2)
    if any(w in text.lower() for w in URGENT_WORDS):
        level = max(0, level - 1)
    return level


def harvest(source, view_id: str = "", limit: int = 50) -> list[QueueItem]:
    items = []
    for tid in source.view_ticket_ids(view_id, limit):
        data = source.get_ticket(tid)
        t = data["ticket"]
        text = f"{t.get('subject', '')}\n{t.get('description', '')}"
        items.append(QueueItem(tid, t.get("created_at", ""), t.get("priority"),
                               urgency(t.get("priority"), text), quick_kind(text),
                               len(source.get_comments(tid))))
    return items


def order(items: list[QueueItem], now: datetime | None = None) -> list[QueueItem]:
    now = now or datetime.now(timezone.utc)
    return sorted(items, key=lambda i: (i.urgency, TYPE_RANK.get(i.kind, 5), -i.age_minutes(now), i.ticket_id))


# ---- seen registry ----------------------------------------------------------------------------

def is_seen(conn, ticket_id: str, comments: int) -> bool:
    row = conn.execute("SELECT comment_count FROM seen_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    return bool(row) and comments <= row["comment_count"]


def mark_seen(conn, ticket_id: str, comments: int, outcome: str) -> None:
    now = time.time()
    conn.execute("INSERT INTO seen_tickets (ticket_id, first_seen, last_seen, comment_count, outcome) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(ticket_id) DO UPDATE SET last_seen=excluded.last_seen, "
                 "comment_count=excluded.comment_count, outcome=excluded.outcome", (ticket_id, now, now, comments, outcome))


def unseen(conn, items: list[QueueItem]) -> list[QueueItem]:
    return [i for i in items if not is_seen(conn, i.ticket_id, i.comments)]


# ---- job table ----------------------------------------------------------------------------------

def new_run(conn, items: list[QueueItem]) -> str:
    run_id = uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO queue_runs (run_id, created_at) VALUES (?,?)", (run_id, time.time()))
    for pos, item in enumerate(items):
        conn.execute("INSERT INTO queue_jobs (run_id, position, ticket_id, updated_at) VALUES (?,?,?,?)",
                     (run_id, pos, item.ticket_id, time.time()))
    return run_id


def next_job(conn, run_id: str) -> str | None:
    """The first job not DONE/FAILED/SKIPPED, marked RUNNING. A RUNNING row left by a crash counts."""
    row = conn.execute("SELECT ticket_id FROM queue_jobs WHERE run_id=? AND state IN ('PENDING','RUNNING') "
                       "ORDER BY position LIMIT 1", (run_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE queue_jobs SET state='RUNNING', updated_at=? WHERE run_id=? AND ticket_id=?",
                 (time.time(), run_id, row["ticket_id"]))
    return row["ticket_id"]


def finish_job(conn, run_id: str, ticket_id: str, state: str, result: str = "", error: str | None = None) -> None:
    conn.execute("UPDATE queue_jobs SET state=?, result=?, error=?, updated_at=? WHERE run_id=? AND ticket_id=?",
                 (state, result, error, time.time(), run_id, ticket_id))


def job_counts(conn, run_id: str) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) n FROM queue_jobs WHERE run_id=? GROUP BY state", (run_id,))}


def latest_run(conn) -> str | None:
    row = conn.execute("SELECT run_id FROM queue_runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def work(conn, source, run_id: str, process) -> list:
    """Run `process(ticket_id)` for each remaining job in order, recording state as it goes.
    A job that raises is marked FAILED and the run carries on. Returns the processed results."""
    results = []
    while (tid := next_job(conn, run_id)) is not None:
        try:
            res = process(tid)
            status = getattr(res, "status", "DONE")
            finish_job(conn, run_id, tid, "DONE", status)
            mark_seen(conn, tid, len(source.get_comments(tid)), status)
            results.append(res)
        except Exception as e:           # one bad ticket must not stop the queue
            finish_job(conn, run_id, tid, "FAILED", error=type(e).__name__)
    return results
