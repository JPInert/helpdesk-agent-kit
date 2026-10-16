"""Unseen issues (inventory item 6): proposals queue, approval, graduation, acceptance rate."""

from __future__ import annotations

import json

import pytest
import yaml

from helpdesk_kit import db, proposals
from helpdesk_kit.analyze import IDENTITY_STATES
from helpdesk_kit.kb import AUTO_EXECUTE_MIN_SOLVED, Rule, load
from helpdesk_kit.safety import should_auto_execute

from conftest import ROOT

EVIDENCE = {"resource": "Build Server", "record_id": "s240", "requester_email": "kofi@example-corp.test"}


def test_only_real_findings_with_no_specific_rule_are_proposable():
    catch_all = Rule(id="catch-all", kind="ask", ticket_type="*", reply="x")
    specific = Rule(id="r", kind="assert", ticket_type="SESSION_REJECTED", state="s", reply="x")
    assert proposals.is_proposable("overlapping_sessions", catch_all, "record")
    assert proposals.is_proposable("overlapping_sessions", None, "record")
    assert not proposals.is_proposable("overlapping_sessions", specific, "record")
    assert not proposals.is_proposable("overlapping_sessions", catch_all, "non_record")
    assert not proposals.is_proposable(None, catch_all, "record")
    assert all(not proposals.is_proposable(s, catch_all, "record") for s in IDENTITY_STATES)
    keyword = Rule(id="hardware", kind="handoff", keywords=["printer"], reply="x")        # matched by words, not "no rule"
    assert not proposals.is_proposable("job_stuck", keyword, "record")


def test_a_proposal_is_filed_with_a_draft_and_its_evidence_and_no_recipients():
    conn = db.connect(":memory:")
    pid = proposals.propose(conn, "1016", "SESSION_REJECTED", "overlapping_sessions", EVIDENCE, ["rule catch-all"])
    row = proposals.get(conn, pid)
    assert row["status"] == "PENDING" and json.loads(row["tickets_json"]) == ["1016"]
    ev = json.loads(row["evidence_json"])
    assert ev["state"] == "overlapping_sessions" and ev["evidence"]["record_id"] == "s240"
    assert "requester_email" not in ev["evidence"]
    rule = proposals.validate_draft(row["draft_yaml"])
    assert rule.id == "proposed-session-rejected-overlapping-sessions" and rule.route_to_human and rule.action == "none"


def test_the_same_shape_of_issue_adds_to_one_proposal_instead_of_a_second():
    conn = db.connect(":memory:")
    a = proposals.propose(conn, "1016", "SESSION_REJECTED", "overlapping_sessions", EVIDENCE, [])
    b = proposals.propose(conn, "1020", "SESSION_REJECTED", "overlapping_sessions", EVIDENCE, [])
    assert a == b and json.loads(proposals.get(conn, a)["tickets_json"]) == ["1016", "1020"]
    assert len(proposals.pending(conn)) == 1
    assert proposals.propose(conn, "1020", "SESSION_REJECTED", "overlapping_sessions", EVIDENCE, []) == a
    assert json.loads(proposals.get(conn, a)["tickets_json"]) == ["1016", "1020"]


def test_a_draft_for_a_state_with_an_allowlisted_fix_names_that_fix_and_opts_in():
    rule = proposals.validate_draft(proposals.draft_rule_yaml("SESSION_REJECTED", "record_never_ended", ["resource"]))
    assert (rule.action, rule.auto_execute, rule.route_to_human, rule.needs) == ("close_session", True, False, ["new_status"])
    assert "${resource}" in rule.reply and rule.kind == "assert"


def test_a_draft_for_any_other_state_only_hands_over_to_a_person():
    rule = proposals.validate_draft(proposals.draft_rule_yaml("SESSION_REJECTED", "daily_max_exceeded", ["resource"]))
    assert (rule.action, rule.auto_execute, rule.route_to_human, rule.kind) == ("none", False, True, "handoff")


