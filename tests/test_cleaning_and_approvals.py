"""Unicode cleaning, approval parsing and claim-vs-record findings (inventory items 1 and 2)."""

from __future__ import annotations

from datetime import date

import pytest

from helpdesk_kit.approval import (ApprovalParseError, compare_claims, parse_approval, parse_date_range,
                                   parse_resources, parse_week_start, parse_weekly_allowance)
from helpdesk_kit.facts import Facts
from helpdesk_kit.textclean import clean_text, name_key

ZWSP, BOM, LRM, NBSP = "\u200b", "\ufeff", "\u200e", "\u00a0"


# ---- cleaning -----------------------------------------------------------------------------------

@pytest.mark.parametrize("dirty", [
    f"Priya{ZWSP} Raman", f"Priya Raman{BOM}", f"Priya{NBSP}Raman", f"{LRM}Priya Raman", "Priya\u00adRaman".replace("\u00ad", " "),
    "Priya\u2009Raman", "PRIYA   RAMAN", "priya\tRaman", "Priya\u2060 Raman\u200d"])
def test_invisible_characters_do_not_change_a_name_key(dirty):
    assert name_key(dirty) == "priya raman"


def test_soft_hyphen_and_zero_width_joiner_are_removed_not_replaced_by_a_space():
    assert clean_text("Pri\u00adya Ra\u200dman") == "Priya Raman"


def test_lookalike_letters_from_other_alphabets_fold_to_latin():
    assert name_key("Priya Raman".replace("a", "\u0430")) == "priya raman"      # Cyrillic a


def test_dashes_quotes_and_fullwidth_forms_are_straightened():
    assert clean_text("O\u2019Brien \u2013 Ops") == "O'Brien - Ops"
    assert clean_text("\uff24\uff41\uff4e\uff41") == "Dana"                       # fullwidth letters (NFKC)


def test_multiline_keeps_line_breaks_and_single_line_does_not():
    assert clean_text(f"1. one{ZWSP}\n2.  two", multiline=True) == "1. one\n2. two"
    assert clean_text("a\nb") == "a b"


def test_empty_and_none():
    assert clean_text(None) == "" and name_key("") == ""


def test_ingest_cleans_names_and_body():
    from helpdesk_kit import ingest
    payload = {"ticket": {"id": 1, "subject": f"Hi{ZWSP}", "description": f"Dana{NBSP}Whitfield{ZWSP} still can't",
                          "requester_id": 7, "status": "open", "created_at": "2026-10-05T09:00:00Z"},
               "users": [{"id": 7, "name": f"Pat{ZWSP} Doe", "email": "pat.doe@example-corp.test"}]}
    row = ingest.normalize(payload, [])
    assert row["requester_name"] == "Pat Doe" and row["body"] == "Dana Whitfield still can't"


# ---- parsing --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Approved 2026-09-14 to 2026-10-31", (date(2026, 9, 14), date(2026, 10, 31))),
    ("Valid 21 Sep 2026 - 25 Oct 2026 | x", (date(2026, 9, 21), date(2026, 10, 25))),
    ("from Sep 14, 2026 through Nov 1, 2026", (date(2026, 9, 14), date(2026, 11, 1))),
    ("1 January 2026 \u2013 31 December 2026", (date(2026, 1, 1), date(2026, 12, 31))),
    (f"2026-09-14{ZWSP} until 2026-09-20", (date(2026, 9, 14), date(2026, 9, 20))),
])
def test_date_range_formats(text, expected):
    assert parse_date_range(text) == expected


@pytest.mark.parametrize("text", ["no dates here", "only 2026-09-14", "2026-10-31 to 2026-09-14",
                                  "2026-09-14 to 2026-09-20 and 2026-10-01", "32 Foo 2026 to 33 Bar 2026"])
def test_bad_date_ranges_raise(text):
    with pytest.raises(ApprovalParseError):
        parse_date_range(text)


@pytest.mark.parametrize("text,minutes", [("up to 10 hours per week", 600), ("max 10 hrs/week", 600),
                                          ("Allowance: 7.5 h/week", 450), ("90 minutes a week", 90),
                                          ("10 hours each week", 600)])
def test_weekly_allowance_in_minutes(text, minutes):
    assert parse_weekly_allowance(text) == minutes


def test_missing_weekly_allowance_raises():
    with pytest.raises(ApprovalParseError, match="weekly allowance"):
        parse_weekly_allowance("Approved for 2026-09-14 to 2026-09-20")


@pytest.mark.parametrize("text,day", [("Week starts Monday.", 0), ("Week starts: Sunday.", 6),
                                      ("weeks run Wednesday to Tuesday", 2), ("the week begins on Fri", 4)])
def test_week_start_day(text, day):
    assert parse_week_start(text) == day


def test_missing_week_start_raises():
    with pytest.raises(ApprovalParseError, match="week starts on"):
        parse_week_start("Approved, 10 hours per week")


