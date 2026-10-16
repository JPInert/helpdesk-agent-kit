"""Run one ticket end to end:

  ingest -> (vision) -> classify -> per issue: enrich -> analyze -> match -> (apply) -> reply
         -> merge replies -> review/revise -> (post)

A ticket can hold several issues, and an issue about session records yields one unit per
rejected record. Every unit is analyzed, matched and fixed on its own; only the replies are
merged, into one message, and that message is what the reviewer checks.

Safe by default. Nothing is written anywhere outside the local DB unless you ask:
  apply=True        lets the agent write to the back-office systems (still behind safety.py)
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
from . import classify, db, enrich, followup, ingest, proposals, safety, send, vision
from .actions import ACTIONS
from .classify import TicketType
from .config import Settings
from .kb import KB
from .reply import MissingEvidence, merge_replies, render
from .revise import review_and_revise
from .systems import SystemSet


@dataclass
class Deps:
    settings: Settings
    source: object                     # zendesk.ZendeskClient or zendesk.FixtureSource
    classifier: object                 # classify.ClaudeClassifier or KeywordClassifier
    vision: object                     # vision.ClaudeVision or SidecarVision
    reviewer: object                   # reply.ClaudeReviewer or LintReviewer
    systems_factory: Callable[[], SystemSet]
    kb: KB
    reviser: object | None = None      # revise.ClaudeReviser or RuleReviser
    worker_id: int = 0


@dataclass
class UnitResult:
    unit: int
    issue: int
    ticket_type: str
    state: str | None = None
    rule_id: str | None = None
    action: str | None = None
    status: str = "PENDING"
    route_reasons: list[str] = field(default_factory=list)
    reply: str | None = None
    record_id: str | None = None
    evidence: dict = field(default_factory=dict)
    cc: list[str] = field(default_factory=list)
    fixed: bool = False
    proposal: int | None = None


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
    units: list[UnitResult] = field(default_factory=list)
    revisions: int = 0


def _resolve_cc(kb: KB, roles: list[str], evidence: dict) -> list[str]:
    out = []
    for role in roles:
        addr = kb.contacts[role]
        out.append(evidence.get(role, "") if addr == "from_portal" else addr)
    return [a for a in out if a]


def _save(conn, r: Result, cc: list[str], confidence: str) -> None:
    db.upsert(conn, "proposals", {
        "ticket_id": r.ticket_id, "rule_id": r.rule_id, "action": r.action, "state": r.state,
        "confidence": confidence, "route_to_human": int(bool(r.route_reasons)),
        "route_reasons_json": db.dumps(r.route_reasons), "cc_json": db.dumps(cc),
        "status": r.status, "reply": r.reply, "review_verdict": r.review,
        "review_notes": None, "updated_at": time.time(), "revisions": r.revisions,
        "issue_count": len({u.issue for u in r.units}) or 1}, "ticket_id")


def _save_unit(conn, ticket_id: str, u: UnitResult, facts: dict, findings: list[dict]) -> None:
    db.upsert(conn, "unit_results", {
        "ticket_id": ticket_id, "unit": u.unit, "issue": u.issue, "ticket_type": u.ticket_type,
        "state": u.state, "rule_id": u.rule_id, "action": u.action, "status": u.status,
        "record_id": u.record_id, "route_reasons_json": db.dumps(u.route_reasons),
        "evidence_json": db.dumps(u.evidence), "facts_json": db.dumps(facts),
        "findings_json": json.dumps(findings), "reply": u.reply}, "ticket_id, unit")


def _skip(conn, ticket_id: str, u: UnitResult, system: str, reason: str) -> None:
    db.record_skip(conn, ticket_id, u.unit, system, u.record_id or "", reason)
    db.log_event(conn, "skip", True, ticket_id, unit=u.unit, record=u.record_id, reason=reason)


def _apply(conn, deps: Deps, systems: SystemSet, ticket_id: str, u: UnitResult, rule, a, confidence: str,
           evidence: dict, *, apply: bool, approved: bool) -> str | None:
    """Decide and, if allowed, run the fix for one unit. Returns the skip reason if the
    record was refused, so the caller records it."""
    action = ACTIONS[rule.action]
    auto = safety.should_auto_execute(rule, confidence, db.times_solved(conn, rule.id), state=a.state,
                                      route_reasons=u.route_reasons)
    with_approval = safety.may_execute_with_approval(rule, a.state, u.route_reasons)
    if not with_approval:
        u.route_reasons.append(f"fix refused: {with_approval.reason}")
        return with_approval.reason
    if not (auto or approved):
        u.status = "AWAITING_APPROVAL"
        return None
    if not apply:
        u.status = "WOULD_APPLY"
        return None
    rid = a.target["id"]
    if not db.claim_attempt(conn, ticket_id, rid, action.name):
        reason = f"already tried {rid} once on this ticket; not looping"
        u.route_reasons.append(reason)
        return reason
    if not db.claim_fix(conn, action.system, rid, ticket_id, action.name):
        reason = f"{rid} was already fixed once by the agent (ledger); not fixing it again"
        u.route_reasons.append(reason)
        db.record_attempt_outcome(conn, ticket_id, rid, action.name, reason)
        return reason
    adapter = systems.get(action.system)
    try:
        action.run(adapter, a.target)
        flip = action.confirm(adapter, a.target, deps.settings.flip_timeout_s)
    except Exception as e:
        flip = None
        u.route_reasons.append(f"{action.name} failed in {action.system}: {type(e).__name__}")
    if flip and flip.confirmed and flip.status == action.done_status:
        evidence["new_status"] = action.new_status
        db.mark_solved(conn, rule.id)
        u.status, u.fixed = "APPLIED", True
    elif flip and flip.confirmed:
        u.route_reasons.append(f"{action.name} finished as {flip.status} ({flip.error})")
    elif flip:
        u.route_reasons.append(f"{action.name} not confirmed after {flip.waited_s:.0f}s (still {flip.status})")
    outcome = "APPLIED" if u.fixed else "; ".join(u.route_reasons)
    db.record_attempt_outcome(conn, ticket_id, rid, action.name, outcome)
    db.record_fix_outcome(conn, action.system, rid, outcome)
    db.log_event(conn, "apply", u.fixed, ticket_id, unit=u.unit, status=u.status, auto=bool(auto),
                 reasons=u.route_reasons)
    return None


def _process_unit(conn, deps: Deps, systems: SystemSet, t: dict, c, ic, a, n: int, issue: int, single: bool,
                  *, apply: bool, approved: bool) -> tuple[UnitResult, str, list[dict]]:
    ticket_id = t["id"]
    text = f"{t['subject']}\n{t['body']}" if single else ic.summary
    rule = deps.kb.match(text, ic.ticket_type.value, c.submitter_kind, a.state)
    u = UnitResult(n, issue, ic.ticket_type.value, a.state, rule.id if rule else None,
                   rule.action if rule else None, route_reasons=list(a.route_reasons),
                   record_id=(a.target or {}).get("id"))
    confidence = a.confidence if a.dispatch == "record" else (rule.confidence or "MEDIUM") if rule else "LOW"
    if rule is None:
        u.route_reasons.append("no knowledge-base rule matches")
    elif rule.route_to_human:
        u.route_reasons.append(f"rule {rule.id} always goes to a person")
    if confidence == "LOW" and not u.route_reasons:
        u.route_reasons.append("low confidence")

    evidence = dict(a.evidence)
    u.evidence = evidence
    u.cc = _resolve_cc(deps.kb, rule.cc, evidence) if rule else []

    skip_reason: str | None = None
    if rule and rule.action in ACTIONS and a.target and not u.route_reasons:
        skip_reason = _apply(conn, deps, systems, ticket_id, u, rule, a, confidence, evidence,
                             apply=apply, approved=approved)
        if u.status in ("AWAITING_APPROVAL", "WOULD_APPLY"):
            db.log_event(conn, "apply", False, ticket_id, unit=u.unit, status=u.status, reasons=u.route_reasons)

    if rule and u.status in ("PENDING", "APPLIED"):
        try:
            u.reply = render(rule.reply, evidence, rule.needs)
        except MissingEvidence as e:
            u.route_reasons.append(str(e))
    if u.status in ("PENDING", "APPLIED"):
        u.status = "HUMAN_REVIEW_REQUIRED" if u.route_reasons else "REPLY_READY"

    if proposals.is_proposable(a.state, rule, a.dispatch):
        u.proposal = proposals.propose(conn, ticket_id, u.ticket_type, a.state, evidence, u.route_reasons)
    if a.target and _needs_fix(a) and not u.fixed and u.status not in ("AWAITING_APPROVAL", "WOULD_APPLY"):
        _skip(conn, ticket_id, u, a.system,
              skip_reason or "; ".join(u.route_reasons) or f"rule {u.rule_id} has no automatic fix")
    return u, confidence, a.findings


def _needs_fix(a) -> bool:
    """Is the target record actually broken (so not fixing it deserves a recorded reason)?"""
    return bool(a.diagnosis_codes) or a.state in ("job_stuck", "job_failed", "seat_pool_exhausted")


def _ticket_status(units: list[UnitResult], blocked: bool) -> str:
    statuses = {u.status for u in units}
    if "HUMAN_REVIEW_REQUIRED" in statuses or blocked:
        return "HUMAN_REVIEW_REQUIRED"
    for s in ("AWAITING_APPROVAL", "WOULD_APPLY"):
        if s in statuses:
            return s
    return "REPLY_READY"


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
        approved = approve or db.is_ticket_approved(conn, ticket_id)
        systems = stack.enter_context(deps.systems_factory())

        issues = c.issues()
        units: list[UnitResult] = []
        confidences: list[str] = []
        enrichments: list[dict] = []
        all_findings: list[dict] = []
        unit_facts: list[tuple[dict, list[dict]]] = []
        for issue_idx, ic in enumerate(issues):
            enrichment = None
            if analyze_mod.needs_record(ic):
                enrichment = enrich.run(conn, systems, t, ic)
                enrichments.append(enrichment)
            if ic.ticket_type == TicketType.SESSION_REJECTED:
                analyses = analyze_mod.analyze_sessions(ic, enrichment)
            else:
                analyses = [analyze_mod.analyze(ic, enrichment, stuck_after_minutes=deps.settings.stuck_after_minutes)]
            for a in analyses:
                u, confidence, findings = _process_unit(conn, deps, systems, t, c, ic, a, len(units), issue_idx,
                                                        len(issues) == 1,
                                                        apply=apply, approved=approved)
                units.append(u)
                confidences.append(confidence)
                unit_facts.append((a.facts, findings))
                all_findings += [f | {"issue": issue_idx, "unit": u.unit} for f in findings]

        db.write_findings(conn, ticket_id, all_findings)
        db.upsert(conn, "enrichments", {"ticket_id": ticket_id, "data_json": db.dumps({"issues": enrichments})},
                  "ticket_id")

        first = units[0]
        r = Result(ticket_id, "PENDING", first.ticket_type, first.state, first.rule_id, first.action,
                   units=units, fixed=any(u.fixed for u in units))
        for u in units:
            for why in u.route_reasons:
                if why not in r.route_reasons:
                    r.route_reasons.append(why)
        cc = list(dict.fromkeys(x for u in units for x in u.cc))

        # ---- reply: merge, review, revise ----------------------------------------------------
        bodies = [u.reply for u in units if u.reply]
        blocked = False
        if bodies:
            reply = bodies[0] if len(bodies) == 1 else merge_replies(bodies)
            facts = (units[0].evidence if len(units) == 1 else
                     {f"issue{u.unit + 1}_{k}": v for u in units for k, v in u.evidence.items()})
            facts = facts | {"ticket_type": first.ticket_type}
            rr = review_and_revise(deps.reviewer, deps.reviser, reply, facts)
            r.reply, r.review, r.revisions = rr.reply, rr.outcome.verdict, len(rr.rounds)
            db.log_event(conn, "review", not rr.outcome.blocks_send, ticket_id, verdict=rr.outcome.verdict,
                         notes=rr.outcome.notes, revisions=len(rr.rounds))
            if rr.outcome.blocks_send:
                blocked = True
                r.route_reasons.append(f"reply review {rr.outcome.verdict}: {rr.outcome.notes}")
        r.status = _ticket_status(units, blocked)

        # ---- post ----------------------------------------------------------------------------
        if r.reply and post == "public" and r.status == "REPLY_READY":
            body_html, problems = send.prepare(r.reply, deps.settings.sign_off)
            if problems:
                r.route_reasons.append("send blocked: " + "; ".join(problems))
                r.status = "HUMAN_REVIEW_REQUIRED"
            else:
                deps.source.add_comment(ticket_id, body_html, public=True, status="pending",
                                        cc_emails=[x for x in cc if "@" in x])
                r.status = "SENT"
                if any(deps.kb.rule(u.rule_id) and deps.kb.rule(u.rule_id).kind == "ask" for u in units):
                    followup.mark_waiting(conn, ticket_id)
        if r.reply and post in ("note", "public") and r.status != "SENT":
            left = [u for u in units if not u.reply]
            gap = (f"<i>Not covered by this draft: {', '.join(sorted({u.ticket_type + ' ' + (u.record_id or '') for u in left}))}.</i><br>"
                   if left else "")
            deps.source.add_comment(ticket_id, "<b>Draft reply (agent):</b><br>" + gap + send.to_html(r.reply, deps.settings.sign_off),
                                    public=False)
        if post != "none":
            db.log_event(conn, "post", True, ticket_id, status=r.status)

        conn.execute("DELETE FROM unit_results WHERE ticket_id=?", (ticket_id,))     # a re-run may have fewer units
        for u, (facts_json, findings) in zip(units, unit_facts):
            _save_unit(conn, ticket_id, u, facts_json, findings)
        _save(conn, r, cc, confidences[0])
        db.log_event(conn, "route", True, ticket_id, status=r.status, rule=r.rule_id, reasons=r.route_reasons,
                     units=len(units), issues=len(issues))
        return r
