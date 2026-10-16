"""Fix safety (inventory item 5): ledger in the database, the choke point, per-row skips, the allowlist."""

from __future__ import annotations

import sqlite3

import pytest

from helpdesk_kit import db
from helpdesk_kit.kb import AUTO_EXECUTE_MIN_SOLVED, Rule
from helpdesk_kit.safety import FIX_ALLOWLIST, hard_guards, may_execute_with_approval, should_auto_execute


def rule(**kw) -> Rule:
    base = dict(id="r", kind="assert", ticket_type="X", state="job_stuck", action="retry_job", auto_execute=True,
                route_to_human=False, confidence="HIGH", reply="Hi there,")
    return Rule(**(base | kw))


GOOD = dict(state="job_stuck", route_reasons=())


def test_the_happy_path_needs_a_track_record():
    assert not should_auto_execute(rule(), "HIGH", AUTO_EXECUTE_MIN_SOLVED - 1, **GOOD)
    assert should_auto_execute(rule(), "HIGH", AUTO_EXECUTE_MIN_SOLVED, **GOOD)


def test_anything_routed_to_a_human_is_refused_whatever_upstream_said():
    d = should_auto_execute(rule(), "HIGH", 99, state="job_stuck", route_reasons=["addressed to Tom"])
    assert not d and "routed to a person" in d.reason
    assert not may_execute_with_approval(rule(), "job_stuck", ["more than one account matches"])


def test_a_rule_that_always_goes_to_a_person_is_refused_even_with_every_other_flag_set():
    d = should_auto_execute(rule(route_to_human=True), "HIGH", 99, **GOOD)
    assert not d and "always goes to a person" in d.reason


def test_the_allowlist_is_checked_against_the_diagnosis_and_the_action():
    assert FIX_ALLOWLIST == {"job_stuck": "retry_job", "record_never_ended": "close_session"}
    assert not should_auto_execute(rule(state="seat_pool_exhausted"), "HIGH", 99, state="seat_pool_exhausted")
    assert not should_auto_execute(rule(action="close_session"), "HIGH", 99, **GOOD)      # wrong action for the state
    assert should_auto_execute(rule(state="record_never_ended", action="close_session"), "HIGH", 99,
                               state="record_never_ended")


def test_a_person_can_approve_a_fix_below_the_gate_but_not_past_the_hard_guards():
    assert may_execute_with_approval(rule(), "job_stuck")
    assert not may_execute_with_approval(rule(), "weekly_cap_exceeded")
    assert not may_execute_with_approval(rule(route_to_human=True), "job_stuck")
    assert not may_execute_with_approval(None, "job_stuck")
    assert not may_execute_with_approval(rule(action="none"), "job_stuck")


def test_opt_in_confidence_and_no_rule_are_each_checked():
    assert not should_auto_execute(rule(auto_execute=False), "HIGH", 99, **GOOD)
    assert not should_auto_execute(rule(), "MEDIUM", 99, **GOOD)
    assert not should_auto_execute(None, "HIGH", 99, **GOOD)
    assert not hard_guards(None, "job_stuck").allowed


def test_every_refusal_says_why():
    for args in [(rule(auto_execute=False), "HIGH", 99), (rule(), "LOW", 99), (rule(), "HIGH", 0)]:
        assert should_auto_execute(*args, **GOOD).reason


# ---- the ledger lives in the database -----------------------------------------------------------------

def test_a_record_can_be_claimed_once_by_anyone():
    conn = db.connect(":memory:")
    assert db.claim_fix(conn, "sessions", "s201", "1012", "close_session")
    assert not db.claim_fix(conn, "sessions", "s201", "1012", "close_session")
    assert not db.claim_fix(conn, "sessions", "s201", "9999", "close_session")       # a different ticket, same record
    assert db.claim_fix(conn, "provisioning", "s201", "1012", "retry_job")           # same id in another system is another record


def test_the_ledger_refuses_deletes_and_rewrites_but_takes_an_outcome():
    conn = db.connect(":memory:")
    db.claim_fix(conn, "sessions", "s201", "1012", "close_session")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM fix_ledger")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE fix_ledger SET ticket_id='other'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE fix_ledger SET record_id='s999'")
    db.record_fix_outcome(conn, "sessions", "s201", "APPLIED")
    assert conn.execute("SELECT outcome FROM fix_ledger").fetchone()[0] == "APPLIED"


def test_the_ledger_holds_across_connections(tmp_path):
    a, b = db.connect(tmp_path / "x.db"), db.connect(tmp_path / "x.db")
    assert db.claim_fix(a, "sessions", "s1", "1", "close_session")
    assert not db.claim_fix(b, "sessions", "s1", "2", "close_session")


def test_skips_are_recorded_with_their_reason_per_row():
    conn = db.connect(":memory:")
    db.record_skip(conn, "1012", 1, "sessions", "s202", "diagnosis resource_not_approved is not on the allowlist")
    db.record_skip(conn, "1012", 2, "sessions", "s203", "already fixed once")
    rows = db.skips(conn, "1012")
    assert [(r["unit"], r["record_id"]) for r in rows] == [(1, "s202"), (2, "s203")]
    assert "allowlist" in rows[0]["reason"] and db.skips(conn, "other") == []


def test_ticket_approval_is_per_ticket():
    conn = db.connect(":memory:")
    assert not db.is_ticket_approved(conn, "1")
    db.approve_ticket(conn, "1", "dashboard")
    assert db.is_ticket_approved(conn, "1") and not db.is_ticket_approved(conn, "2")


def test_v1_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE proposals (ticket_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(proposals)")}
    assert {"reply_edited", "sent_at", "revisions", "issue_count"} <= cols
