"""Enrich: look the affected person up in the portal. No model, no judgement, just data.

Always fetch the current AND the previous billing cycle: people usually write in about
something from the cycle that just closed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db
from .classify import Classification, looks_abbreviated


def _cycles(now: datetime) -> set[str]:
    first = now.replace(day=1)
    prev = (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))
    return {now.strftime("%Y-%m"), prev.strftime("%Y-%m")}


def run(conn, portal, ticket: dict, c: Classification, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result: dict = {"lookup": None, "user": None, "candidates": [], "jobs": [], "seats": [], "error": None}

    if c.on_behalf:
        query, result["lookup"] = c.affected_name, "affected_name"
    elif c.affected_identifier:
        query, result["lookup"] = c.affected_identifier, "identifier"
    else:
        query, result["lookup"] = ticket["requester_email"], "requester_email"

    if c.on_behalf and looks_abbreviated(c.affected_name):
        result["error"] = "abbreviated_name"
    else:
        try:
            matches = portal.find_users(query)
            result["candidates"] = [{"id": u["id"], "name": u["name"]} for u in matches]
            if len(matches) == 1:
                user = matches[0]
                cycles = _cycles(now)
                result["user"] = user
                result["jobs"] = [j for j in portal.jobs(user["id"]) if j["cycle"] in cycles]
                result["seats"] = portal.seats()
        except Exception as e:  # enrichment failure is not fatal; analyze routes it to a human
            result["error"] = f"portal: {type(e).__name__}"

    db.upsert(conn, "enrichments", {"ticket_id": ticket["id"], "data_json": db.dumps(result)}, "ticket_id")
    db.log_event(conn, "enrich", result["error"] is None, ticket["id"], lookup=result["lookup"],
                 candidates=len(result["candidates"]), jobs=len(result["jobs"]), error=result["error"])
    return result
