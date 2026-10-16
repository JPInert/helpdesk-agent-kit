"""Deterministic diagnosis: why was this session record rejected?

The activity log only says a record failed validation. This module works out the reason from
the merged facts: the record, the person's other records, and their parsed approval. No model
is involved; every rule is a plain function with a unit test.

Rules, in priority order (the first match is the primary diagnosis):

  outside_approval_dates  the session started before the approval began or after it ended
  resource_not_approved     the session resource is not on the approval's resource list
  overlapping_sessions    the person has another session running at the same time
  daily_max_exceeded      the day's total is over the daily maximum
  weekly_cap_exceeded     the week's total, counted from the approval's week-start day, is over the cap
  wrong_week_start        the approval's own weeks are within the cap, but the log counts weeks from a
                          different day and that makes the total look over the cap
  record_never_ended      the session has no end time and was last active long ago

`record_never_ended` is last on purpose: it is the only diagnosis the agent may fix on its
own (close the session at its last recorded activity), and only when it is the ONLY thing
wrong with the record. Any other finding means a person decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .approval import Approval
from .textclean import name_key

DAILY_MAX_MINUTES = 720
NEVER_ENDED_AFTER = timedelta(hours=24)
LOG_WEEK_START = 0                      # the activity log counts weeks from Monday

PRIORITY = ["outside_approval_dates", "resource_not_approved", "overlapping_sessions",
            "daily_max_exceeded", "weekly_cap_exceeded", "wrong_week_start", "record_never_ended"]


@dataclass(frozen=True)
class SessionRecord:
    id: str
    username: str
    resource: str
    start: datetime
    end: datetime | None
    last_activity: datetime | None
    status: str

    @classmethod
    def from_json(cls, d: dict) -> "SessionRecord":
        def ts(v):
            return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None
        return cls(id=d["id"], username=d["username"], resource=d["resource"], start=ts(d["start"]),
                   end=ts(d.get("end")), last_activity=ts(d.get("last_activity")), status=d["status"])

    @property
    def effective_end(self) -> datetime:
        """An open session counts up to its last recorded activity."""
        return self.end or self.last_activity or self.start

    @property
    def minutes(self) -> int:
        return max(0, int((self.effective_end - self.start).total_seconds() // 60))


@dataclass
class Diagnosis:
    code: str
    record_id: str
    detail: str
    evidence: dict = field(default_factory=dict)


def week_bucket(d: date, week_start: int) -> date:
    return d - timedelta(days=(d.weekday() - week_start) % 7)


def week_minutes(records: list[SessionRecord], d: date, week_start: int) -> int:
    bucket = week_bucket(d, week_start)
    return sum(r.minutes for r in records if week_bucket(r.start.date(), week_start) == bucket)


def pick_approval(approvals: list[Approval], d: date) -> Approval | None:
    """The approval that covers the date, else the one that ended last (so findings can say
    how far outside it the record is)."""
    covering = [a for a in approvals if a.covers(d)]
    if covering:
        return max(covering, key=lambda a: a.end)
    return max(approvals, key=lambda a: a.end, default=None)


def _hours(minutes: int) -> str:
    return f"{minutes / 60:.1f}".rstrip("0").rstrip(".")


def diagnose(record: SessionRecord, records: list[SessionRecord], approvals: list[Approval], *,
             now: datetime | None = None, log_week_start: int = LOG_WEEK_START,
             daily_max_minutes: int = DAILY_MAX_MINUTES) -> list[Diagnosis]:
    """Every rule that matches `record`, most important first. Empty if nothing is wrong
    that this module can see. `approvals` must be the person's own; empty means the caller
    already knows there is no approval."""
    now = now or datetime.now(timezone.utc)
    mine = [r for r in records if r.username == record.username]
    approval = pick_approval(approvals, record.start.date())
    found: dict[str, Diagnosis] = {}
    day = record.start.date()

    def add(code: str, detail: str, **evidence) -> None:
        found[code] = Diagnosis(code, record.id, detail, evidence)

    if approval and not approval.covers(day):
        add("outside_approval_dates",
            f"the session started on {day.isoformat()}, outside approval {approval.id} ({approval.window})",
            approval_id=approval.id, approval_window=approval.window, session_date=day.isoformat())

    if approval and approval.resource_keys and name_key(record.resource) not in approval.resource_keys:
        add("resource_not_approved",
            f"{record.resource} is not on approval {approval.id} ({', '.join(approval.resources)})",
            approval_id=approval.id, approved_resources=", ".join(approval.resources))

    overlaps = [r for r in mine if r.id != record.id and r.start < record.effective_end
                and record.start < r.effective_end]
    if overlaps:
        add("overlapping_sessions", f"overlaps session {overlaps[0].id}", overlapping_id=overlaps[0].id)

    day_total = sum(r.minutes for r in mine if r.start.date() == day)
    if day_total > daily_max_minutes:
        add("daily_max_exceeded", f"{_hours(day_total)} hours on {day.isoformat()} against a daily maximum of "
            f"{_hours(daily_max_minutes)}", hours_logged=_hours(day_total), session_date=day.isoformat())

    if approval:
        total = week_minutes(mine, day, approval.week_start)
        bucket = week_bucket(day, approval.week_start)
        if total > approval.weekly_minutes:
            add("weekly_cap_exceeded",
                f"{_hours(total)} hours in the week starting {bucket.isoformat()} against an allowance of "
                f"{approval.weekly_hours_text}", hours_logged=_hours(total), week_start=bucket.isoformat(),
                approval_id=approval.id, weekly_allowance=approval.weekly_hours_text)
        elif log_week_start != approval.week_start:
            log_total = week_minutes(mine, day, log_week_start)
            if log_total > approval.weekly_minutes:
                add("wrong_week_start",
                    f"approval {approval.id} counts weeks from {approval.week_start_name} "
                    f"({_hours(total)} hours, within {approval.weekly_hours_text}) but the log counts from "
                    f"Monday ({_hours(log_total)} hours)",
                    hours_logged=_hours(log_total), approval_id=approval.id,
                    weekly_allowance=approval.weekly_hours_text, week_start_day=approval.week_start_name)

    if record.end is None and now - (record.last_activity or record.start) >= NEVER_ENDED_AFTER:
        add("record_never_ended",
            "the session has no end time"
            + (f"; last activity {record.last_activity:%Y-%m-%d %H:%M} UTC" if record.last_activity else ""),
            last_activity=f"{record.last_activity:%Y-%m-%d %H:%M} UTC" if record.last_activity else "")

    return [found[c] for c in PRIORITY if c in found]


def is_only_never_ended(diagnoses: list[Diagnosis]) -> bool:
    """The one case safe to fix by closing the record."""
    return [d.code for d in diagnoses] == ["record_never_ended"]


def hours_in_week(records: list[SessionRecord], username: str, d: date, week_start: int) -> float:
    return week_minutes([r for r in records if r.username == username], d, week_start) / 60
