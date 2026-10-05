"""Run one ticket end to end:

  ingest -> (vision) -> classify -> enrich -> analyze -> match -> (apply) -> reply -> review -> (post)

Safe by default. Nothing is written anywhere outside the local DB unless you ask:
  apply=True        lets the agent press buttons in the portal
  post="note"       puts the drafted reply on the ticket as an INTERNAL note for a human
  post="public"     sends it to the requester, only if the reviewer said PASS and no human is needed
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

from . import analyze as analyze_mod
from . import classify, db, enrich, ingest, vision
from .config import Settings
from .kb import KB, should_auto_execute
from .reply import MissingEvidence, render


@dataclass
class Deps:
    settings: Settings
    source: object                     # zendesk.ZendeskClient or zendesk.FixtureSource
    classifier: object                 # classify.ClaudeClassifier or KeywordClassifier
    vision: object                     # vision.ClaudeVision or SidecarVision
    reviewer: object                   # reply.ClaudeReviewer or LintReviewer
    portal_factory: Callable[[], object]
    kb: KB
    worker_id: int = 0


@dataclass
class Result:
    ticket_id: str
    status: str
    ticket_type: str | None = None
    state: str | None = None
    rule_id: str | None = None
    action: str | None = None
    route_reasons: list[str] = field(default_factory=list)
    reply: str | None = None
    review: str | None = None
    fixed: bool = False


def _resolve_cc(kb: KB, roles: list[str], evidence: dict) -> list[str]:
    out = []
    for role in roles:
        addr = kb.contacts[role]
        out.append(evidence.get(role, "") if addr == "from_portal" else addr)
    return [a for a in out if a]


def _save(conn, r: Result, a, rule, cc: list[str], confidence: str) -> None:
    db.upsert(conn, "proposals", {
        "ticket_id": r.ticket_id, "rule_id": r.rule_id, "action": r.action, "state": r.state,
        "confidence": confidence, "route_to_human": int(bool(r.route_reasons)),
        "route_reasons_json": db.dumps(r.route_reasons), "cc_json": db.dumps(cc),
        "status": r.status, "reply": r.reply, "review_verdict": r.review,
        "review_notes": None, "updated_at": time.time()}, "ticket_id")


def run_ticket(conn, deps: Deps, ticket_id: str, *, apply: bool = False, approve: bool = False,
               post: str = "none") -> Result:
    if post not in ("none", "note", "public"):
        raise ValueError("post must be none, note or public")
    with db.leases(conn, [f"ticket:{ticket_id}", f"worker:{deps.worker_id}"]), ExitStack() as stack:
        t = ingest.run(conn, deps.source, ticket_id)
        if t["skipped_reason"]:
            return Result(ticket_id, "SKIPPED", route_reasons=[t["skipped_reason"]])
        if t["duplicate_of"]:
            db.log_event(conn, "route", True, ticket_id, duplicate_of=t["duplicate_of"])
            return Result(ticket_id, "DUPLICATE", route_reasons=[f"duplicate of #{t['duplicate_of']}"])

        if json.loads(t["vision_reasons_json"]) and json.loads(t["images_json"]):
            vision.run(conn, deps.source, deps.vision, t)
            t = db.get(conn, "tickets", "id", ticket_id)

        c = classify.run(conn, deps.classifier, t, deps.settings.staff_domain)

        portal = None
        enrichment = None
        if analyze_mod.needs_record(c):
            portal = stack.enter_context(deps.portal_factory())
            enrichment = enrich.run(conn, portal, t, c)
        a = analyze_mod.analyze(c, enrichment, stuck_after_minutes=deps.settings.stuck_after_minutes)
        db.write_findings(conn, ticket_id, a.findings)

        rule = deps.kb.match(f"{t['subject']}\n{t['body']}", c.ticket_type.value, c.submitter_kind, a.state)
        r = Result(ticket_id, "PENDING", c.ticket_type.value, a.state, rule.id if rule else None,
                   rule.action if rule else None, list(a.route_reasons))
        confidence = a.confidence if a.dispatch == "record" else (rule.confidence or "MEDIUM") if rule else "LOW"
        if rule is None:
            r.route_reasons.append("no knowledge-base rule matches")
        elif rule.route_to_human:
            r.route_reasons.append(f"rule {rule.id} always goes to a person")
        if confidence == "LOW" and not r.route_reasons:
            r.route_reasons.append("low confidence")

        evidence = dict(a.evidence)
        cc = _resolve_cc(deps.kb, rule.cc, evidence) if rule else []

        # ---- apply -----------------------------------------------------------------------
        if rule and rule.action == "retry_job" and not r.route_reasons:
            auto = should_auto_execute(rule, confidence, db.times_solved(conn, rule.id))
            if not (auto or approve):
                r.status = "AWAITING_APPROVAL"
            elif not apply:
                r.status = "WOULD_APPLY"
            elif not db.claim_attempt(conn, ticket_id, a.target["id"], "retry_job"):
                r.route_reasons.append(f"already tried {a.target['id']} once on this ticket; not looping")
            else:
                try:
                    portal.retry_job(a.target["user_id"], a.target["id"])
                    flip = portal.wait_for_flip(a.target["user_id"], a.target["id"],
                                                timeout_s=deps.settings.flip_timeout_s)
                except Exception as e:
                    flip = None
                    r.route_reasons.append(f"retry failed in the portal: {type(e).__name__}")
                if flip and flip.confirmed and flip.status == "done":
                    evidence["new_status"] = "active"
                    db.mark_solved(conn, rule.id)
                    r.status, r.fixed = "APPLIED", True
                elif flip and flip.confirmed:
                    r.route_reasons.append(f"retry finished as {flip.status} ({flip.error})")
                elif flip:
                    r.route_reasons.append(f"retry not confirmed after {flip.waited_s:.0f}s (still {flip.status})")
                db.record_attempt_outcome(conn, ticket_id, a.target["id"], "retry_job",
                                          r.status if r.status == "APPLIED" else "; ".join(r.route_reasons))
            db.log_event(conn, "apply", r.status == "APPLIED", ticket_id, status=r.status, auto=auto,
                         reasons=r.route_reasons)

        # ---- reply -----------------------------------------------------------------------
        if rule and r.status in ("PENDING", "APPLIED"):
            try:
                r.reply = render(rule.reply, evidence, rule.needs)
            except MissingEvidence as e:
                r.route_reasons.append(str(e))
            if r.reply:
                outcome = deps.reviewer.review(r.reply, evidence | {"ticket_type": c.ticket_type.value})
                r.review = outcome.verdict
                db.log_event(conn, "review", not outcome.blocks_send, ticket_id, verdict=outcome.verdict,
                             notes=outcome.notes)
                if outcome.blocks_send:
                    r.route_reasons.append(f"reply review {outcome.verdict}: {outcome.notes}")

        if r.status in ("PENDING", "APPLIED"):
            r.status = "HUMAN_REVIEW_REQUIRED" if r.route_reasons else "REPLY_READY"

        # ---- post ------------------------------------------------------------------------
        if r.reply and post == "public" and r.status == "REPLY_READY":
            deps.source.add_comment(ticket_id, r.reply.replace("\n", "<br>"), public=True, status="pending",
                                    cc_emails=[x for x in cc if "@" in x])
            r.status = "SENT"
        elif r.reply and post in ("note", "public"):
            deps.source.add_comment(ticket_id, ("<b>Draft reply (agent):</b><br>" + r.reply.replace("\n", "<br>")),
                                    public=False)
        if post != "none":
            db.log_event(conn, "post", True, ticket_id, status=r.status)

        _save(conn, r, a, rule, cc, confidence)
        db.log_event(conn, "route", True, ticket_id, status=r.status, rule=r.rule_id, reasons=r.route_reasons)
        return r
