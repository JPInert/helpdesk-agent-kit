"""Enrich: look the affected person up in every back-office system that serves this issue.
No model, no judgement, just data.

1. Identity. The provisioning system turns the name, username or email into one account (or
   none, or several; analyze decides what that means).
2. Collect. Every adapter that serves the issue's ticket type reads what it knows. Each
   adapter owns its own login and session handling; a system that fails is recorded under
   `system_errors` and does not stop the others.
3. Merge. Everything lands in one `Facts` object with per-fact provenance (which system
   said it). The result keeps the dict shape analyze already reads, plus `facts`.

Always fetch the current AND the previous billing cycle: people usually write in about
something from the cycle that just closed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db
from .classify import Classification, looks_abbreviated
from .facts import Facts
from .systems import AdapterError, Subject, SystemSet


def _cycles(now: datetime) -> set[str]:
    first = now.replace(day=1)
    prev = (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))
    return {now.strftime("%Y-%m"), prev.strftime("%Y-%m")}


def run(conn, systems: SystemSet, ticket: dict, c: Classification, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result: dict = {"lookup": None, "user": None, "candidates": [], "jobs": [], "seats": [], "error": None,
                    "system_errors": {}, "facts": {}, "ticket_type": c.ticket_type.value}
    facts = Facts()

    if c.on_behalf:
        query, result["lookup"] = c.affected_name, "affected_name"
    elif c.affected_identifier:
        query, result["lookup"] = c.affected_identifier, "identifier"
    else:
        query, result["lookup"] = ticket["requester_email"], "requester_email"

    prov = systems.get("provisioning")
    subject = Subject(name=c.affected_name or ticket["requester_name"],
                      username=c.affected_identifier or "", email=ticket["requester_email"])
    if c.on_behalf and looks_abbreviated(c.affected_name):
        result["error"] = "abbreviated_name"
    elif prov is None:
        result["error"] = "no identity system registered"
    else:
        try:
            matches = prov.find_users(query)
            result["candidates"] = [{"id": u["id"], "name": u["name"]} for u in matches]
            if len(matches) == 1:
                user = matches[0]
                result["user"] = user
                subject = Subject(name=user["name"], username=user.get("username", ""), email=user.get("email", ""),
                                  ids={"provisioning": user["id"]})
                facts.update({"person.name": user["name"], "person.username": user.get("username", ""),
                              "person.status": user["status"], "person.manager": user.get("manager", "")},
                             "provisioning")
        except Exception as e:  # enrichment failure is not fatal; analyze routes it to a human
            result["error"] = f"portal: {type(e).__name__}"

    if result["user"]:
        for name in systems.serving(c.ticket_type.value):
            adapter = systems.get(name)
            try:
                facts.merge(adapter.collect(subject))
            except AdapterError as e:
                result["system_errors"][name] = str(e)
            except Exception as e:
                result["system_errors"][name] = f"{type(e).__name__}"
        cycles = _cycles(now)
        result["jobs"] = [j for j in facts.get("jobs", []) if j["cycle"] in cycles]
        result["seats"] = facts.get("seats", [])

    result["facts"] = facts.to_json()
    db.log_event(conn, "enrich", result["error"] is None and not result["system_errors"], ticket["id"],
                 lookup=result["lookup"], candidates=len(result["candidates"]), jobs=len(result["jobs"]),
                 error=result["error"], systems=sorted(set(facts.provenance().values())),
                 system_errors=sorted(result["system_errors"]))
    return result
