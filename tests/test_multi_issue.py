"""Several issues in one ticket (inventory item 4): classification, analysis and the session analysis."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from helpdesk_kit.analyze import analyze_sessions
from helpdesk_kit.approval import parse_approval
from helpdesk_kit.classify import (Claims, Classification, ExtraIssue, KeywordClassifier, TicketType, extract_claims,
                                   split_issues)
from helpdesk_kit.facts import Facts

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def ticket(body, subject="s"):
    return {"subject": subject, "body": body, "screenshot_json": "{}",
            "thread_json": json.dumps([{"author": "x", "public": True, "text": body}])}


def test_a_numbered_list_is_several_issues_and_a_paragraph_is_one():
    assert split_issues("Two things:\n1. first\n2. second") == ["first", "second"]
    assert split_issues("- a\n- b\n- c") == ["a", "b", "c"]
    assert split_issues("1. only one item") == ["1. only one item"]
    assert split_issues("one plain paragraph. with 2. numbers inside") == ["one plain paragraph. with 2. numbers inside"]


def test_each_issue_in_a_ticket_gets_its_own_type_and_product():
    c = KeywordClassifier().classify(ticket("Hi there, two problems:\n1. My Office Suite license is still not showing up.\n"
                                            "2. My session from Monday was rejected."))
    issues = c.issues()
    assert [i.ticket_type for i in issues] == [TicketType.LICENSE_NOT_ASSIGNED, TicketType.SESSION_REJECTED]
    assert issues[0].product == "Office Suite" and issues[1].product is None
    assert issues[0].extra_issues == [] and issues[1].extra_issues == []        # each is a complete single-issue view


def test_a_single_issue_ticket_is_one_issue_and_unchanged():
    c = KeywordClassifier().classify(ticket("The 3rd floor printer is jammed"))
    assert [i.ticket_type for i in c.issues()] == [TicketType.HARDWARE] and c.extra_issues == []


def test_issues_share_the_person_and_submitter():
    c = Classification(ticket_type=TicketType.ACCESS_PENDING, affected_name="Dana Whitfield", summary="a",
                       submitter_kind="internal", on_behalf=True,
                       extra_issues=[ExtraIssue(ticket_type=TicketType.SESSION_REJECTED, summary="b")])
    assert all((i.affected_name, i.submitter_kind, i.on_behalf) == ("Dana Whitfield", "internal", True) for i in c.issues())
    assert c.issues()[1].summary == "b"


def test_claims_are_pulled_out_of_the_words_of_each_issue():
    c = extract_claims("My approval runs through 2026-10-31 for 12 hours a week. I only logged about 8 hours. "
                       "Weeks start on Sunday.")
    assert c == Claims(approved_until="2026-10-31", weekly_hours=12.0, hours_used=8.0, week_starts="Sunday")
    assert extract_claims("nothing to claim here") == Claims()


def test_claims_belong_to_their_own_issue():
    c = KeywordClassifier().classify(ticket("Hi there:\n1. My session was rejected, approval runs through 2026-10-31.\n"
                                            "2. Another session was rejected, I only logged 3 hours."))
    first, second = c.issues()
    assert first.claims.approved_until == "2026-10-31" and first.claims.hours_used is None
    assert second.claims.hours_used == 3.0 and second.claims.approved_until is None


# ---- session analysis: one Analysis per rejected record ----------------------------------------------------------------

def _approval(**kw):
    base = dict(id="ap-1", holder="Pat Doe", resources="Build Server",
                summary="2026-09-14 to 2026-10-31, 10 hours per week, week starts Monday")
    return parse_approval(base | kw).to_json()


def _session(sid, day, start, end, status="valid", last=None):
    iso = lambda hm: f"2026-{day}T{hm}:00Z" if hm else None
    return {"id": sid, "username": "pat", "resource": "Build Server", "start": iso(start), "end": iso(end),
            "last_activity": iso(last or end), "status": status}


def _enrichment(sessions, approvals=None, **kw):
    facts = Facts()
    facts.set("approvals", [_approval()] if approvals is None else approvals, "approvals")
    facts.set("sessions", sessions, "sessions")
    facts.set("log_week_start_day", "Monday", "sessions")
    user = {"id": "u1", "name": "Pat Doe", "status": "active", "manager": "Tom Hale"}
    return {"user": user, "candidates": [{"id": "u1", "name": "Pat Doe"}], "error": None, "system_errors": {},
            "facts": facts.to_json()} | kw


def _c(**claims):
    return Classification(ticket_type=TicketType.SESSION_REJECTED, summary="x", claims=Claims(**claims))


def test_each_rejected_record_is_analyzed_on_its_own():
    rows = [_session("s1", "09-29", "09:00", None, "rejected", last="13:00"),
            _session("s2", "09-30", "09:00", "10:00", "rejected"), _session("s3", "09-30", "11:00", "12:00")]
    out = analyze_sessions(_c(), _enrichment(rows), NOW)
    assert [(a.target["id"], a.state) for a in out] == [("s1", "record_never_ended"), ("s2", "cause_unknown")]
    assert out[0].evidence["last_activity"] == "2026-09-29 13:00 UTC" and out[0].target["system"] == "sessions"
    assert out[1].route_reasons and out[1].confidence == "LOW"


def test_a_record_with_two_problems_goes_to_a_person():
    rows = [_session("s1", "10-02", "09:00", None, "rejected", last="10:00")]
    a = analyze_sessions(_c(), _enrichment(rows, approvals=[_approval(summary="2026-09-14 to 2026-09-30, 10 hours per week, week starts Monday")]), NOW)[0]
    assert a.state == "outside_approval_dates" and a.route_reasons and "record_never_ended" in a.route_reasons[0]


def test_findings_compare_the_tickets_claims_with_the_systems_that_hold_the_record():
    rows = [_session("s1", "09-28", "09:00", "14:00"), _session("s2", "09-30", "09:00", "13:00"),
            _session("s3", "10-02", "09:00", "12:00", "rejected")]
    a = analyze_sessions(_c(hours_used=8, approved_until="2026-10-31", weekly_hours=15), _enrichment(rows), NOW)[0]
    assert a.state == "weekly_cap_exceeded"
    assert {(f["field"], f["system"]): (f["claimed"], f["actual"]) for f in a.findings} == {
        ("hours_logged_in_week", "sessions"): ("8", "12"), ("weekly_allowance_hours", "approvals"): ("15", "10")}


def test_no_rejected_records_and_no_approval_are_findings_not_guesses():
    a = analyze_sessions(_c(), _enrichment([_session("s1", "09-29", "09:00", "10:00")]), NOW)[0]
    assert a.state == "no_rejected_records" and a.findings[0]["system"] == "sessions"
    b = analyze_sessions(_c(), _enrichment([_session("s1", "09-29", "09:00", "10:00", "rejected")], approvals=[]), NOW)[0]
    assert b.state == "no_approval_found" and b.findings[0]["system"] == "approvals"


def test_a_system_that_could_not_be_read_is_a_reason_for_a_person_not_a_guess():
    a = analyze_sessions(_c(), _enrichment([], system_errors={"approvals": "approvals system answered 500"}), NOW)[0]
    assert a.state == "lookup_failed" and "approvals" in a.route_reasons[0]


def test_systems_that_disagree_are_routed_to_a_person():
    facts = Facts()
    facts.set("person.name", "Pat Doe", "provisioning")
    facts.set("person.name", "Patricia Doe", "approvals")
    e = _enrichment([_session("s1", "09-29", "09:00", None, "rejected", last="13:00")]) | {"facts": facts.to_json()}
    a = analyze_sessions(_c(), e, NOW)[0]
    assert a.state == "systems_disagree" and "provisioning says Pat Doe" in a.route_reasons[0]


def test_an_unreadable_approval_is_routed_to_a_person():
    facts = Facts()
    facts.set("approval_parse_errors", ["approval ap-9: no weekly allowance found"], "approvals")
    a = analyze_sessions(_c(), _enrichment([]) | {"facts": facts.to_json()}, NOW)[0]
    assert a.state == "approval_unreadable" and "ap-9" in a.route_reasons[0]


def test_identity_problems_come_first_as_before():
    a = analyze_sessions(_c(), {"error": "abbreviated_name"}, NOW)[0]
    assert a.state == "unresolvable_person"
