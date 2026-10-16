"""Analyze: compare what the ticket says with what the portal shows. Pure and deterministic:
no model, no knowledge-base lookup, no I/O. Same inputs, same answer, easy to test.

It branches on what the DATA looks like (no such person, two candidates, a stuck job, an
empty seat pool...), never on the ticket-type label, because the label is a model's guess and
the data is not. The type label is only used later, to pick the reply.

Output: a state name, a confidence, the reasons a human is needed (if any), the findings
(claim vs record), and the evidence a reply is allowed to cite.

Session tickets (a logged elevated-access session marked invalid) are analyzed per rejected
record: `analyze_sessions` returns one Analysis per record, each diagnosed by diagnose.py
from the merged facts of the approvals system and the activity log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .approval import Approval, compare_claims
from .classify import Classification, TicketType
from .diagnose import SessionRecord, diagnose, hours_in_week, pick_approval
from .facts import Facts

RECORD_TYPES = {TicketType.LOGIN_FAILED, TicketType.ACCESS_PENDING, TicketType.LICENSE_NOT_ASSIGNED,
                TicketType.ACCOUNT_SUSPENDED, TicketType.BILLING_QUESTION, TicketType.SESSION_REJECTED}

# States that only say "we could not tell who or what this is". A person handles them;
# they are never worth proposing a rule for.
IDENTITY_STATES = {"unresolvable_person", "ambiguous_person", "user_not_found", "lookup_failed"}


@dataclass
class Analysis:
    dispatch: str
    state: str | None = None
    confidence: str = "LOW"
    route_reasons: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    target: dict | None = None      # the record a fix would act on
    system: str = "provisioning"    # which system the target record lives in
    diagnosis_codes: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # Facts.to_json() for this issue, for the dashboard


def needs_record(c: Classification) -> bool:
    return c.ticket_type in RECORD_TYPES


def _age_minutes(created_at: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds() / 60


def _identity(c: Classification, e: dict) -> tuple[Analysis, dict | None]:
    """Who is this about? Anything short of exactly one account goes to a person."""
    a = Analysis(dispatch="record", facts=e.get("facts") or {})
    if c.addressed_to:
        a.route_reasons.append(f"addressed to {c.addressed_to}")
    if e.get("error") == "abbreviated_name":
        a.state = "unresolvable_person"
        a.route_reasons.append(f"name '{c.affected_name}' is abbreviated; not guessing who it is")
        return a, None
    if e.get("error"):
        a.state = "lookup_failed"
        a.route_reasons.append(f"portal lookup failed ({e['error']})")
        return a, None
    if len(e.get("candidates") or []) > 1:
        a.state = "ambiguous_person"
        a.route_reasons.append("more than one account matches: "
                               + ", ".join(x["name"] for x in e["candidates"]))
        return a, None
    user = e.get("user")
    if not user:
        a.state = "user_not_found"
        a.route_reasons.append("no portal account matches the person on the ticket")
        return a, None
    return a, user


def analyze(c: Classification, enrichment: dict | None, now: datetime | None = None,
            stuck_after_minutes: int = 30) -> Analysis:
    now = now or datetime.now(timezone.utc)
    if not needs_record(c):
        a = Analysis(dispatch="non_record", confidence="HIGH")
        if c.addressed_to:
            a.route_reasons.append(f"addressed to {c.addressed_to}")
        return a

    e = enrichment or {}
    a, user = _identity(c, e)
    if user is None:
        return a

    a.evidence.update(person=user["name"], account_status=user["status"], manager=user.get("manager", ""))

    if user["status"] != "active":
        a.state, a.confidence = "account_suspended", "HIGH"
        a.findings.append({"field": "account_status", "claimed": "active", "actual": user["status"]})
        return a

    if c.ticket_type == TicketType.LOGIN_FAILED:
        a.state, a.confidence = "account_active", "HIGH"
        a.route_reasons.append("account is active, so this is a password or sign-in factor issue")
        return a

    jobs = e.get("jobs") or []
    relevant = [j for j in jobs if not c.product or j["target"].lower() == c.product.lower()]
    if c.ticket_type == TicketType.BILLING_QUESTION:
        locked = [j for j in relevant if j.get("invoiced")]
        if locked:
            a.state, a.confidence, a.target = "cycle_locked", "HIGH", locked[0]
            a.evidence.update(product=locked[0]["target"], cycle=locked[0]["cycle"])
            a.route_reasons.append("the charge is in an invoiced cycle; billing must approve any change")
            return a

    open_jobs = [j for j in relevant if j["status"] != "done"]
    if not relevant:
        a.state, a.confidence = "no_request_found", "HIGH"
        a.findings.append({"field": "request", "claimed": c.product or "a request", "actual": "none on record"})
        if c.product:
            a.evidence["product"] = c.product
        return a
    if not open_jobs:
        done = relevant[0]
        a.state, a.confidence, a.target = "all_ok", "HIGH", done
        a.evidence.update(product=done["target"], job_id=done["id"], status=done["status"])
        return a

    if len(open_jobs) > 1:
        a.route_reasons.append(f"{len(open_jobs)} open jobs; acting on the oldest only")
    job = min(open_jobs, key=lambda j: j["created_at"])
    a.target = job
    a.confidence = "HIGH" if len(open_jobs) == 1 else "MEDIUM"
    age = _age_minutes(job["created_at"], now)
    a.evidence.update(product=job["target"], job_id=job["id"], status=job["status"],
                      waiting=f"{int(age // 60)} h {int(age % 60)} min" if age >= 60 else f"{int(age)} min")

    if job["status"] == "failed" and job.get("error") == "SEAT_POOL_EXHAUSTED":
        pool = next((s for s in e.get("seats") or [] if s["product"] == job["target"]), None)
        a.state = "seat_pool_exhausted"
        if pool:
            a.evidence["seats"] = f"{pool['used']} of {pool['total']}"
        a.findings.append({"field": "seat_pool", "claimed": "seat available",
                           "actual": a.evidence.get("seats", "pool full")})
    elif job["status"] == "failed":
        a.state = "job_failed"
        a.findings.append({"field": "job_status", "claimed": "done", "actual": f"failed: {job.get('error')}"})
        a.route_reasons.append(f"job failed with {job.get('error')}; no known fix")
    elif age >= stuck_after_minutes:
        a.state = "job_stuck"
        a.findings.append({"field": "job_status", "claimed": "done", "actual": f"{job['status']} for {a.evidence['waiting']}"})
    else:
        a.state = "job_in_progress"
    return a


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def analyze_sessions(c: Classification, enrichment: dict | None, now: datetime | None = None) -> list[Analysis]:
    """One Analysis per rejected session record, each diagnosed from the merged facts."""
    now = now or datetime.now(timezone.utc)
    e = enrichment or {}
    a, user = _identity(c, e)
    if user is None:
        return [a]
    facts = Facts.from_json(e.get("facts") or {})
    a.evidence.update(person=user["name"], manager=user.get("manager", ""))

    errors = e.get("system_errors") or {}
    if errors:
        a.state = "lookup_failed"
        a.route_reasons.append("could not read " + ", ".join(f"{k} ({v})" for k, v in sorted(errors.items())))
        return [a]
    if facts.conflicts:
        a.state = "systems_disagree"
        a.route_reasons.append("systems disagree: " + "; ".join(
            f"{x['key']} " + " vs ".join(f"{s} says {v}" for s, v in x["values"].items()) for x in facts.conflicts))
        return [a]
    if facts.get("approval_parse_errors"):
        a.state = "approval_unreadable"
        a.route_reasons.append("; ".join(facts.get("approval_parse_errors")))
        return [a]

    approvals = [Approval.from_json(x) for x in facts.get("approvals", [])]
    records = [SessionRecord.from_json(x) for x in facts.get("sessions", [])]
    rejected = sorted((r for r in records if r.status == "rejected"), key=lambda r: r.start)
    log_week = facts.get("log_week_start_day", "Monday")
    if not rejected:
        a.state, a.confidence = "no_rejected_records", "HIGH"
        a.findings.append({"field": "rejected_records", "claimed": "at least one", "actual": "none on record",
                           "system": facts.source("sessions") or "sessions"})
        return [a]
    if not approvals:
        a.state, a.confidence = "no_approval_found", "HIGH"
        a.findings.append({"field": "approval", "claimed": "approved", "actual": "none on record",
                           "system": facts.source("approvals") or "approvals"})
        return [a]

    out: list[Analysis] = []
    for i, rec in enumerate(rejected):
        ra = Analysis(dispatch="record", system="sessions", facts=e.get("facts") or {}, confidence="HIGH")
        if c.addressed_to:
            ra.route_reasons.append(f"addressed to {c.addressed_to}")
        approval = pick_approval(approvals, rec.start.date())
        diagnoses = diagnose(rec, records, approvals, now=now)
        ra.evidence.update(person=user["name"], manager=user.get("manager", ""), resource=rec.resource,
                           record_id=rec.id, session_date=rec.start.date().isoformat(),
                           approval_id=approval.id, approval_window=approval.window,
                           weekly_allowance=approval.weekly_hours_text, week_start_day=approval.week_start_name,
                           hours_logged=f"{hours_in_week(records, rec.username, rec.start.date(), approval.week_start):g}")
        ra.target = {"id": rec.id, "system": "sessions", "username": rec.username,
                     "last_activity": _iso(rec.last_activity), "status": rec.status}
        if diagnoses:
            primary = diagnoses[0]
            ra.state = primary.code
            ra.evidence.update({k: v for k, v in primary.evidence.items()})
            ra.evidence["why"] = primary.detail
            if len(diagnoses) > 1:
                ra.evidence["other_findings"] = "; ".join(d.detail for d in diagnoses[1:])
                ra.route_reasons.append("more than one thing is wrong with " + rec.id + ": "
                                        + ", ".join(d.code for d in diagnoses))
            ra.diagnosis_codes = [d.code for d in diagnoses]
        else:
            ra.state, ra.confidence = "cause_unknown", "LOW"
            ra.route_reasons.append(f"no rule explains why {rec.id} was rejected")
        if i == 0:
            hours = hours_in_week(records, rec.username, rec.start.date(), approval.week_start)
            ra.findings = compare_claims(c.claims.model_dump(), approval=approval, hours_logged=hours,
                                         log_week_start=log_week,
                                         approval_source=facts.source("approvals") or "approvals",
                                         log_source=facts.source("sessions") or "sessions")
        out.append(ra)
    return out
