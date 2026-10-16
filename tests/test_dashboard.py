"""Review dashboard (inventory item 8): queue, side-by-side page, approve / edit / send, token header."""

from __future__ import annotations

import json
import secrets

import pytest

from helpdesk_kit import db, proposals
from helpdesk_kit.cli import build_deps
from helpdesk_kit.config import Settings
from helpdesk_kit.dashboard import create_app
from helpdesk_kit.facts import Facts
from helpdesk_kit.pipeline import run_ticket
from helpdesk_kit.zendesk import FixtureSource

from conftest import FIXTURES

TOKEN = secrets.token_urlsafe(16)          # a fresh random value every run
H = {"X-Dashboard-Token": TOKEN}


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=tmp_path / "d.db", approved_rules_path=tmp_path / "approved.yaml", dashboard_token=TOKEN)
    source = FixtureSource(FIXTURES)
    conn = db.connect(s.db_path)
    deps = build_deps(s, live=False, use_llm=False)
    deps.source = source
    run_ticket(conn, deps, "1009")         # hardware: a ready reply
    run_ticket(conn, deps, "1007")         # policy question: a person must answer
    facts = Facts()
    facts.set("person.name", "Marcus Bell", "provisioning")
    facts.set("sessions", [{"id": "s201"}], "sessions")
    db.upsert(conn, "proposals", {"ticket_id": "1012", "rule_id": "session-never-ended", "action": "close_session",
                                  "state": "record_never_ended", "confidence": "HIGH", "route_to_human": 0,
                                  "route_reasons_json": "[]", "cc_json": "[]", "status": "AWAITING_APPROVAL",
                                  "reply": "Hi there,\n\nYour session s201 now shows as valid.", "review_verdict": "PASS",
                                  "updated_at": 1.0, "issue_count": 1}, "ticket_id")
    db.upsert(conn, "unit_results", {"ticket_id": "1012", "unit": 0, "issue": 0, "ticket_type": "SESSION_REJECTED",
                                     "state": "record_never_ended", "rule_id": "session-never-ended",
                                     "action": "close_session", "status": "AWAITING_APPROVAL", "record_id": "s201",
                                     "route_reasons_json": "[]", "evidence_json": json.dumps({"resource": "Build Server"}),
                                     "facts_json": json.dumps(facts.to_json()),
                                     "findings_json": json.dumps([{"field": "hours_logged_in_week", "claimed": "8",
                                                                   "actual": "12", "system": "sessions"}]),
                                     "reply": None}, "ticket_id, unit")
    proposals.propose(conn, "1016", "SESSION_REJECTED", "overlapping_sessions", {"resource": "Build Server"}, [])
    app = create_app(s.db_path, source, s)
    return app, app.test_client(), conn, source, s


def signed_in(client):
    r = client.post("/login", data={"token": TOKEN})
    assert r.status_code == 302
    return client


def csrf_of(page: bytes) -> str:
    import re
    return re.search(rb'name="csrf" value="([0-9a-f]+)"', page).group(1).decode()


def test_nothing_is_served_without_a_token_configured(tmp_path):
    s = Settings(db_path=tmp_path / "d.db", dashboard_token="")
    app = create_app(s.db_path, FixtureSource(FIXTURES), s)
    for path in ("/", "/login", "/api/draft/1"):
        assert app.test_client().get(path, headers=H).status_code == 503


def test_the_cli_refuses_to_start_without_a_token(monkeypatch, capsys):
    from helpdesk_kit import cli
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(dashboard_token=""))
    assert cli.main(["dashboard"]) == 2 and "DASHBOARD_TOKEN" in capsys.readouterr().err


def test_the_cli_refuses_a_short_token_and_accepts_a_long_one(monkeypatch, capsys):
    from helpdesk_kit import cli
    monkeypatch.setattr(cli, "Settings", lambda: Settings(dashboard_token="x" * 15))
    assert cli.main(["dashboard"]) == 2
    err = capsys.readouterr().err
    assert "too short" in err and "15 characters" in err and "at least 16" in err
    started = []
    monkeypatch.setattr(cli, "Settings", lambda: Settings(dashboard_token="x" * 16))
    monkeypatch.setattr(cli, "build_deps", lambda *a, **k: type("D", (), {"source": None})())
    import helpdesk_kit.dashboard as dash
    monkeypatch.setattr(dash, "create_app", lambda *a: type("A", (), {"run": lambda self, **k: started.append(k)})())
    assert cli.main(["dashboard"]) == 0 and started


def test_a_browser_without_the_token_is_sent_to_the_login_page(env):
    _, client, *_ = env
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert client.post("/login", data={"token": "wrong"}).status_code == 401


def test_the_queue_lists_tickets_waiting_on_a_person_and_nothing_else(env):
    _, client, *_ = env
    page = signed_in(client).get("/").data.decode()
    assert "#1012" in page and "#1007" in page and "#1009" in page
    assert "AWAITING_APPROVAL" in page and "HUMAN_REVIEW_REQUIRED" in page


def test_the_ticket_page_shows_facts_fix_and_draft_side_by_side_with_provenance(env):
    _, client, *_ = env
    page = signed_in(client).get("/ticket/1012").data.decode()
    assert "Facts" in page and "Proposed fix" in page and "Draft reply" in page
    assert "Marcus Bell" in page and '<span class="src">provisioning</span>' in page
    assert "Ticket says <b>8</b>, sessions says <b>12</b>" in page
    assert "session-never-ended" in page and "Your session s201 now shows as valid." in page


def test_an_unknown_ticket_is_a_404(env):
    assert signed_in(env[1]).get("/ticket/nope").status_code == 404


