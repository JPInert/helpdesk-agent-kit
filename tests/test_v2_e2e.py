"""v2 end to end: fixture tickets -> three mock systems (one driven by headless Chromium) -> reply.

Covers multi-system enrichment with provenance, session diagnosis, multi-issue merge, fix safety
(ledger, choke point, per-row skips), the proposals queue and the send stage, all on mocks.
"""

from __future__ import annotations

import json
import shutil

import pytest
import requests

from helpdesk_kit import db, proposals
from helpdesk_kit.cli import build_deps, mock_systems
from helpdesk_kit.config import Settings
from helpdesk_kit.facts import Facts
from helpdesk_kit.kb import AUTO_EXECUTE_MIN_SOLVED
from helpdesk_kit.pipeline import run_ticket

from conftest import FIXTURES


@pytest.fixture
def env(tmp_path, monkeypatch):
    import mock_portal.app as portal_app
    import mock_portal.sessions_app as sessions_app
    monkeypatch.setattr(portal_app, "PROCESS_SECONDS", 0.5)
    monkeypatch.setattr(portal_app, "STALE_SECONDS", 1.5)
    monkeypatch.setattr(sessions_app, "PROCESS_SECONDS", 0.5)
    with mock_systems() as ms:
        s = ms.settings(db_path=tmp_path / "v2.db", approved_rules_path=tmp_path / "approved.yaml", flip_timeout_s=5)
        yield ms, s, build_deps(s, live=False, use_llm=False), db.connect(s.db_path)


def row(ms, sid):
    return next(r for r in ms.sessions_app.rows if r["id"] == sid)


def units(conn, tid):
    return [dict(r) for r in conn.execute("SELECT * FROM unit_results WHERE ticket_id=? ORDER BY unit", (tid,))]


# ---- diagnosis of every shipped session scenario, dry run -----------------------------------------------------------

@pytest.mark.parametrize("tid,state,rule", [
    ("1013", "weekly_cap_exceeded", "session-weekly-cap"),
    ("1014", "wrong_week_start", "session-wrong-week-start"),
    ("1015", "outside_approval_dates", "session-outside-approval"),
])
def test_each_diagnosis_reaches_its_rule(env, tid, state, rule):
    _, _, deps, conn = env
    r = run_ticket(conn, deps, tid)
    assert (r.ticket_type, r.state, r.rule_id) == ("SESSION_REJECTED", state, rule)
    assert r.reply.startswith("Hi there,") and r.review == "PASS"


def test_the_dirty_unicode_in_the_approval_does_not_stop_the_match(env):
    _, _, deps, conn = env
    r = run_ticket(conn, deps, "1013")                       # the approval's holder name has a zero-width space in it
    assert r.state == "weekly_cap_exceeded" and "12 hours" in r.reply and "10 hours per week" in r.reply


def test_facts_from_three_systems_are_stored_with_their_source(env):
    _, _, deps, conn = env
    run_ticket(conn, deps, "1013")
    facts = Facts.from_json(json.loads(units(conn, "1013")[0]["facts_json"]))
    prov = facts.provenance()
    assert prov["person.status"] == "provisioning" and prov["approvals"] == "approvals" and prov["sessions"] == "sessions"


def test_findings_say_what_the_ticket_claimed_and_which_system_disagrees(env):
    _, _, deps, conn = env
    run_ticket(conn, deps, "1015")
    run_ticket(conn, deps, "1013")
    rows = {(r["ticket_id"], r["field"]): (r["claimed"], r["actual"], json.loads(r["extra_json"])["system"])
            for r in conn.execute("SELECT * FROM findings WHERE extra_json LIKE '%system%'")}
    assert rows[("1015", "approval_end_date")] == ("2026-10-31", "2026-09-30", "approvals")
    assert rows[("1013", "hours_logged_in_week")] == ("8", "12", "sessions")


