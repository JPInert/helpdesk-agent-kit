"""Every deterministic diagnosis rule, in isolation (inventory item 3). No model, no I/O."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from helpdesk_kit.approval import parse_approval
from helpdesk_kit.diagnose import (SessionRecord, diagnose, hours_in_week, is_only_never_ended, pick_approval,
                                   week_bucket, week_minutes)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def dt(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"2026-{day}T{hm}:00+00:00")


def rec(sid, day, start, end, *, user="pat", resource="Build Server", status="valid", last=None) -> SessionRecord:
    return SessionRecord(sid, user, resource, dt(day, start), dt(day, end) if end else None,
                         dt(day, last) if last else (dt(day, end) if end else None), status)


def appr(summary="2026-09-14 to 2026-10-31, 10 hours per week, week starts Monday", resources="Build Server; Docs"):
    return parse_approval({"id": "ap-1", "holder": "Pat Doe", "summary": summary, "resources": resources})


def codes(record, records, approvals, **kw):
    return [d.code for d in diagnose(record, records, approvals, now=NOW, **kw)]


def test_a_clean_record_has_no_diagnosis():
    r = rec("s1", "09-29", "09:00", "11:00", status="rejected")
    assert codes(r, [r], [appr()]) == []


def test_outside_approval_dates_after_the_end():
    r = rec("s1", "10-02", "09:00", "11:00")
    d = diagnose(r, [r], [appr("2026-09-14 to 2026-09-30, 10 hours per week, week starts Monday")], now=NOW)
    assert [x.code for x in d] == ["outside_approval_dates"]
    assert d[0].evidence["approval_window"] == "2026-09-14 to 2026-09-30" and "2026-10-02" in d[0].detail


def test_outside_approval_dates_before_the_start():
    r = rec("s1", "09-10", "09:00", "11:00")
    assert codes(r, [r], [appr()]) == ["outside_approval_dates"]


def test_the_boundary_days_are_inside_the_approval():
    a = appr("2026-09-14 to 2026-09-30, 10 hours per week, week starts Monday")
    assert codes(rec("s1", "09-14", "09:00", "10:00"), [], [a]) == []
    assert codes(rec("s2", "09-30", "09:00", "10:00"), [], [a]) == []


def test_resource_not_approved_compares_cleaned_names():
    r = rec("s1", "09-29", "09:00", "10:00", resource="Finance Console")
    assert codes(r, [r], [appr()]) == ["resource_not_approved"]
    ok = rec("s2", "09-29", "09:00", "10:00", resource="build\u00a0server")
    assert codes(ok, [ok], [appr()]) == []


def test_overlapping_sessions():
    a, b = rec("s1", "10-01", "10:00", "12:00"), rec("s2", "10-01", "11:00", "13:00")
    assert codes(a, [a, b], [appr()]) == ["overlapping_sessions"] and codes(b, [a, b], [appr()]) == ["overlapping_sessions"]


def test_back_to_back_sessions_do_not_overlap():
    a, b = rec("s1", "10-01", "10:00", "12:00"), rec("s2", "10-01", "12:00", "13:00")
    assert codes(b, [a, b], [appr()]) == []


def test_other_peoples_sessions_never_overlap_mine():
    a, b = rec("s1", "10-01", "10:00", "12:00"), rec("s2", "10-01", "11:00", "13:00", user="someone-else")
    assert codes(a, [a, b], [appr()]) == []


def test_daily_max_exceeded():
    a, b = rec("s1", "10-01", "00:00", "07:00"), rec("s2", "10-01", "07:00", "14:00")      # 14 hours on one day
    got = diagnose(b, [a, b], [appr("2026-09-14 to 2026-10-31, 40 hours per week, week starts Monday")], now=NOW)
    assert [d.code for d in got] == ["daily_max_exceeded"] and got[0].evidence["hours_logged"] == "14"


def test_daily_max_can_be_set_by_the_caller():
    a = rec("s1", "10-01", "09:00", "12:00")
    assert codes(a, [a], [appr()], daily_max_minutes=120) == ["daily_max_exceeded"]


def test_weekly_cap_exceeded_counts_from_the_approvals_week_start():
    rows = [rec("s1", "09-28", "09:00", "14:00"), rec("s2", "09-30", "09:00", "13:00"),
            rec("s3", "10-02", "09:00", "12:00", status="rejected")]                      # 5 + 4 + 3 = 12 hours
    got = diagnose(rows[2], rows, [appr()], now=NOW)
    assert [d.code for d in got] == ["weekly_cap_exceeded"]
    assert got[0].evidence["hours_logged"] == "12" and got[0].evidence["week_start"] == "2026-09-28"


def test_total_exactly_at_the_cap_is_allowed():
    rows = [rec("s1", "09-28", "09:00", "14:00"), rec("s2", "09-30", "09:00", "14:00")]   # 10 hours
    assert codes(rows[1], rows, [appr()]) == []


def test_wrong_week_start_cap_holds_on_the_approvals_weeks_but_not_the_logs():
    # Sunday-start approval. Wed 3h + Fri 4h + the next Sunday 4h: 7 and 4 hours on the approval's
    # weeks, 11 hours on the log's Monday-start week.
    rows = [rec("s1", "09-30", "09:00", "12:00"), rec("s2", "10-02", "09:00", "13:00"),
            rec("s3", "10-04", "10:00", "14:00", status="rejected")]
    a = appr("2026-09-14 to 2026-11-01, 10 hours per week, week starts Sunday")
    got = diagnose(rows[2], rows, [a], now=NOW)
    assert [d.code for d in got] == ["wrong_week_start"]
    assert got[0].evidence["hours_logged"] == "11" and got[0].evidence["week_start_day"] == "Sunday"


def test_no_wrong_week_start_when_the_approval_also_counts_from_monday():
    rows = [rec("s1", "09-28", "09:00", "14:00"), rec("s2", "09-30", "09:00", "14:00"), rec("s3", "10-02", "09:00", "10:00")]
    assert [d.code for d in diagnose(rows[2], rows, [appr()], now=NOW)] == ["weekly_cap_exceeded"]     # real overrun


def test_record_never_ended_uses_last_activity_for_hours_and_age():
    r = rec("s1", "09-29", "09:00", None, status="rejected", last="13:30")
    d = diagnose(r, [r], [appr()], now=NOW)
    assert [x.code for x in d] == ["record_never_ended"] and d[0].evidence["last_activity"] == "2026-09-29 13:30 UTC"
    assert r.minutes == 270 and is_only_never_ended(d)


def test_a_recent_open_session_is_not_a_never_ended_record():
    r = SessionRecord("s1", "pat", "Build Server", NOW - timedelta(hours=2), None, NOW - timedelta(minutes=5), "rejected")
    assert codes(r, [r], [appr("2026-09-14 to 2026-10-31, 10 hours per week, week starts Monday")]) == []


def test_never_ended_is_last_in_priority_so_it_is_only_fixable_when_alone():
    r = rec("s1", "10-02", "09:00", None, status="rejected", last="10:00")
    got = diagnose(r, [r], [appr("2026-09-14 to 2026-09-30, 10 hours per week, week starts Monday")], now=NOW)
    assert [d.code for d in got] == ["outside_approval_dates", "record_never_ended"]
    assert not is_only_never_ended(got)


def test_priority_order_when_everything_is_wrong():
    a, b = rec("s1", "10-02", "00:00", "14:00", resource="Finance Console"), rec("s2", "10-02", "10:00", "15:00")
    got = diagnose(a, [a, b], [appr("2026-09-14 to 2026-09-30, 1 hours per week, week starts Monday")], now=NOW)
    assert [d.code for d in got] == ["outside_approval_dates", "resource_not_approved", "overlapping_sessions",
                                     "daily_max_exceeded", "weekly_cap_exceeded"]


def test_with_no_approvals_only_the_record_level_rules_can_fire():
    r = rec("s1", "09-29", "09:00", None, last="10:00")
    assert codes(r, [r], []) == ["record_never_ended"]


def test_pick_approval_prefers_the_one_covering_the_date_else_the_latest():
    early = parse_approval({"id": "ap-1", "holder": "x", "summary": "2026-08-01 to 2026-08-31, 5 hours per week, week starts Monday", "resources": ""})
    late = parse_approval({"id": "ap-2", "holder": "x", "summary": "2026-09-01 to 2026-09-30, 5 hours per week, week starts Monday", "resources": ""})
    assert pick_approval([early, late], date(2026, 8, 15)).id == "ap-1"
    assert pick_approval([early, late], date(2026, 12, 1)).id == "ap-2"
    assert pick_approval([], date(2026, 8, 15)) is None


def test_week_helpers():
    assert week_bucket(date(2026, 10, 4), 0) == date(2026, 9, 28)       # Sunday belongs to the week that began Monday
    assert week_bucket(date(2026, 10, 4), 6) == date(2026, 10, 4)       # ...or starts a new week if weeks begin Sunday
    rows = [rec("s1", "09-28", "09:00", "10:00"), rec("s2", "10-04", "09:00", "11:00"), rec("s3", "10-05", "09:00", "10:00")]
    assert week_minutes(rows, date(2026, 9, 30), 0) == 180 and hours_in_week(rows, "pat", date(2026, 10, 5), 0) == 1
