"""Reply pipeline (inventory item 7): merge, review, revise, send, follow-up."""

from __future__ import annotations

from helpdesk_kit import db, followup, send
from helpdesk_kit.reply import (ClaudeReviewer, LintReviewer, ReviewOutcome, lint, merge_replies, ungrounded)
from helpdesk_kit.revise import MAX_REVISIONS, ClaudeReviser, RuleReviser, review_and_revise
from helpdesk_kit.zendesk import FixtureSource

from conftest import FIXTURES

FACTS = {"record_id": "s201", "last_activity": "2026-09-29 13:30 UTC", "new_status": "valid", "product": "Office Suite"}
GOOD = "Hi there,\n\nYour session s201 was closed at 2026-09-29 13:30 UTC and now shows as valid."


# ---- merge ------------------------------------------------------------------------------------------------

def test_replies_for_several_issues_become_one_message_with_one_greeting():
    merged = merge_replies(["Hi there,\n\nFirst thing is done.", "Hi there,\n\nSecond thing needs a person."])
    assert merged == "Hi there,\n\nFirst thing is done.\n\nSecond thing needs a person."
    assert merged.count("Hi there,") == 1


def test_identical_issue_replies_appear_once_and_empty_ones_vanish():
    assert merge_replies(["Hi there,\n\nSame.", "Hi there,\n\nSame.", ""]) == "Hi there,\n\nSame."
    assert merge_replies([]) == ""


# ---- review: every claim against the collected facts ----------------------------------------------------------

def test_ids_dates_and_times_must_come_from_the_facts():
    assert ungrounded(GOOD, FACTS) == []
    assert ungrounded("Your session s999 ended 2026-01-01 at 09:15.", FACTS) == ["09:15", "2026-01-01", "s999"]


def test_the_offline_reviewer_flags_an_unsupported_claim_and_names_it():
    out = LintReviewer().review("Hi there,\n\nYour session s999 was closed and now shows as valid.", FACTS)
    assert out.verdict == "FLAG" and out.categories == ["unsupported_claim"] and "s999" in out.notes


def test_recipient_fields_do_not_count_as_facts():
    out = LintReviewer().review("Hi there,\n\nTicket j123 is done, thanks for waiting today.",
                                {"requester_email": "j123@example.test"})
    assert out.verdict == "FLAG" and "j123" in out.notes


def test_the_model_reviewer_runs_the_deterministic_checks_first_and_never_calls_the_model_for_them():
    class Boom:
        class messages:
            @staticmethod
            def parse(**kw):
                raise AssertionError("the model must not be called")
    out = ClaudeReviewer("m", client=Boom()).review("Hi there,\n\nSession s999 is fine, thanks for waiting today.", FACTS)
    assert out.verdict == "FLAG"


# ---- revise: bounded retries ---------------------------------------------------------------------------------------

def test_the_offline_reviser_removes_mechanical_tells_and_unsupported_sentences():
    bad = "Hello, your session s999 was closed. It is also fine \u2014 sign in again.\n\nThanks for waiting."
    fixed = RuleReviser().revise(bad, ReviewOutcome("FLAG", "x"), FACTS)
    assert fixed.startswith("Hi there,") and "s999" not in fixed and "\u2014" not in fixed and "also" not in fixed.lower()
    assert "Thanks for waiting." in fixed


def test_a_reply_that_needs_no_change_is_left_alone():
    assert RuleReviser().revise(GOOD, ReviewOutcome("FLAG", "x"), FACTS) is None


def test_review_and_revise_fixes_a_flagged_reply_in_one_round():
    bad = "Hi there,\n\nYour session s999 was closed. Your session s201 now shows as valid."
    r = review_and_revise(LintReviewer(), RuleReviser(), bad, FACTS)
    assert r.outcome.verdict == "PASS" and len(r.rounds) == 1 and "s999" not in r.reply and "s201" in r.reply


def test_a_passing_reply_is_not_revised():
    r = review_and_revise(LintReviewer(), RuleReviser(), GOOD, FACTS)
    assert r.outcome.verdict == "PASS" and r.rounds == [] and r.reply == GOOD