def test_the_dry_run_changes_nothing_in_any_system(env):
    ms, _, deps, conn = env
    r = run_ticket(conn, deps, "1012")
    assert [u.status for u in r.units] == ["AWAITING_APPROVAL", "HUMAN_REVIEW_REQUIRED"] and r.status == "HUMAN_REVIEW_REQUIRED"
    assert row(ms, "s201")["end"] is None and row(ms, "s201")["status"] == "rejected"
    assert db.skips(conn, "1012")[0]["record_id"] == "s202"      # the second row is recorded as skipped, with a reason


# ---- fix safety -----------------------------------------------------------------------------------------------------

def test_an_approved_fix_is_applied_confirmed_and_the_other_row_is_skipped_with_a_reason(env):
    ms, _, deps, conn = env
    r = run_ticket(conn, deps, "1012", apply=True, approve=True)
    assert r.fixed and [u.fixed for u in r.units] == [True, False]
    assert row(ms, "s201")["status"] == "valid" and row(ms, "s201")["end"] == "2026-09-29T13:30:00Z"
    assert row(ms, "s202")["status"] == "rejected"                       # the human-routed row was left alone
    skip = db.skips(conn, "1012")
    assert [(s["record_id"], "person" in s["reason"]) for s in skip] == [("s202", True)]
    ledger = conn.execute("SELECT system, record_id, outcome FROM fix_ledger").fetchall()
    assert [tuple(x) for x in ledger] == [("sessions", "s201", "APPLIED")]
    assert db.times_solved(conn, "session-never-ended") == 1
    assert "set the end time there" in r.reply and "Finance Console" in r.reply     # one merged reply covers both rows


def test_the_ledger_stops_a_second_ticket_from_fixing_the_same_record_again(env, tmp_path):
    ms, s, deps, conn = env
    run_ticket(conn, deps, "1012", apply=True, approve=True)
    ms.sessions_app.rows[:] = __import__("mock_portal.sessions_app", fromlist=["seed"]).seed()      # the record is broken again
    fx = tmp_path / "fx"
    shutil.copytree(FIXTURES, fx)
    data = json.loads((fx / "zendesk" / "1012.json").read_text())
    data["ticket"].update(id=2012, created_at="2026-10-07T10:10:00Z")
    (fx / "zendesk" / "2012.json").write_text(json.dumps(data))
    deps.source.dir = fx
    r = run_ticket(conn, deps, "2012", apply=True, approve=True)
    assert not r.fixed and row(ms, "s201")["end"] is None
    assert any("already fixed once by the agent (ledger)" in why for why in r.route_reasons)
    assert any("ledger" in sk["reason"] for sk in db.skips(conn, "2012"))
    assert conn.execute("SELECT COUNT(*) FROM fix_ledger").fetchone()[0] == 1


def test_the_same_ticket_is_never_retried_for_a_record(env):
    ms, _, deps, conn = env
    run_ticket(conn, deps, "1012", apply=True, approve=True)
    ms.sessions_app.rows[:] = __import__("mock_portal.sessions_app", fromlist=["seed"]).seed()
    r = run_ticket(conn, deps, "1012", apply=True, approve=True)
    assert not r.fixed and any("already tried s201 once on this ticket" in why for why in r.route_reasons)


def test_a_dashboard_approval_lets_the_next_run_apply_without_the_global_flag(env):
    ms, _, deps, conn = env
    assert not run_ticket(conn, deps, "1012", apply=True).fixed          # no approval: waits
    conn.execute("DELETE FROM attempts")                                 # (a fresh try is allowed once approved)
    db.approve_ticket(conn, "1012", "dashboard")
    assert run_ticket(conn, deps, "1012", apply=True).fixed and row(ms, "s201")["status"] == "valid"


