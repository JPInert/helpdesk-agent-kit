"""Unit tests: each pins one rule from the README's "lessons" list."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpdesk_kit import db, ingest
from helpdesk_kit.analyze import analyze
from helpdesk_kit.classify import Classification, KeywordClassifier, TicketType, finalize, looks_abbreviated
from helpdesk_kit.kb import AUTO_EXECUTE_MIN_SOLVED, load, should_auto_execute
from helpdesk_kit.reply import ClaudeReviewer, LintReviewer, MissingEvidence, lint, render, scrub
from helpdesk_kit.vision import strip_narrated_absences

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


# ---- ingest ------------------------------------------------------------------------------

def _payload(channel="email", **ticket):
    return {"ticket": {"id": 1, "subject": "s", "description": "d", "requester_id": 7, "status": "open",
                       "created_at": "2026-10-05T09:00:00Z", "via": {"channel": channel}} | ticket,
            "users": [{"id": 7, "name": "Pat Doe", "email": "pat.doe@example-corp.test"}]}


def test_inline_screenshot_with_no_attachment_entry_still_needs_vision():
    comments = [{"author_id": 7, "body": "see below", "html_body": '<p>see</p><img src="https://x/y.png">',
                 "attachments": []}]
    assert ingest.vision_reasons(_payload()["ticket"], comments) == ["inline_image"]
    assert ingest.collect_images(comments)[0]["url"] == "https://x/y.png"


def test_messaging_ticket_uses_transcript_not_stub_description():
    comments = [{"author_id": 7, "body": "I can't sign in", "attachments": []},
                {"author_id": 7, "body": "uploaded: error.png", "attachments": []}]
    row = ingest.normalize(_payload(channel="native_messaging", description="Conversation with Web User"), comments)
    assert "I can't sign in" in row["body"] and "Conversation with" not in row["body"]
    assert json.loads(row["vision_reasons_json"]) == ["messaging_channel", "messaging_image_mention"]


def test_machine_generated_tickets_are_skipped():
    p = _payload()
    p["users"][0]["email"] = "alerts@monitoring.example-corp.test"
    assert ingest.normalize(p, [])["skipped_reason"] == "machine_generated"


def test_duplicate_from_typod_address_minutes_later():
    conn = db.connect(":memory:")
    first = ingest.normalize(_payload(description="Office Suite never arrived on my laptop"), [])
    db.upsert(conn, "tickets", first, "id")
    p = _payload(description="Office Suite never arrived on my laptop!!", id=2, created_at="2026-10-05T09:04:00Z")
    p["users"][0]["email"] = "pat.doe@example-crop.test"
    assert ingest.find_duplicate(conn, ingest.normalize(p, [])) == "1"


def test_reply_split_keeps_intent_above_the_quote():
    new, quoted = ingest.split_reply("Fixed it, thanks\n\nOn Mon, Oct 5, Report Bot wrote:\n> row 1")
    assert new == "Fixed it, thanks" and quoted.startswith("On Mon")


# ---- vision --------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("No 'Retry' button was visible.", None),
    ("Banner reads 'No seats available'.", "Banner reads 'No seats available'."),
    ("Status shows 'Pending...' since 9am. No error was shown.", "Status shows 'Pending...' since 9am."),
    ("User can't sign in. Nothing else was visible.", "User can't sign in."),
    (None, None),
])
def test_narrated_absences_are_dropped_but_quoted_screen_text_is_kept(text, expected):
    assert strip_narrated_absences(text) == expected


# ---- classify ------------------------------------------------------------------------------

@pytest.mark.parametrize("name,abbrev", [("DaWh", True), ("Dana", True), ("D. Whitfield", True),
                                         ("Dana Whitfield", False), (None, False)])
def test_abbreviated_names(name, abbrev):
    assert looks_abbreviated(name) is abbrev


def test_external_requester_cannot_file_for_someone_else():
    c = Classification(ticket_type=TicketType.ACCESS_PENDING, affected_name="Dana Whitfield", summary="x")
    t = {"requester_email": "sam@contractor-mail.test", "requester_name": "Sam Okafor"}
    c = finalize(c, t, "example-corp.test")
    assert c.submitter_kind == "external" and c.affected_name is None and not c.on_behalf


def test_classifier_reads_body_not_subject():
    t = {"subject": "License question", "body": "The 3rd floor printer is jammed", "screenshot_json": "{}",
         "thread_json": json.dumps([{"author": "x", "public": True, "text": "The 3rd floor printer is jammed"}])}
    assert KeywordClassifier().classify(t).ticket_type == TicketType.HARDWARE


# ---- analyze -------------------------------------------------------------------------------

def _c(t=TicketType.LICENSE_NOT_ASSIGNED, **kw):
    return Classification(ticket_type=t, summary="x", **kw)


def _job(status="pending", minutes=180, **kw):
    return {"id": "j1", "user_id": "u1", "kind": "license", "target": "Office Suite", "status": status,
            "error": None, "created_at": (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cycle": "2026-10", "invoiced": False} | kw


def _e(jobs=(), status="active", **kw):
    user = {"id": "u1", "name": "Pat Doe", "status": status, "manager": "Tom Hale"}
    return {"user": user, "candidates": [{"id": "u1", "name": "Pat Doe"}], "jobs": list(jobs), "seats": [],
            "error": None} | kw


def test_abbreviated_name_goes_to_a_human():
    a = analyze(_c(), {"error": "abbreviated_name"}, NOW)
    assert a.state == "unresolvable_person" and a.route_reasons


def test_two_matching_accounts_is_never_a_guess():
    a = analyze(_c(), _e(candidates=[{"id": "u1", "name": "A"}, {"id": "u5", "name": "B"}], user=None), NOW)
    assert a.state == "ambiguous_person" and a.confidence == "LOW"


def test_suspension_wins_over_a_stuck_job():
    assert analyze(_c(), _e([_job()], status="suspended"), NOW).state == "account_suspended"


def test_stuck_versus_still_processing_is_decided_by_age():
    assert analyze(_c(), _e([_job(minutes=45)]), NOW).state == "job_stuck"
    assert analyze(_c(), _e([_job(minutes=10)]), NOW).state == "job_in_progress"


def test_seat_pool_exhausted():
    e = _e([_job(status="failed", error="SEAT_POOL_EXHAUSTED")],
           seats=[{"product": "Office Suite", "used": 50, "total": 50}])
    a = analyze(_c(), e, NOW)
    assert a.state == "seat_pool_exhausted" and a.evidence["seats"] == "50 of 50"


def test_invoiced_cycle_is_locked():
    a = analyze(_c(TicketType.BILLING_QUESTION), _e([_job(status="done", invoiced=True)]), NOW)
    assert a.state == "cycle_locked" and a.route_reasons


def test_non_record_ticket_skips_record_logic():
    assert analyze(_c(TicketType.HARDWARE), None, NOW).dispatch == "non_record"


# ---- kb ----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kb():
    return load(ROOT / "kb" / "rules.yaml")


def test_more_specific_rule_wins(kb):
    assert kb.match("x", "ACCESS_PENDING", "internal", "job_stuck").id == "access-stuck-for-someone"
    assert kb.match("x", "ACCESS_PENDING", "external", "job_stuck").id == "access-stuck"


def test_keyword_rule_runs_before_record_dispatch(kb):
    assert kb.match("printer jammed", "LICENSE_NOT_ASSIGNED", "internal", "job_stuck").id == "hardware-vendor"


def test_unknown_state_falls_to_catch_all_which_only_asks(kb):
    r = kb.match("x", "ACCESS_PENDING", "internal", "ambiguous_person")
    assert r.id == "catch-all" and r.kind == "ask" and r.route_to_human


def test_catch_all_that_asserts_is_rejected_at_load(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("contacts: {}\nrules:\n  - {id: bad, kind: assert, ticket_type: '*', reply: hi}\n")
    with pytest.raises(ValueError, match="never assert"):
        load(p)


def test_unknown_cc_role_is_rejected_at_load(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("contacts: {}\nrules:\n  - {id: r, kind: ask, cc: [nobody], reply: hi}\n")
    with pytest.raises(ValueError, match="no contact"):
        load(p)


def test_auto_execute_needs_opt_in_high_confidence_and_a_track_record(kb):
    rule = next(r for r in kb.rules if r.id == "license-stuck")
    assert not should_auto_execute(rule, "HIGH", AUTO_EXECUTE_MIN_SOLVED - 1)
    assert not should_auto_execute(rule, "MEDIUM", 99)
    assert should_auto_execute(rule, "HIGH", AUTO_EXECUTE_MIN_SOLVED)
    seat_rule = next(r for r in kb.rules if r.id == "seat-pool-full")
    assert not should_auto_execute(seat_rule, "HIGH", 99)


def test_every_shipped_reply_passes_house_style(kb):
    for r in kb.rules:  # placeholders are filled later, so only that check is excused here
        assert [p for p in lint(r.reply.strip()) if p != "unrendered placeholder"] == [], r.id


# ---- reply -----------------------------------------------------------------------------------

def test_reply_will_not_claim_a_fix_the_run_did_not_confirm():
    with pytest.raises(MissingEvidence, match="new_status"):
        render("Hi there, it now shows as ${new_status}.", {"product": "X"}, ["new_status"])


def test_lint_catches_machine_tells():
    assert lint("Hello Pat, done — also fixed.") == [
        "must open with 'Hi there,'", "contains an em/en dash (reads as machine-written)",
        "contains 'also' (reads as machine-written)", "too short to be a real reply"]


def test_reviewer_never_sees_recipients():
    assert scrub({"product": "X", "requester_email": "a@b", "cc": ["c@d"]}) == {"product": "X"}


class _Boom:
    class messages:
        @staticmethod
        def parse(**kw):
            raise TimeoutError("slow")


def test_review_that_did_not_run_is_error_not_flag_and_still_blocks():
    out = ClaudeReviewer("m", client=_Boom()).review("Hi there, your request went through fine today.", {})
    assert out.verdict == "ERROR" and out.blocks_send


def test_lint_reviewer_passes_clean_text():
    assert LintReviewer().review("Hi there,\n\nYour request went through and is active now.", {}).verdict == "PASS"


# ---- db --------------------------------------------------------------------------------------

def test_one_live_run_per_worker_across_connections(tmp_path):
    a, b = db.connect(tmp_path / "x.db"), db.connect(tmp_path / "x.db")
    with db.leases(a, ["worker:0"]):
        with pytest.raises(db.LeaseHeld):
            with db.leases(b, ["worker:0"]):
                pass
    with db.leases(b, ["worker:0"]):   # released on exit
        pass


def test_a_portal_write_is_attempted_once_per_ticket_and_record():
    conn = db.connect(":memory:")
    assert db.claim_attempt(conn, "1", "j1", "retry_job")
    assert not db.claim_attempt(conn, "1", "j1", "retry_job")
    assert db.claim_attempt(conn, "2", "j1", "retry_job")


def test_json_columns_are_never_null():
    assert db.dumps(None) == "{}"


# ---- model wiring (fake client, no network) ----------------------------------------------------

class _FakeClient:
    def __init__(self, answer):
        self.calls, self._answer = [], answer
        outer = self

        class messages:
            @staticmethod
            def parse(**kw):
                outer.calls.append(kw)
                return type("R", (), {"stop_reason": "end_turn", "parsed_output": outer._answer})()
        self.messages = messages


def test_claude_classifier_asks_only_for_model_fields_and_code_fills_the_rest():
    from helpdesk_kit.classify import ClaudeClassifier, ModelAnswer
    fake = _FakeClient(ModelAnswer(ticket_type=TicketType.ACCESS_PENDING, affected_name="Dana Whitfield",
                                   summary="needs the finance drive"))
    t = {"id": "1", "subject": "s", "body": "b", "requester_email": "tom@example-corp.test",
         "requester_name": "Tom Hale", "thread_json": "[]", "screenshot_json": "{}"}
    c = finalize(ClaudeClassifier("claude-opus-5-5", client=fake).classify(t), t, "example-corp.test")
    assert fake.calls[0]["output_format"] is ModelAnswer
    assert "submitter_kind" not in ModelAnswer.model_fields
    assert c.on_behalf and c.submitter_kind == "internal"
