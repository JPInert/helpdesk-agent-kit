"""Rule proposals: what the agent does with an issue the knowledge base has no rule for.

The ticket still goes to a person. On top of that the agent drafts a new rule and files it
in a queue, with the evidence that supports it, for a person to approve or reject.

* One proposal per (ticket type, state). A second ticket with the same shape adds itself to
  the evidence instead of making a duplicate. A rejected proposal is not made again.
* Nothing the agent drafts runs by itself. Approving a proposal appends the rule to the
  approved-rules file (loaded next to kb/rules.yaml, origin "proposed"). If the state has an
  allowlisted fix, the draft names it and opts in to auto-execute; the track-record gate
  (safety.py) then keeps a person approving every fix until the rule has 5 confirmed solves.
  If it has none, the draft only hands the ticket to a person with a holding reply.
* Identity problems (who is this person?) never produce proposals; they are not a missing rule.
* The acceptance rate is tracked, so you can see whether the drafts are any good.

The drafting here is deterministic. It does not call a model.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import yaml

from . import db
from .analyze import IDENTITY_STATES
from .kb import Rule
from .reply import lint
from .safety import FIX_ALLOWLIST

_HANDOFF_REPLY = """Hi there,

Thanks for flagging this. Your request needs a closer look from the team that owns it, so I have
passed it to them with everything I found. They will reply on this ticket."""

_FIX_REPLY = """Hi there,

I found the cause of the problem with your ${%s} request and ran the fix. It now shows as
${new_status}.

If anything still looks wrong, reply here and we will take another look."""

# evidence keys that name the thing a ticket is about, in order of preference
_SUBJECT_KEYS = ("resource", "product")


def is_proposable(state: str | None, rule: Rule | None, dispatch: str) -> bool:
    """A record-backed issue whose state is a real finding, matched to nothing but the catch-all."""
    if dispatch != "record" or not state or state in IDENTITY_STATES:
        return False
    return rule is None or (rule.ticket_type in (None, "*") and not rule.keywords)


def signature(ticket_type: str, state: str) -> str:
    return f"{ticket_type}/{state}"


def rule_id(ticket_type: str, state: str) -> str:
    return "proposed-" + re.sub(r"[^a-z0-9]+", "-", f"{ticket_type}-{state}".lower()).strip("-")


def draft_rule_yaml(ticket_type: str, state: str, evidence_keys: list[str]) -> str:
    action = FIX_ALLOWLIST.get(state)
    subject = next((k for k in _SUBJECT_KEYS if k in evidence_keys), None)
    if action and subject:
        head = (f"- id: {rule_id(ticket_type, state)}\n  kind: assert\n  ticket_type: {ticket_type}\n"
                f"  state: {state}\n  action: {action}\n  auto_execute: true\n  route_to_human: false\n"
                f"  confidence: HIGH\n  needs: [new_status]\n")
        reply = _FIX_REPLY % subject
    else:
        head = (f"- id: {rule_id(ticket_type, state)}\n  kind: handoff\n  ticket_type: {ticket_type}\n"
                f"  state: {state}\n  action: none\n  route_to_human: true\n  confidence: MEDIUM\n")
        reply = _HANDOFF_REPLY
    return head + "  reply: |\n" + "".join(f"    {ln}\n" if ln else "\n" for ln in reply.split("\n"))


def validate_draft(draft_yaml: str) -> Rule:
    """The draft must load as a rule and pass the same house style as a shipped one."""
    items = yaml.safe_load(draft_yaml)
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("a draft is exactly one rule")
    rule = Rule(**items[0])
    if rule.kind == "assert" and not rule.state:
        raise ValueError("an assert rule must be pinned to a state")
    bad = [p for p in lint(rule.reply.strip()) if p != "unrendered placeholder"]
    if bad:
        raise ValueError(f"reply breaks house style: {'; '.join(bad)}")
    return rule


def propose(conn, ticket_id: str, ticket_type: str, state: str, evidence: dict, reasons: list[str]) -> int | None:
    """File (or extend) a proposal. Returns its id, or None if a person already rejected this shape."""
    sig = signature(ticket_type, state)
    row = conn.execute("SELECT * FROM rule_proposals WHERE signature=?", (sig,)).fetchone()
    safe = {k: v for k, v in evidence.items() if not re.search(r"email|cc|recipient|address", k, re.I)}
    if row:
        if row["status"] == "REJECTED":
            return None
        tickets = json.loads(row["tickets_json"])
        if ticket_id not in tickets:
            tickets.append(ticket_id)
            conn.execute("UPDATE rule_proposals SET tickets_json=? WHERE id=?", (json.dumps(tickets), row["id"]))
        return row["id"]
    draft = draft_rule_yaml(ticket_type, state, list(safe))
    cur = conn.execute(
        "INSERT INTO rule_proposals (signature, draft_yaml, evidence_json, tickets_json, created_at) VALUES (?,?,?,?,?)",
        (sig, draft, db.dumps({"ticket_type": ticket_type, "state": state, "evidence": safe, "why_human": reasons}),
         json.dumps([ticket_id]), time.time()))
    db.log_event(conn, "propose", True, ticket_id, proposal=cur.lastrowid, signature=sig)
    return cur.lastrowid


def pending(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM rule_proposals WHERE status='PENDING' ORDER BY id")]


def get(conn, pid: int) -> dict | None:
    row = conn.execute("SELECT * FROM rule_proposals WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def approve(conn, pid: int, who: str, approved_path: Path, edited_yaml: str | None = None) -> str:
    """Approve a pending proposal: validate it, append it to the approved-rules file."""
    row = get(conn, pid)
    if not row or row["status"] != "PENDING":
        raise ValueError(f"proposal {pid} is not pending")
    draft = edited_yaml if edited_yaml is not None else row["draft_yaml"]
    rule = validate_draft(draft)
    path = Path(approved_path)
    existing = (yaml.safe_load(path.read_text()) or []) if path.is_file() else []
    if any(r["id"] == rule.id for r in existing):
        raise ValueError(f"rule id {rule.id} is already approved")
    existing.append(yaml.safe_load(draft)[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False, width=100))
    conn.execute("UPDATE rule_proposals SET status='APPROVED', decided_at=?, decided_by=?, draft_yaml=? WHERE id=?",
                 (time.time(), who, draft, pid))
    db.log_event(conn, "proposal_decision", True, None, proposal=pid, decision="APPROVED")
    return rule.id


def reject(conn, pid: int, who: str) -> None:
    row = get(conn, pid)
    if not row or row["status"] != "PENDING":
        raise ValueError(f"proposal {pid} is not pending")
    conn.execute("UPDATE rule_proposals SET status='REJECTED', decided_at=?, decided_by=? WHERE id=?",
                 (time.time(), who, pid))
    db.log_event(conn, "proposal_decision", True, None, proposal=pid, decision="REJECTED")


def stats(conn) -> dict:
    """Counts only. acceptance_rate is accepted / decided, or None before any decision."""
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM rule_proposals GROUP BY status")}
    accepted, rejected, waiting = counts.get("APPROVED", 0), counts.get("REJECTED", 0), counts.get("PENDING", 0)
    decided = accepted + rejected
    return {"proposed": accepted + rejected + waiting, "accepted": accepted, "rejected": rejected,
            "pending": waiting, "acceptance_rate": round(accepted / decided, 2) if decided else None}
