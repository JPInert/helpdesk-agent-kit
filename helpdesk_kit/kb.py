"""Knowledge base: YAML rules that turn (ticket type, submitter, state) into an action, a
confidence, a routing decision and a reply.

Matching order:
1. Keyword rules first. A ticket with no record behind it ("the 3rd floor printer is
   jammed") is matched on words alone, before any record logic runs.
2. Otherwise the most specific rule wins:
       submitter set + state set   -> 4
       submitter set, any state    -> 3
       any submitter, state set    -> 2
       any submitter, any state    -> 1
       catch-all (any type)        -> 0

Three kinds of reply, enforced when the file loads:
  assert   states a diagnosis. Only allowed on a rule pinned to a state.
  ask      asks the user for what is missing. Catch-alls may ask, never assert.
  handoff  says who will pick it up. States nothing about the record.

Auto-execute needs all three: the rule opts in, the proposal is HIGH confidence, and the
rule has already been confirmed solved at least 5 times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

AUTO_EXECUTE_MIN_SOLVED = 5


@dataclass
class Rule:
    id: str
    kind: str
    action: str = "none"
    ticket_type: str | list[str] | None = None
    submitter: str | None = None
    state: str | None = None
    keywords: list[str] = field(default_factory=list)
    route_to_human: bool = True
    confidence: str | None = None
    auto_execute: bool = False
    cc: list[str] = field(default_factory=list)
    reply: str = ""
    needs: list[str] = field(default_factory=list)

    @property
    def specificity(self) -> int:
        if self.ticket_type in (None, "*"):
            return 0
        return 1 + (2 if self.submitter else 0) + (1 if self.state else 0)


@dataclass
class KB:
    rules: list[Rule]
    contacts: dict[str, str]

    def match(self, text: str, ticket_type: str, submitter: str, state: str | None) -> Rule | None:
        low = text.lower()
        for r in self.rules:
            if r.keywords and any(k.lower() in low for k in r.keywords):
                return r
        candidates = [r for r in self.rules if not r.keywords
                      and (r.ticket_type in (None, "*") or ticket_type in _as_list(r.ticket_type))
                      and r.submitter in (None, submitter)
                      and r.state in (None, state)]
        return max(candidates, key=lambda r: r.specificity, default=None)


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def should_auto_execute(rule: Rule, confidence: str, times_solved: int) -> bool:
    return bool(rule.auto_execute and confidence == "HIGH" and times_solved >= AUTO_EXECUTE_MIN_SOLVED)


def load(path: Path) -> KB:
    raw = yaml.safe_load(Path(path).read_text())
    rules = [Rule(**r) for r in raw["rules"]]
    seen: set[str] = set()
    for r in rules:
        if r.id in seen:
            raise ValueError(f"duplicate rule id {r.id}")
        seen.add(r.id)
        if r.kind not in ("assert", "ask", "handoff"):
            raise ValueError(f"{r.id}: kind must be assert, ask or handoff")
        if r.kind == "assert" and not r.state:
            raise ValueError(f"{r.id}: a rule that matches any state may ask, never assert")
        if r.auto_execute and r.action == "none":
            raise ValueError(f"{r.id}: auto_execute with no action")
        for role in r.cc:
            if role not in raw.get("contacts", {}):
                raise ValueError(f"{r.id}: cc role '{role}' has no contact")
    return KB(rules=rules, contacts=raw.get("contacts", {}))
