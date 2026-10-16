"""Follow-up stage: tickets that are waiting on the requester.

When the agent sends a reply that asks the requester for something (a rule of kind "ask"),
the ticket is recorded in `waiting`. `run` then looks at each waiting ticket:

  requester answered       -> state REPLIED; the ticket is released from the seen registry so
                              the next queue run picks it up again
  quiet for REMIND_AFTER   -> a short reminder, then another every REMIND_EVERY, up to MAX_REMINDERS
  still quiet after that   -> one close-out message, state EXPIRED, and nothing more is sent

Reminders are fixed templates, not model text, but they go through the same send stage as
every other reply: house style, sanity check, hyperlinks, sign-off. `now` is a parameter so
the timing is testable without waiting.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import db, send

REMIND_AFTER = timedelta(hours=48)
REMIND_EVERY = timedelta(hours=48)
MAX_REMINDERS = 2
CLOSE_AFTER = timedelta(days=7)

REMINDER = """Hi there,

Just checking in on this ticket. We are still waiting for the details we asked for above, and we
cannot move forward without them. A short reply here is all it takes."""

CLOSE_OUT = """Hi there,

We have not heard back, so I am setting this ticket aside for now. If you still need help, reply
here and it will be picked up again."""


def mark_waiting(conn, ticket_id: str, asked_at: float | None = None) -> None:
    conn.execute("INSERT INTO waiting (ticket_id, asked_at, state) VALUES (?,?, 'WAITING') "
                 "ON CONFLICT(ticket_id) DO UPDATE SET asked_at=excluded.asked_at, last_nudge_at=NULL, "
                 "reminders=0, state='WAITING'", (ticket_id, asked_at if asked_at is not None else time.time()))


def _ts(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def requester_replied(source, ticket_id: str, since: float) -> bool:
    ticket = source.get_ticket(ticket_id)["ticket"]
    for c in source.get_comments(ticket_id):
        when = _ts(c.get("created_at", ""))
        if c.get("author_id") == ticket.get("requester_id") and c.get("public", True) and when and when > since:
            return True
    return False


def decide(row: dict, replied: bool, now: float) -> str:
    """Pure: what to do for one waiting row. One of replied, remind, close, wait."""
    if replied:
        return "replied"
    age = now - row["asked_at"]
    if row["reminders"] >= MAX_REMINDERS:
        return "close" if age >= CLOSE_AFTER.total_seconds() else "wait"
    last = row["last_nudge_at"] or row["asked_at"]
    gap = REMIND_AFTER if row["reminders"] == 0 else REMIND_EVERY
    return "remind" if now - last >= gap.total_seconds() else "wait"


def run(conn, source, sign_off: str, now: float | None = None, send_comments: bool = True) -> list[dict]:
    """Process every WAITING ticket once. Returns what was done: [{ticket, action}]."""
    now = now if now is not None else time.time()
    done: list[dict] = []
    for row in [dict(r) for r in conn.execute("SELECT * FROM waiting WHERE state='WAITING' ORDER BY ticket_id")]:
        tid = row["ticket_id"]
        action = decide(row, requester_replied(source, tid, row["asked_at"]), now)
        if action == "wait":
            continue
        if action == "replied":
            conn.execute("UPDATE waiting SET state='REPLIED' WHERE ticket_id=?", (tid,))
            conn.execute("DELETE FROM seen_tickets WHERE ticket_id=?", (tid,))
        else:
            text = REMINDER if action == "remind" else CLOSE_OUT
            html, problems = send.prepare(text, sign_off)
            if problems:
                db.log_event(conn, "followup", False, tid, action=action, problems=problems)
                continue
            if send_comments:
                source.add_comment(tid, html, public=True)
            if action == "remind":
                conn.execute("UPDATE waiting SET reminders=reminders+1, last_nudge_at=? WHERE ticket_id=?", (now, tid))
            else:
                conn.execute("UPDATE waiting SET state='EXPIRED' WHERE ticket_id=?", (tid,))
        db.log_event(conn, "followup", True, tid, action=action)
        done.append({"ticket": tid, "action": action})
    return done