def test_an_edited_draft_that_breaks_house_style_is_refused():
    bad = proposals.draft_rule_yaml("X", "y", []).replace("Thanks for flagging this.", "Thanks \u2014 also.")
    with pytest.raises(ValueError, match="house style"):
        proposals.validate_draft(bad)
    with pytest.raises(ValueError, match="exactly one rule"):
        proposals.validate_draft("- {id: a}\n- {id: b}\n")


def test_approving_writes_the_rule_where_the_kb_loads_it_and_counts_as_accepted(tmp_path):
    conn, approved = db.connect(":memory:"), tmp_path / "approved.yaml"
    pid = proposals.propose(conn, "1016", "SESSION_REJECTED", "overlapping_sessions", EVIDENCE, [])
    assert proposals.approve(conn, pid, "reviewer", approved) == "proposed-session-rejected-overlapping-sessions"
    assert yaml.safe_load(approved.read_text())[0]["state"] == "overlapping_sessions"
    kb = load(ROOT / "kb" / "rules.yaml", approved)
    match = kb.match("x", "SESSION_REJECTED", "internal", "overlapping_sessions")
    assert match.id.startswith("proposed-") and match.origin == "proposed"
    assert kb.match("x", "SESSION_REJECTED", "internal", "record_never_ended").origin == "builtin"
    assert proposals.stats(conn) == {"proposed": 1, "accepted": 1, "rejected": 0, "pending": 0, "acceptance_rate": 1.0}
    with pytest.raises(ValueError, match="not pending"):
        proposals.approve(conn, pid, "reviewer", approved)


def test_a_rejected_proposal_is_not_made_again_and_the_rate_is_tracked(tmp_path):
    conn = db.connect(":memory:")
    a = proposals.propose(conn, "1", "T", "s1", {}, [])
    b = proposals.propose(conn, "2", "T", "s2", {}, [])
    proposals.propose(conn, "3", "T", "s3", {}, [])
    proposals.approve(conn, a, "r", tmp_path / "a.yaml")
    proposals.reject(conn, b, "r")
    assert proposals.propose(conn, "9", "T", "s2", {}, []) is None
    assert proposals.stats(conn) == {"proposed": 3, "accepted": 1, "rejected": 1, "pending": 1, "acceptance_rate": 0.5}
    assert proposals.stats(db.connect(":memory:"))["acceptance_rate"] is None


def test_an_approved_rule_starts_in_approve_each_fix_mode_and_graduates_only_on_a_track_record(tmp_path):
    """The drafted rule opts in to auto-execute, but at zero confirmed solves a person approves every fix."""
    conn, approved = db.connect(":memory:"), tmp_path / "approved.yaml"
    pid = proposals.propose(conn, "1", "SESSION_REJECTED", "record_never_ended", {"resource": "Build Server"}, [])
    proposals.approve(conn, pid, "r", approved)
    rule = load(ROOT / "kb" / "rules.yaml", approved).rule("proposed-session-rejected-record-never-ended")
    assert rule.origin == "proposed" and rule.auto_execute
    for solved in range(AUTO_EXECUTE_MIN_SOLVED):
        assert not should_auto_execute(rule, "HIGH", db.times_solved(conn, rule.id), state="record_never_ended")
        db.mark_solved(conn, rule.id)
    assert should_auto_execute(rule, "HIGH", db.times_solved(conn, rule.id), state="record_never_ended")


def test_two_approvals_of_the_same_rule_id_do_not_overwrite(tmp_path):
    conn, approved = db.connect(":memory:"), tmp_path / "approved.yaml"
    a = proposals.propose(conn, "1", "T", "s", {}, [])
    proposals.approve(conn, a, "r", approved)
    conn.execute("UPDATE rule_proposals SET status='PENDING' WHERE id=?", (a,))
    with pytest.raises(ValueError, match="already approved"):
        proposals.approve(conn, a, "r", approved)
