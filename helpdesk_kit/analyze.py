"""Analyze: compare what the ticket says with what the portal shows. Pure and deterministic:
no model, no knowledge-base lookup, no I/O. Same inputs, same answer, easy to test.

It branches on what the DATA looks like (no such person, two candidates, a stuck job, an
empty seat pool...), never on the ticket-type label, because the label is a model's guess and
the data is not. The type label is only used later, to pick the reply.

Output: a state name, a confidence, the reasons a human is needed (if any), the findings
(claim vs record), and the evidence a reply is allowed to cite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .classify import Classification, TicketType

RECORD_TYPES = {TicketType.LOGIN_FAILED, TicketType.ACCESS_PENDING, TicketType.LICENSE_NOT_ASSIGNED,
                TicketType.ACCOUNT_SUSPENDED, TicketType.BILLING_QUESTION}


@dataclass
class Analysis:
    dispatch: str
    state: str | None = None
    confidence: str = "LOW"
    route_reasons: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    target: dict | None = None      # the record a fix would act on


def needs_record(c: Classification) -> bool:
    return c.ticket_type in RECORD_TYPES


def _age_minutes(created_at: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds() / 60


def analyze(c: Classification, enrichment: dict | None, now: datetime | None = None,
            stuck_after_minutes: int = 30) -> Analysis:
    now = now or datetime.now(timezone.utc)
    if not needs_record(c):
        a = Analysis(dispatch="non_record", confidence="HIGH")
        if c.addressed_to:
            a.route_reasons.append(f"addressed to {c.addressed_to}")
        return a

    e = enrichment or {}
    a = Analysis(dispatch="record")
    if c.addressed_to:
        a.route_reasons.append(f"addressed to {c.addressed_to}")

    if e.get("error") == "abbreviated_name":
        a.state = "unresolvable_person"
        a.route_reasons.append(f"name '{c.affected_name}' is abbreviated; not guessing who it is")
        return a
    if e.get("error"):
        a.state = "lookup_failed"
        a.route_reasons.append(f"portal lookup failed ({e['error']})")
        return a
    if len(e.get("candidates") or []) > 1:
        a.state = "ambiguous_person"
        a.route_reasons.append("more than one account matches: "
                               + ", ".join(x["name"] for x in e["candidates"]))
        return a
    user = e.get("user")
    if not user:
        a.state = "user_not_found"
        a.route_reasons.append("no portal account matches the person on the ticket")
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
