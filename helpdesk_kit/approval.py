"""Parse an approval record and compare what a ticket claims with what the systems say.

An approval grants one person temporary elevated access to a list of resources. The approvals
system hands it over as a holder name, a free-text summary and a free-text resource list, in
whatever shape the person who typed it preferred:

    "Approved 2026-09-14 to 2026-10-31, up to 10 hours per week. Week starts Monday."
    "Valid 21 Sep 2026 - 25 Oct 2026 | max 10 hrs/week | weeks run Sunday to Saturday"

`parse_approval` turns either into a typed `Approval`: date range, weekly allowance in
minutes, the day the week starts on, and the resource names, cleaned of invisible Unicode so
they can be matched. Anything it cannot read raises `ApprovalParseError` naming the field;
the caller sends that ticket to a person rather than guessing.

All dates are calendar dates in UTC; a session counts toward the date it starts on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .textclean import clean_text, name_key

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_ISO = r"\d{4}-\d{2}-\d{2}"
_DMY = r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
_MDY = r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
DATE_TOKEN = re.compile(f"({_ISO})|({_DMY})|({_MDY})")
_WEEKLY = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b\s*(?:/|per|a|each)\s*(?:week|wk)\b", re.I)
_WEEK_START = re.compile(
    r"\bweeks?\s+(?:starts?|begins?|run|runs|start\s+on|begin\s+on|starting)\s*(?:on|from|:)?\s*([A-Za-z]+)", re.I)


class ApprovalParseError(ValueError):
    pass


@dataclass(frozen=True)
class Approval:
    id: str
    holder: str
    holder_key: str
    start: date
    end: date
    weekly_minutes: int
    week_start: int                 # 0 = Monday ... 6 = Sunday
    resources: tuple[str, ...]
    resource_keys: frozenset[str]

    @property
    def week_start_name(self) -> str:
        return DAY_NAMES[self.week_start]

    @property
    def window(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    @property
    def weekly_hours_text(self) -> str:
        hours = self.weekly_minutes / 60
        return f"{hours:g} hours per week"

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    def to_json(self) -> dict:
        return {"id": self.id, "holder": self.holder, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "weekly_minutes": self.weekly_minutes,
                "week_start": self.week_start, "resources": list(self.resources)}

    @classmethod
    def from_json(cls, d: dict) -> "Approval":
        return cls(id=d["id"], holder=d["holder"], holder_key=name_key(d["holder"]),
                   start=date.fromisoformat(d["start"]), end=date.fromisoformat(d["end"]),
                   weekly_minutes=int(d["weekly_minutes"]), week_start=int(d["week_start"]),
                   resources=tuple(d["resources"]), resource_keys=frozenset(name_key(t) for t in d["resources"]))


def _to_date(token: str) -> date:
    token = token.strip()
    if re.fullmatch(_ISO, token):
        return date.fromisoformat(token)
    words = re.findall(r"[A-Za-z]+|\d+", token)
    nums = [w for w in words if w.isdigit()]
    names = [w for w in words if w.isalpha()]
    month = MONTHS.get(names[0][:3].lower()) if names else None
    if month is None or len(nums) != 2:
        raise ApprovalParseError(f"unreadable date '{token}'")
    day, year = int(nums[0]), int(nums[1])   # "28 Sep 2026" and "Sep 28, 2026" both read day then year
    return date(year, month, day)


def parse_date_range(text: str) -> tuple[date, date]:
    tokens = [next(g for g in m.groups() if g) for m in DATE_TOKEN.finditer(clean_text(text))]
    if len(tokens) != 2:
        raise ApprovalParseError(f"expected a start and an end date, found {len(tokens)}")
    try:
        start, end = _to_date(tokens[0]), _to_date(tokens[1])
    except ValueError as e:
        raise ApprovalParseError(f"unreadable date: {e}") from None
    if end < start:
        raise ApprovalParseError("the end date is before the start date")
    return start, end


def parse_weekly_allowance(text: str) -> int:
    """Weekly allowance in minutes."""
    m = _WEEKLY.search(clean_text(text))
    if not m:
        raise ApprovalParseError("no weekly allowance found")
    amount = float(m.group(1))
    return round(amount * (60 if m.group(2).lower().startswith("h") else 1))


def parse_week_start(text: str) -> int:
    m = _WEEK_START.search(clean_text(text))
    day = DAYS.get(m.group(1)[:3].lower()) if m else None
    if day is None:
        raise ApprovalParseError("no 'week starts on' day found")
    return day


def parse_resources(text: str) -> tuple[str, ...]:
    parts = re.split(r"[;\n,|]+", clean_text(text, multiline=True))
    return tuple(p.strip() for p in parts if p.strip())


def parse_approval(record: dict) -> Approval:
    """`record` is what the approvals system returns: id, holder, summary, resources."""
    try:
        summary = record["summary"]
        start, end = parse_date_range(summary)
        return Approval(
            id=str(record["id"]), holder=clean_text(record["holder"]), holder_key=name_key(record["holder"]),
            start=start, end=end, weekly_minutes=parse_weekly_allowance(summary),
            week_start=parse_week_start(summary), resources=(t := parse_resources(record.get("resources", ""))),
            resource_keys=frozenset(name_key(x) for x in t))
    except KeyError as e:
        raise ApprovalParseError(f"approval {record.get('id', '?')} has no {e.args[0]} field") from None
    except ApprovalParseError as e:
        raise ApprovalParseError(f"approval {record.get('id', '?')}: {e}") from None


# ---- ticket claim vs system record -----------------------------------------------------------

def compare_claims(claims: dict, *, approval: Approval | None, hours_logged: float | None,
                   log_week_start: str | None, approval_source: str = "approvals",
                   log_source: str = "sessions") -> list[dict]:
    """Field-level findings: the ticket says X, the system says Y. A claim the ticket does
    not make, or one the system agrees with, produces nothing."""
    findings: list[dict] = []

    def add(field: str, claimed, actual, system: str) -> None:
        findings.append({"field": field, "claimed": str(claimed), "actual": str(actual), "system": system})

    if approval:
        if claims.get("approved_until") and claims["approved_until"] != approval.end.isoformat():
            add("approval_end_date", claims["approved_until"], approval.end.isoformat(), approval_source)
        if claims.get("weekly_hours") is not None and round(claims["weekly_hours"] * 60) != approval.weekly_minutes:
            add("weekly_allowance_hours", f"{claims['weekly_hours']:g}", f"{approval.weekly_minutes / 60:g}",
                approval_source)
        if claims.get("week_starts") and claims["week_starts"].lower()[:3] != DAY_NAMES[approval.week_start][:3].lower():
            add("week_start_day", claims["week_starts"], approval.week_start_name, approval_source)
    if claims.get("hours_used") is not None and hours_logged is not None and abs(claims["hours_used"] - hours_logged) >= 0.25:
        add("hours_logged_in_week", f"{claims['hours_used']:g}", f"{hours_logged:g}", log_source)
    if claims.get("week_starts") and log_week_start and claims["week_starts"].lower()[:3] != log_week_start.lower()[:3]:
        add("week_start_day", claims["week_starts"], log_week_start, log_source)
    return findings