def test_revision_is_bounded_and_the_reviewer_has_the_last_word():
    calls = []

    class Stubborn:
        def revise(self, reply, outcome, facts):
            calls.append(reply)
            return reply + " (revised)"           # changes the text but never fixes the problem
    r = review_and_revise(LintReviewer(), Stubborn(), "Hello there, nothing here is right today \u2014 sorry.", FACTS)
    assert len(calls) == MAX_REVISIONS == len(r.rounds) and r.outcome.blocks_send


def test_a_reviser_that_returns_nothing_ends_the_loop():
    class Silent:
        def revise(self, *a):
            return None
    r = review_and_revise(LintReviewer(), Silent(), "no greeting and far too short", FACTS)
    assert r.rounds == [] and r.outcome.verdict == "FLAG"


def test_a_review_that_did_not_run_is_not_revised():
    class Down:
        def review(self, reply, facts):
            return ReviewOutcome("ERROR", "timeout")
    calls = []

    class Reviser:
        def revise(self, *a):
            calls.append(1)
    r = review_and_revise(Down(), Reviser(), GOOD, FACTS)
    assert r.outcome.verdict == "ERROR" and calls == [] and r.outcome.blocks_send


def test_the_model_reviser_sends_the_notes_and_the_facts_and_returns_only_its_text():
    seen = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                return type("R", (), {"content": [type("B", (), {"type": "text", "text": "Hi there,\n\nFixed."})()]})()
    out = ClaudeReviser("m", client=Client()).revise("draft", ReviewOutcome("FLAG", "mentions s999"), FACTS)
    prompt = seen["messages"][0]["content"]
    assert out == "Hi there,\n\nFixed." and "mentions s999" in prompt and "record_id: s201" in prompt


def test_a_failed_model_call_means_no_revision():
    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                raise TimeoutError
    assert ClaudeReviser("m", client=Client()).revise("draft", ReviewOutcome("FLAG", "x"), FACTS) is None


# ---- send: sign-off, links, sanity words -------------------------------------------------------------------------------

SIGN = "Thanks,\nIT Help Desk"


def test_sign_off_stays_in_one_paragraph_after_a_list():
    body = "Hi there,\n\nTry these:\n- sign out\n- sign back in\n\nThanks,\n\nIT Help Desk"
    html = send.to_html(body, SIGN)
    assert html.endswith("<p>Thanks,<br>IT Help Desk</p>")
    assert html.count("Thanks,") == 1 and "<ul><li>sign out</li><li>sign back in</li></ul>" in html


def test_a_sign_off_glued_to_the_last_list_item_is_not_swallowed_by_the_list():
    html = send.to_html("Hi there,\n\n- one\n- two\nThanks,\nIT Help Desk", SIGN)
    assert "<li>two</li></ul><p>Thanks,<br>IT Help Desk</p>" in html and "<li>Thanks" not in html


def test_a_body_without_a_sign_off_gets_one():
    assert send.to_html("Hi there,\n\nDone.", SIGN) == "<p>Hi there,</p><p>Done.</p><p>Thanks,<br>IT Help Desk</p>"


def test_every_bare_url_becomes_a_hyperlink_and_punctuation_stays_outside():
    html = send.to_html("Hi there,\n\nSee https://help.example-corp.test/a?b=1&c=2, or http://x.test/y.\n- https://z.test/l", SIGN)
    assert '<a href="https://help.example-corp.test/a?b=1&amp;c=2">https://help.example-corp.test/a?b=1&amp;c=2</a>,' in html
    assert '<a href="http://x.test/y">http://x.test/y</a>.' in html
    assert '<li><a href="https://z.test/l">https://z.test/l</a></li>' in html


def test_an_existing_link_is_not_wrapped_twice():
    html = send.to_html('Hi there,\n\nSee <a href="https://a.test/x">the guide</a> or https://b.test/y', SIGN)
    assert html.count("<a ") == 2 and 'href="https://a.test/x">the guide</a>' in html


def test_everything_else_is_escaped():
    assert "&lt;script&gt;" in send.to_html("Hi there,\n\n<script>alert(1)</script>", SIGN)


