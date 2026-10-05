"""Classify: what kind of ticket is this, who filed it, and who actually has the problem.

The model reads the whole thread plus any screenshot fields and fills a fixed schema. A few
facts are then decided by plain code, never by the model, because they are cheap to get
right and expensive to get wrong:

* Internal (company domain) vs external (contractor, partner) comes from the email domain.
* A manager or colleague often files for someone else ("Dana can't open the finance drive"),
  so the affected person may differ from the requester. Only internal requesters may file
  for a third party; an external requester is always the affected person.
* A name that is abbreviated or a single token ("DaWh", "Dana") cannot be looked up
  reliably. That ticket goes to a human; we do not guess.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field

from . import db


class TicketType(str, Enum):
    LOGIN_FAILED = "LOGIN_FAILED"
    ACCESS_PENDING = "ACCESS_PENDING"          # asked for a group / share / app and it never arrived
    LICENSE_NOT_ASSIGNED = "LICENSE_NOT_ASSIGNED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    HARDWARE = "HARDWARE"
    POLICY_QUESTION = "POLICY_QUESTION"        # "do I still have to...?" dressed up as a bug
    BILLING_QUESTION = "BILLING_QUESTION"
    OTHER = "OTHER"
    AMBIGUOUS = "AMBIGUOUS"


class ModelAnswer(BaseModel):
    """What the model is asked to fill. Nothing here is decided by code."""
    ticket_type: TicketType
    affected_name: str | None = Field(None, description="Full name of the person who has the problem, "
                                                        "if it is someone other than the requester")
    affected_identifier: str | None = Field(None, description="Username or email of the affected person if given")
    product: str | None = Field(None, description="License, group, share or app the ticket is about")
    addressed_to: str | None = Field(None, description="First name if the ticket opens by greeting a specific "
                                                       "staff member (\"Hi Tom,\"), else null")
    summary: str = Field(description="One sentence, in your own words, of what the user needs")


class Classification(ModelAnswer):
    # Decided by code after the model answers, never by the model:
    submitter_kind: str = "external"
    on_behalf: bool = False


SYSTEM = """You triage tickets for an internal IT help desk. Read the entire thread, not just
the first message, and classify from what the user actually describes, never from the
subject line. If the ticket asks whether a rule applies to them rather than reporting
something broken, it is POLICY_QUESTION. If you cannot tell, use AMBIGUOUS."""


def _prompt(ticket: dict) -> str:
    thread = json.loads(ticket["thread_json"])
    screenshot = json.loads(ticket.get("screenshot_json") or "{}")
    parts = [f"Subject: {ticket['subject']}", f"Requester: {ticket['requester_name']} <{ticket['requester_email']}>",
             "Thread:"]
    parts += [f"- {m['author']}{'' if m['public'] else ' (internal note)'}: {m['text']}" for m in thread] \
        or [ticket["body"]]
    if screenshot:
        parts.append("Screenshot fields: " + json.dumps(screenshot))
    return "\n".join(parts)


class ClaudeClassifier:
    def __init__(self, model: str, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model

    def classify(self, ticket: dict) -> Classification:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=8192,
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": _prompt(ticket)}],
            output_format=ModelAnswer,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"classifier returned nothing (stop_reason={response.stop_reason})")
        return Classification(**response.parsed_output.model_dump())


# Offline classifier: ordered keyword rules. Good enough for the demo fixtures and for
# running the whole pipeline without an API key; the model replaces it in real use.
KEYWORDS: list[tuple[TicketType, tuple[str, ...]]] = [
    (TicketType.POLICY_QUESTION, ("do i still need", "do i have to", "is it required", "am i supposed to")),
    (TicketType.HARDWARE, ("printer", "monitor", "keyboard", "laptop won't", "docking", "toner")),
    (TicketType.BILLING_QUESTION, ("charged", "invoice", "billed")),
    (TicketType.ACCOUNT_SUSPENDED, ("suspended", "disabled account", "account is locked")),
    (TicketType.LICENSE_NOT_ASSIGNED, ("license", "licence", "seat", "activate", "never showed up", "isn't on my")),
    (TicketType.ACCESS_PENDING, ("access", "shared drive", "share", "group", "permission")),
    (TicketType.LOGIN_FAILED, ("can't log in", "cannot log in", "can't sign in", "password", "login")),
]
PRODUCTS = ("Office Suite", "Design Suite", "Finance Shared Drive", "VPN Users")
NAME = re.compile(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b(?:'s)?\s+(?:can't|cannot|isn't|is not|still|needs|has|never|doesn't)")
ABBREV = re.compile(r"\b(?:for|user)\s+([A-Z][a-z]?[A-Z][a-z]+)\b")
GREETING = re.compile(r"^\s*(?:hi|hello|hey)\s+([A-Z][a-z]+)\b")


class KeywordClassifier:
    def classify(self, ticket: dict) -> Classification:
        thread = json.loads(ticket["thread_json"])
        text = " ".join(m["text"] for m in thread) or ticket["body"]
        screenshot = json.loads(ticket.get("screenshot_json") or "{}")
        low = (text + " " + " ".join(str(v) for v in screenshot.values() if v)).lower()
        ttype = next((t for t, words in KEYWORDS if any(w in low for w in words)), TicketType.AMBIGUOUS)
        name = NAME.search(text) or ABBREV.search(text)
        greet = GREETING.search(ticket["body"])
        return Classification(
            ticket_type=ttype,
            affected_name=name.group(1) if name else None,
            affected_identifier=screenshot.get("username"),
            product=next((p for p in PRODUCTS if p.lower() in low), None),
            addressed_to=greet.group(1) if greet and greet.group(1).lower() not in ("there", "team", "all") else None,
            summary=(ticket["subject"] or text[:80]).strip(),
        )


def looks_abbreviated(name: str | None) -> bool:
    """'DaWh', 'Dana', 'D. W.' -> True. We only look people up by a full first + last name."""
    if not name:
        return False
    parts = name.replace(".", " ").split()
    return len(parts) < 2 or any(len(p) < 2 for p in parts) or bool(re.search(r"[a-z][A-Z]", name))


def finalize(c: Classification, ticket: dict, staff_domain: str) -> Classification:
    email = ticket["requester_email"]
    is_staff = email.endswith("@" + staff_domain)
    c.submitter_kind = "internal" if is_staff else "external"
    if not is_staff:
        c.affected_name = None  # an outside requester reports their own problem
    c.on_behalf = bool(is_staff and c.affected_name
                       and c.affected_name.lower() != ticket["requester_name"].lower())
    return c


def run(conn, classifier, ticket: dict, staff_domain: str) -> Classification:
    c = finalize(classifier.classify(ticket), ticket, staff_domain)
    db.upsert(conn, "classifications", {"ticket_id": ticket["id"], "data_json": c.model_dump_json()}, "ticket_id")
    db.log_event(conn, "classify", True, ticket["id"], type=c.ticket_type.value, submitter=c.submitter_kind,
                 on_behalf=c.on_behalf)
    return c