def test_a_rule_with_a_track_record_fixes_without_an_approval(env):
    ms, _, deps, conn = env
    for _ in range(AUTO_EXECUTE_MIN_SOLVED):
        db.mark_solved(conn, "session-never-ended")
    r = run_ticket(conn, deps, "1017", apply=True)                       # no --approve
    by_rule = {u.rule_id: u for u in r.units}
    assert by_rule["session-never-ended"].fixed                          # earned the gate
    assert not by_rule["license-stuck"].fixed and by_rule["license-stuck"].status == "AWAITING_APPROVAL"   # has not


def test_a_row_routed_to_a_person_is_never_executed_even_with_approval_and_a_record(env):
    ms, _, deps, conn = env
    r = run_ticket(conn, deps, "1014", apply=True, approve=True)         # wrong_week_start: a person fixes the log
    assert not r.fixed and r.status == "HUMAN_REVIEW_REQUIRED" and conn.execute("SELECT COUNT(*) FROM fix_ledger").fetchone()[0] == 0


def test_a_fix_that_does_not_settle_goes_to_a_person_and_is_not_retried(env, monkeypatch):
    import mock_portal.sessions_app as sessions_app
    ms, _, deps, conn = env
    monkeypatch.setattr(sessions_app, "PROCESS_SECONDS", 600)
    deps.settings = Settings(**{**deps.settings.__dict__, "flip_timeout_s": 1})
    r = run_ticket(conn, deps, "1012", apply=True, approve=True)
    assert not r.fixed and any("not confirmed" in why for why in r.route_reasons)
    assert conn.execute("SELECT outcome FROM fix_ledger").fetchone()[0].startswith("close_session not confirmed")


# ---- several issues in one ticket -------------------------------------------------------------------------------------------

def test_a_ticket_with_two_issues_is_fixed_independently_and_answered_in_one_message(env):
    ms, _, deps, conn = env
    r = run_ticket(conn, deps, "1017", apply=True, approve=True, post="public")
    assert [(u.ticket_type, u.rule_id, u.fixed) for u in r.units] == [
        ("LICENSE_NOT_ASSIGNED", "license-stuck", True), ("SESSION_REJECTED", "session-never-ended", True)]
    assert r.status == "SENT" and r.reply.count("Hi there,") == 1
    assert "Office Suite request (j108)" in r.reply and "session s250" in r.reply
    assert len(deps.source.sent) == 1 and deps.source.sent[0]["html_body"].endswith("<p>Thanks,<br>IT Help Desk</p>")
    assert row(ms, "s250")["status"] == "valid"
    assert [u["issue"] for u in units(conn, "1017")] == [0, 1]
    assert db.get(conn, "proposals", "ticket_id", "1017")["issue_count"] == 2


def test_one_unit_needing_a_person_holds_the_whole_ticket_back(env):
    _, _, deps, conn = env
    r = run_ticket(conn, deps, "1012", post="public")
    assert r.status == "HUMAN_REVIEW_REQUIRED" and [m for m in deps.source.sent if m["public"]] == []


def test_a_draft_note_says_what_it_does_not_cover(env):
    _, _, deps, conn = env
    run_ticket(conn, deps, "1012", post="note")
    note = deps.source.sent[-1]
    assert not note["public"] and "Not covered by this draft" in note["html_body"] and "s201" in note["html_body"]


# ---- unseen issues ------------------------------------------------------------------------------------------------------------

def test_an_issue_with_no_rule_goes_to_a_person_and_files_one_proposal(env):
    ms, s, deps, conn = env
    r = run_ticket(conn, deps, "1016")
    assert [u.state for u in r.units] == ["overlapping_sessions"] * 2 and r.status == "HUMAN_REVIEW_REQUIRED"
    assert {u.rule_id for u in r.units} == {"catch-all"}
    pending = proposals.pending(conn)
    assert [p["signature"] for p in pending] == ["SESSION_REJECTED/overlapping_sessions"]
    assert json.loads(pending[0]["evidence_json"])["evidence"]["record_id"] in ("s240", "s241")
    assert len(db.skips(conn, "1016")) == 2


