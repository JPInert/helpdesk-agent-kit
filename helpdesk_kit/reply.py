"""Compose the reply, then have a second model check it before anything is sent.

Evidence gate: every ${placeholder} in a reply template must be filled from evidence the run
actually collected. A missing value does not render as blank; it stops the reply. That is
how "never state something the run did not check" is enforced in code: e.g. the
"I've re-run your request and it went through" reply needs `new_status`, which only exists
after the portal confirmed the fix.

Review verdicts are PASS, FLAG and ERROR. Anything but PASS blocks the send. ERROR (the
review did not run: timeout, refusal, unparseable answer) is kept separate from FLAG (the
review ran and objected) because they mean different things to the person reading the log,
even though both block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from string import Template
from typing import Literal

from pydantic import BaseModel, Field

PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


class MissingEvidence(ValueError):
    pass


def render(template: str, evidence: dict, needs: list[str] = ()) -> str:
    wanted = set(PLACEHOLDER.findall(template)) | set(needs)
    missing = sorted(k for k in wanted if evidence.get(k) in (None, ""))
    if missing:
        raise MissingEvidence(f"reply needs evidence the run does not have: {', '.join(missing)}")
    return Template(template).substitute({k: str(v) for k, v in evidence.items()}).strip()


def lint(text: str) -> list[str]:
    """House style for anything sent to a user. Cheap, deterministic, runs before the model."""
    problems = []
    if not text.startswith("Hi there,"):
        problems.append("must open with 'Hi there,'")
    if "—" in text or "–" in text:
        problems.append("contains an em/en dash (reads as machine-written)")
    if re.search(r"\balso\b", text, re.I):
        problems.append("contains 'also' (reads as machine-written)")
    if "${" in text:
        problems.append("unrendered placeholder")
    if len(text) < 40:
        problems.append("too short to be a real reply")
    return problems


@dataclass
class ReviewOutcome:
    verdict: Literal["PASS", "FLAG", "ERROR"]
    notes: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def blocks_send(self) -> bool:
        return self.verdict != "PASS"


class _ReviewAnswer(BaseModel):
    verdict: Literal["PASS", "FLAG"]
    categories: list[Literal["unsupported_claim", "wrong_person", "tone", "missing_next_step", "other"]] = \
        Field(default_factory=list)
    notes: str = ""


REVIEW_PROMPT = """You are checking a help desk reply before it is sent. You get the reply and
the facts the system verified. FLAG it if the reply states anything the facts do not
support, addresses the wrong person, promises something nobody verified, or leaves the user
without a clear next step. Otherwise PASS. Be strict about unsupported claims.

Verified facts:
{facts}

Reply:
{reply}"""

_RECIPIENT_KEYS = re.compile(r"email|cc|recipient|address", re.I)


def scrub(facts: dict) -> dict:
    """The reviewer judges the words, not who receives them: drop every recipient field."""
    return {k: v for k, v in facts.items() if not _RECIPIENT_KEYS.search(k)}


class LintReviewer:
    """Offline reviewer: the lint rules only."""

    def review(self, reply: str, facts: dict) -> ReviewOutcome:
        problems = lint(reply)
        return ReviewOutcome("FLAG", "; ".join(problems), ["other"]) if problems else ReviewOutcome("PASS")


class ClaudeReviewer:
    def __init__(self, model: str, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model

    def review(self, reply: str, facts: dict) -> ReviewOutcome:
        problems = lint(reply)
        if problems:
            return ReviewOutcome("FLAG", "; ".join(problems), ["other"])
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=8192,
                messages=[{"role": "user", "content": REVIEW_PROMPT.format(
                    facts="\n".join(f"- {k}: {v}" for k, v in scrub(facts).items()), reply=reply)}],
                output_format=_ReviewAnswer,
            )
        except Exception as e:
            return ReviewOutcome("ERROR", f"review call failed: {type(e).__name__}")
        answer = response.parsed_output
        if response.stop_reason == "refusal" or answer is None:
            return ReviewOutcome("ERROR", f"no usable verdict (stop_reason={response.stop_reason})")
        return ReviewOutcome(answer.verdict, answer.notes, list(answer.categories))