def test_sanity_words_block_the_send():
    for bad, why in [("Hi there, your request is None right now, sorry.", "missing value"),
                     ("Hi there, TODO write the rest of this reply.", "draft marker"),
                     ("Hi there, welcome [Name], your session is fine.", "square-bracket"),
                     ("Hi there, {{ product }} is active again for you.", "template braces"),
                     ("Hi there, your request is undefined for now ok.", "missing value")]:
        assert any(why in p for p in send.sanity_check(bad)), bad


def test_prepare_returns_html_only_when_nothing_is_wrong():
    html, problems = send.prepare("Hi there,\n\nYour session is valid again, thanks for waiting.", SIGN)
    assert problems == [] and html.startswith("<p>Hi there,</p>")
    html, problems = send.prepare("hello \u2014 None", SIGN)
    assert html == "" and problems


def test_house_style_is_part_of_the_sanity_check():
    assert set(lint("hello \u2014 also")) <= set(send.sanity_check("hello \u2014 also"))


# ---- follow-up: tickets waiting on the requester --------------------------------------------------------------------------

H = 3600


def test_follow_up_timing_is_a_pure_function():
    row = dict(asked_at=0, last_nudge_at=None, reminders=0)
    assert followup.decide(row, False, 47 * H) == "wait"
    assert followup.decide(row, False, 48 * H) == "remind"
    assert followup.decide(row | dict(reminders=1, last_nudge_at=48 * H), False, 90 * H) == "wait"
    assert followup.decide(row | dict(reminders=1, last_nudge_at=48 * H), False, 96 * H) == "remind"
    assert followup.decide(row | dict(reminders=2, last_nudge_at=96 * H), False, 100 * H) == "wait"
    assert followup.decide(row | dict(reminders=2, last_nudge_at=96 * H), False, 8 * 24 * H) == "close"
    assert followup.decide(row, True, 0) == "replied"


def test_follow_up_sends_reminders_then_closes_out_and_then_stops():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    followup.mark_waiting(conn, "1018", asked_at=0)
    for hours, expected in [(10, []), (49, ["remind"]), (60, []), (100, ["remind"]), (120, []), (200, ["close"]), (400, [])]:
        assert [d["action"] for d in followup.run(conn, source, SIGN, now=hours * H)] == expected, hours
    assert len(source.sent) == 3 and all(m["public"] for m in source.sent)
    assert "Thanks,<br>IT Help Desk" in source.sent[0]["html_body"]
    assert conn.execute("SELECT state, reminders FROM waiting").fetchone()[:] == ("EXPIRED", 2)


def test_a_requester_answer_releases_the_ticket_and_the_seen_registry():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    from helpdesk_kit import triage
    triage.mark_seen(conn, "1019", 1, "SENT")
    followup.mark_waiting(conn, "1019", asked_at=100)
    source.simulate_requester_reply("1019", "It was filed on Monday.", at=100 + H)
    assert [d["action"] for d in followup.run(conn, source, SIGN, now=100 + 200 * H)] == ["replied"]
    assert source.sent == [] and conn.execute("SELECT state FROM waiting").fetchone()[0] == "REPLIED"
    assert not triage.is_seen(conn, "1019", 1)


def test_an_answer_from_somebody_else_is_not_the_requester_answering():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    followup.mark_waiting(conn, "1011", asked_at=0)        # 1011 has an internal agent comment, not a requester one
    assert followup.requester_replied(source, "1011", since=0) is False


def test_a_reminder_that_fails_the_sanity_check_is_not_sent(monkeypatch):
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    monkeypatch.setattr(followup, "REMINDER", "Hi there, None of this is ready yet, sorry about it.")
    followup.mark_waiting(conn, "1018", asked_at=0)
    assert followup.run(conn, source, SIGN, now=60 * H) == [] and source.sent == []


def test_hard_wrapped_template_lines_reflow_into_one_paragraph():
    html = send.to_html("Hi there,\n\nThis sentence was wrapped\nby a template at 100 columns.", SIGN)
    assert "<p>This sentence was wrapped by a template at 100 columns.</p>" in html