def test_form_posts_need_the_csrf_value_when_signed_in_by_cookie(env):
    _, client, *_ = env
    signed_in(client)
    assert client.post("/ticket/1012/approve", data={}).status_code == 403
    assert client.post("/ticket/1012/approve", data={"csrf": "x"}).status_code == 403


def test_approving_records_a_per_ticket_approval_the_pipeline_reads(env):
    _, client, conn, *_ = env
    signed_in(client)
    token = csrf_of(client.get("/ticket/1012").data)
    r = client.post("/ticket/1012/approve", data={"csrf": token})
    assert r.status_code == 302 and db.is_ticket_approved(conn, "1012")
    assert not db.is_ticket_approved(conn, "1009")


def test_approval_is_refused_when_no_fix_is_waiting_or_the_guards_would_refuse(env):
    _, client, conn, *_ = env
    signed_in(client)
    token = csrf_of(client.get("/ticket/1009").data)
    client.post("/ticket/1009/approve", data={"csrf": token}, follow_redirects=False)
    assert not db.is_ticket_approved(conn, "1009")
    conn.execute("UPDATE unit_results SET state='weekly_cap_exceeded' WHERE ticket_id='1012'")
    client.post("/ticket/1012/approve", data={"csrf": token})
    assert not db.is_ticket_approved(conn, "1012")


def test_editing_saves_the_draft_and_flags_a_draft_that_could_not_be_sent(env):
    _, client, conn, *_ = env
    signed_in(client)
    token = csrf_of(client.get("/ticket/1009").data)
    r = client.post("/ticket/1009/edit", data={"csrf": token, "reply": "Hi there,\n\nEdited by a person, thanks for waiting."},
                    follow_redirects=True)
    assert b"Saved." in r.data and b"edited by a person" in r.data
    row = db.get(conn, "proposals", "ticket_id", "1009")
    assert row["reply"].startswith("Hi there,\n\nEdited") and row["reply_edited"] == 1
    r = client.post("/ticket/1009/edit", data={"csrf": token, "reply": "oops \u2014 None"}, follow_redirects=True)
    assert b"Not sendable yet" in r.data


def test_sending_posts_the_checked_html_publicly_and_closes_the_queue_entry(env):
    _, client, conn, source, _ = env
    signed_in(client)
    token = csrf_of(client.get("/ticket/1009").data)
    client.post("/ticket/1009/edit", data={"csrf": token, "reply": "Hi there,\n\nSee https://help.example-corp.test/printers for status."})
    r = client.post("/ticket/1009/send", data={"csrf": token})
    assert r.status_code == 302
    sent = source.sent[-1]
    assert sent["public"] and '<a href="https://help.example-corp.test/printers">' in sent["html_body"]
    assert sent["html_body"].endswith("<p>Thanks,<br>IT Help Desk</p>")
    assert db.get(conn, "proposals", "ticket_id", "1009")["status"] == "SENT"
    assert "#1009" not in client.get("/").data.decode()


def test_a_reply_that_fails_the_checks_is_not_sent(env):
    _, client, conn, source, _ = env
    signed_in(client)
    token = csrf_of(client.get("/ticket/1009").data)
    client.post("/ticket/1009/edit", data={"csrf": token, "reply": "Hi there,\n\nYour request is None right now, sorry."})
    before = len(source.sent)
    r = client.post("/ticket/1009/send", data={"csrf": token}, follow_redirects=True)
    assert b"Not sent" in r.data and len(source.sent) == before
    assert db.get(conn, "proposals", "ticket_id", "1009")["status"] != "SENT"


def test_the_proposals_page_approves_and_rejects(env):
    _, client, conn, _, s = env
    signed_in(client)
    page = client.get("/proposals")
    assert b"overlapping_sessions" in page.data and b"acceptance rate n/a" in page.data
    token = csrf_of(page.data)
    assert client.post("/proposals/1/approve", data={"csrf": token}).status_code == 302
    assert s.approved_rules_path.is_file() and proposals.stats(conn)["accepted"] == 1
    assert client.post("/proposals/1/reject", data={"csrf": token}).status_code == 409      # already decided


# ---- the two userscript routes: header token only -------------------------------------------------------------------

def test_userscript_routes_need_the_token_header(env):
    _, client, *_ = env
    assert client.get("/api/draft/1009").status_code == 401
    assert client.get("/api/draft/1009", headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    assert client.post("/api/draft/1009/used").status_code == 401


def test_a_signed_in_browser_cookie_does_not_open_the_api(env):
    _, client, *_ = env
    signed_in(client)
    assert client.get("/api/draft/1009").status_code == 401        # another site can not ride the cookie


def test_the_draft_route_returns_text_html_and_any_problems(env):
    _, client, *_ = env
    data = client.get("/api/draft/1009", headers=H).get_json()
    assert data["text"].startswith("Hi there,") and data["html"].endswith("<p>Thanks,<br>IT Help Desk</p>")
    assert data["problems"] == [] and data["status"] == "REPLY_READY"
    assert client.get("/api/draft/nope", headers=H).status_code == 404


def test_marking_a_draft_used_closes_it_out_of_the_queue(env):
    _, client, conn, *_ = env
    assert client.post("/api/draft/1009/used", headers=H).get_json() == {"ok": True}
    assert db.get(conn, "proposals", "ticket_id", "1009")["status"] == "SENT"
    assert client.post("/api/draft/nope/used", headers=H).status_code == 404


def test_header_auth_needs_no_csrf_because_a_page_can_not_set_the_header(env):
    _, client, *_ = env
    r = client.post("/ticket/1012/approve", headers=H, data={})
    assert r.status_code == 302 and db.is_ticket_approved(env[2], "1012")
