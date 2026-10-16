"""Fix safety: the single place that decides whether the agent may change something.

Every write to a back-office system goes through here. Nothing upstream can talk its way
past it: a rule, a confidence score, a dashboard click or a CLI flag can each only add a
"yes" on top of checks that are never skipped.

Hard guards (apply even with a person's approval):
  1. Nothing routed to a human is ever executed, whatever the rule or the model said.
  2. The diagnosis must be on the allowlist, and the rule's action must be the one the
     allowlist names for that diagnosis. Editing the YAML can not make a new kind of fix
     automatic.

Track-record gate (needed to run WITHOUT a person's approval):
  3. The rule opts in (`auto_execute`), confidence is HIGH, and the rule has been confirmed
     solved at least AUTO_EXECUTE_MIN_SOLVED times.

Two more rules live in the database, not here, because a prompt can not enforce them:
  * `fix_ledger`: a record is fixed at most once, ever (primary key + append-only triggers).
  * `attempts`: one try per ticket per record.
Rows that are refused are written to `row_skips` with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from .kb import AUTO_EXECUTE_MIN_SOLVED, Rule

# diagnosis state -> the one action that may be run for it
FIX_ALLOWLIST: dict[str, str] = {
    "job_stuck": "retry_job",
    "record_never_ended": "close_session",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def hard_guards(rule: Rule | None, state: str | None, route_reasons=()) -> Decision:
    if route_reasons:
        return Decision(False, "routed to a person: " + "; ".join(route_reasons))
    if rule is None:
        return Decision(False, "no rule matched")
    if rule.route_to_human:
        return Decision(False, f"rule {rule.id} always goes to a person")
    if rule.action == "none":
        return Decision(False, f"rule {rule.id} has no action")
    if state not in FIX_ALLOWLIST:
        return Decision(False, f"diagnosis {state} is not on the auto-fix allowlist")
    if FIX_ALLOWLIST[state] != rule.action:
        return Decision(False, f"action {rule.action} is not the allowlisted fix for {state}")
    return Decision(True)


def may_execute_with_approval(rule: Rule | None, state: str | None, route_reasons=()) -> Decision:
    """Can a person's go-ahead make this fix run? Only if the hard guards hold."""
    return hard_guards(rule, state, route_reasons)


def should_auto_execute(rule: Rule | None, confidence: str, times_solved: int, *, state: str | None,
                        route_reasons=()) -> Decision:
    """The choke point. True only with no person involved at all."""
    d = hard_guards(rule, state, route_reasons)
    if not d:
        return d
    if not rule.auto_execute:
        return Decision(False, f"rule {rule.id} has not opted in to auto-execute")
    if confidence != "HIGH":
        return Decision(False, f"confidence is {confidence}, not HIGH")
    if times_solved < AUTO_EXECUTE_MIN_SOLVED:
        return Decision(False, f"track record {times_solved}/{AUTO_EXECUTE_MIN_SOLVED} confirmed solves")
    return Decision(True)