def test_identity_problems_and_matched_rules_do_not_file_proposals(env):
    _, _, deps, conn = env
    for tid in ("1006", "1013", "1014", "1007"):
        run_ticket(conn, deps, tid)
    assert proposals.pending(conn) == []


def test_after_approval_the_next_ticket_matches_the_new_rule_and_no_second_proposal_is_filed(env):
    ms, s, deps, conn = env
    run_ticket(conn, deps, "1016")
    rule_id = proposals.approve(conn, proposals.pending(conn)[0]["id"], "tester", s.approved_rules_path)
    deps2 = build_deps(s, live=False, use_llm=False)                     # a fresh process loads the approved rule
    conn.execute("DELETE FROM attempts")
    r = run_ticket(conn, deps2, "1016")
    assert {u.rule_id for u in r.units} == {rule_id} and r.reply.startswith("Hi there,")
    assert proposals.pending(conn) == [] and proposals.stats(conn)["accepted"] == 1


# ---- the send stage and follow-up through the pipeline ---------------------------------------------------------------------------

def test_a_public_reply_asking_the_requester_for_something_starts_the_waiting_clock(env):
    _, _, deps, conn = env
    r = run_ticket(conn, deps, "1018", post="public")
    assert r.status == "SENT" and r.rule_id == "no-request-on-file"
    assert conn.execute("SELECT state FROM waiting WHERE ticket_id='1018'").fetchone()[0] == "WAITING"
    r2 = run_ticket(conn, deps, "1013", post="public")
    assert r2.status == "SENT" and conn.execute("SELECT COUNT(*) FROM waiting WHERE ticket_id='1013'").fetchone()[0] == 0


def test_a_reply_the_send_stage_refuses_is_not_posted(env, monkeypatch):
    from helpdesk_kit import send
    _, _, deps, conn = env
    monkeypatch.setattr(send, "sanity_check", lambda text: ["a missing value leaked into the text"])
    r = run_ticket(conn, deps, "1018", post="public")
    assert r.status == "HUMAN_REVIEW_REQUIRED" and any("send blocked" in why for why in r.route_reasons)
    assert [m for m in deps.source.sent if m["public"]] == []


def test_a_reply_the_reviewer_keeps_flagging_goes_to_a_person_after_bounded_revisions(env):
    from helpdesk_kit.reply import ReviewOutcome
    _, _, deps, conn = env

    class Never:
        def review(self, reply, facts):
            return ReviewOutcome("FLAG", "tone")
    deps.reviewer = Never()
    r = run_ticket(conn, deps, "1013")
    assert r.status == "HUMAN_REVIEW_REQUIRED" and r.review == "FLAG" and r.revisions <= 2
    assert any("reply review FLAG: tone" in why for why in r.route_reasons)


# ---- a system that is down --------------------------------------------------------------------------------------------------------

def test_a_system_that_is_down_sends_the_ticket_to_a_person_with_the_reason(env):
    ms, s, _, conn = env
    deps = build_deps(Settings(**{**s.__dict__, "sessions_url": "http://127.0.0.1:9"}), live=False, use_llm=False)
    r = run_ticket(conn, deps, "1013")
    assert r.state == "lookup_failed" and r.status == "HUMAN_REVIEW_REQUIRED" and "sessions" in " ".join(r.route_reasons)


def test_an_approvals_session_that_expires_mid_run_is_renewed(env):
    ms, _, deps, conn = env
    requests.post(ms.approvals + "/internal/expire")
    assert run_ticket(conn, deps, "1013").state == "weekly_cap_exceeded"


def test_a_rerun_replaces_the_unit_rows_instead_of_leaving_stale_ones(env):
    _, _, deps, conn = env
    run_ticket(conn, deps, "1012")
    assert len(units(conn, "1012")) == 2
    conn.execute("UPDATE unit_results SET unit=unit+5 WHERE ticket_id='1012'")        # pretend an older, longer run
    run_ticket(conn, deps, "1012")
    assert [u["unit"] for u in units(conn, "1012")] == [0, 1]