def test_resources_split_on_any_separator_and_are_cleaned():
    assert parse_resources(f"Build{NBSP}Server; Finance Shared Drive, VPN Users\nFinance{ZWSP} Console") == (
        "Build Server", "Finance Shared Drive", "VPN Users", "Finance Console")


def test_parse_approval_end_to_end_with_dirty_record():
    a = parse_approval({"id": "ap-1", "holder": f"Priya{ZWSP} Raman", "resources": f"Build{NBSP}Server; Finance Shared Drive",
                        "summary": "Valid 21 Sep 2026 - 25 Oct 2026 | max 10 hrs/week | weeks run Monday to Sunday"})
    assert (a.holder, a.holder_key) == ("Priya Raman", "priya raman")
    assert (a.start, a.end, a.weekly_minutes, a.week_start) == (date(2026, 9, 21), date(2026, 10, 25), 600, 0)
    assert a.resources == ("Build Server", "Finance Shared Drive")
    assert name_key("build server") in a.resource_keys
    assert a.covers(date(2026, 10, 1)) and not a.covers(date(2026, 10, 26))
    assert a.window == "2026-09-21 to 2026-10-25" and a.week_start_name == "Monday"


def test_unreadable_approval_names_the_record_and_the_problem():
    with pytest.raises(ApprovalParseError, match=r"ap-9.*weekly allowance"):
        parse_approval({"id": "ap-9", "holder": "X", "resources": "", "summary": "Approved 2026-09-14 to 2026-09-20. Week starts Monday."})
    with pytest.raises(ApprovalParseError, match="no summary"):
        parse_approval({"id": "ap-9", "holder": "X"})


def test_approval_round_trips_through_json():
    from helpdesk_kit.approval import Approval
    a = parse_approval({"id": "ap-1", "holder": "Pat Doe", "resources": "A; B",
                        "summary": "2026-09-14 to 2026-09-20, 5 hours per week, week starts Sunday"})
    assert Approval.from_json(a.to_json()) == a


# ---- findings ---------------------------------------------------------------------------------------

def _approval():
    return parse_approval({"id": "ap-1", "holder": "Pat Doe", "resources": "A",
                           "summary": "2026-09-14 to 2026-09-30, 10 hours per week, week starts Sunday"})


def test_findings_say_what_the_ticket_claims_and_which_system_disagrees():
    claims = {"approved_until": "2026-10-31", "weekly_hours": 15.0, "hours_used": 8.0, "week_starts": "Monday"}
    found = compare_claims(claims, approval=_approval(), hours_logged=12.0, log_week_start="Monday")
    by_field = {(f["field"], f["system"]): (f["claimed"], f["actual"]) for f in found}
    assert by_field[("approval_end_date", "approvals")] == ("2026-10-31", "2026-09-30")
    assert by_field[("weekly_allowance_hours", "approvals")] == ("15", "10")
    assert by_field[("hours_logged_in_week", "sessions")] == ("8", "12")
    assert by_field[("week_start_day", "approvals")] == ("Monday", "Sunday")
    assert ("week_start_day", "sessions") not in by_field       # the log agrees with the ticket here


def test_a_claim_the_system_agrees_with_makes_no_finding():
    claims = {"approved_until": "2026-09-30", "weekly_hours": 10.0, "hours_used": 12.0, "week_starts": "Sunday"}
    found = compare_claims(claims, approval=_approval(), hours_logged=12.0, log_week_start="Sunday")
    assert found == []


def test_no_claims_no_findings():
    assert compare_claims({}, approval=_approval(), hours_logged=3, log_week_start="Monday") == []


# ---- facts: merged object with provenance --------------------------------------------------------------

def test_facts_keep_the_system_that_said_each_value():
    f = Facts()
    f.set("person.status", "active", "provisioning")
    f.update({"approvals": [1], "approval.count": 1}, "approvals")
    assert f.source("person.status") == "provisioning" and f.source("approvals") == "approvals"
    assert f.provenance() == {"person.status": "provisioning", "approvals": "approvals", "approval.count": "approvals"}
    assert f.get("missing", "x") == "x" and "approvals" in f


def test_two_systems_disagreeing_is_a_conflict_and_the_first_value_stays():
    f = Facts()
    f.set("person.name", "Pat Doe", "provisioning")
    f.set("person.name", "Patrick Doe", "approvals")
    f.set("person.name", "Pat Doe", "sessions")           # agreement is not a conflict
    assert f.get("person.name") == "Pat Doe"
    assert f.conflicts == [{"key": "person.name", "values": {"provisioning": "Pat Doe", "approvals": "Patrick Doe"}}]


def test_merge_and_json_round_trip_keep_provenance_and_conflicts():
    a, b = Facts(), Facts()
    a.set("x", 1, "one")
    b.set("x", 2, "two")
    b.set("y", 3, "two")
    a.merge(b)
    back = Facts.from_json(a.to_json())
    assert back.values() == {"x": 1, "y": 3} and back.provenance() == {"x": "one", "y": "two"}
    assert back.conflicts == a.conflicts and len(back.conflicts) == 1
