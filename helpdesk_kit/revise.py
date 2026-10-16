"""Revise stage: when the reviewer rejects a reply, fix it and ask again, a bounded number of times.

    review -> FLAG -> revise -> review -> ... (at most MAX_REVISIONS rounds)

* Only a FLAG is revised. ERROR means the review itself did not run; rewriting the reply
  would not help, so it blocks as before.
* A revision is never trusted: it goes back through the full reviewer, deterministic checks
  first. A reply still flagged after the last round goes to a person, with the reviewer's notes.
* `RuleReviser` fixes the mechanical tells offline (dashes, "also", greeting, sentences that
  cite an id or date that no fact supports). `ClaudeReviser` asks the model to fix what the
  reviewer objected to and to add no new facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .reply import GREETING, ReviewOutcome, ungrounded

MAX_REVISIONS = 2


@dataclass
class RevisionResult:
    reply: str
    outcome: ReviewOutcome
    rounds: list[dict] = field(default_factory=list)      # one entry per revision attempted


class RuleReviser:
    def revise(self, reply: str, outcome: ReviewOutcome, facts: dict) -> str | None:
        text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", reply)
        text = re.sub(r",?\s*\balso\b,?\s*", " ", text, flags=re.I)
        loose = ungrounded(text, facts)
        if loose:
            kept = []
            for line in text.split("\n"):
                sentences = re.split(r"(?<=[.!?])\s+", line)
                kept.append(" ".join(s for s in sentences if not any(t in s.lower() for t in loose)))
            text = "\n".join(kept)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text.startswith(GREETING):
            text = f"{GREETING}\n\n{text}"
        return text if text != reply else None


class ClaudeReviser:
    def __init__(self, model: str, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model

    def revise(self, reply: str, outcome: ReviewOutcome, facts: dict) -> str | None:
        prompt = ("A reviewer rejected this help desk reply. Rewrite it to fix exactly what the reviewer "
                  "objected to. Do not add any fact that is not in the verified facts. Keep it short, open "
                  f"with '{GREETING}', and use no em dashes.\n\nVerified facts:\n"
                  + "\n".join(f"- {k}: {v}" for k, v in facts.items())
                  + f"\n\nReviewer notes: {outcome.notes}\n\nReply:\n{reply}\n\nReturn only the revised reply.")
        try:
            response = self.client.messages.create(model=self.model, max_tokens=2048,
                                                   messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        except Exception:
            return None
        return text or None


def review_and_revise(reviewer, reviser, reply: str, facts: dict, max_rounds: int = MAX_REVISIONS) -> RevisionResult:
    outcome = reviewer.review(reply, facts)
    result = RevisionResult(reply, outcome)
    while outcome.verdict == "FLAG" and len(result.rounds) < max_rounds:
        revised = reviser.revise(result.reply, outcome, facts) if reviser else None
        if not revised or revised == result.reply:
            break
        result.rounds.append({"round": len(result.rounds) + 1, "flagged": outcome.notes})
        result.reply = revised
        outcome = reviewer.review(revised, facts)
        result.outcome = outcome
    return result
