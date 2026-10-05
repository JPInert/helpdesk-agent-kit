"""End to end: fixture tickets -> real Playwright browser -> mock portal -> reply.

These start the mock portal on a free port and drive it with headless Chromium, so they
need `python -m playwright install chromium` once.
"""

from __future__ import annotations

import pytest

from helpdesk_kit import db
from helpdesk_kit.cli import build_deps, mock_portal
from helpdesk_kit.config import Settings
from helpdesk_kit.kb import AUTO_EXECUTE_MIN_SOLVED
from helpdesk_kit.pipeline import run_ticket
from helpdesk_kit.portal import Portal


@pytest.fixture
def env(tmp_path, monkeypatch):
    import mock_portal.app as portal_app
    monkeypatch.setattr(portal_app, "PROCESS_SECONDS", 0.5)
    monkeypatch.setattr(portal_app, "STALE_SECONDS", 1.5)
    with mock_portal() as (url, app):
        s = Settings(portal_url=url, db_path=tmp_path / "e2e.db", flip_timeout_s=4)
        deps = build_deps(s, live=False, use_llm=False)
        deps.app = app
        yield s, deps, db.connect(s.db_path)


def _job(s: Settings, user_id: str, job_id: str) -> dict:
    with Portal(s.portal_url, s.portal_user, s.portal_password) as p:
        return next(j for j in p.jobs(user_id) if j["id"] == job_id)


def test_dry_run_never_touches_the_portal(env):
    s, deps, conn = env
    r = run_ticket(conn, deps, "1001")
    assert (r.state, r.status) == ("job_stuck", "AWAITING_APPROVAL")
    assert _job(s, "u1", "j101")["status"] == "pending"
    assert deps.source.sent == []


def test_approved_fix_is_applied_confirmed_then_replied(env):
    s, deps, conn = env
    r = run_ticket(conn, deps, "1001", apply=True, approve=True, post="public")
    assert r.fixed and r.status == "SENT"
    assert "now shows as active" in r.reply
    assert _job(s, "u1", "j101")["status"] == "done"
    assert deps.source.sent[0]["public"] is True
    assert db.times_solved(conn, "license-stuck") == 1


def test_rerun_after_a_fix_sees_the_new_state(env):
    s, deps, conn = env
    run_ticket(conn, deps, "1001", apply=True, approve=True)
    conn.execute("UPDATE tickets SET duplicate_of=NULL")
    r = run_ticket(conn, deps, "1001", apply=True, approve=True)
    assert not r.fixed  # the job is done now, so nothing is retried
    assert r.state == "all_ok"


def test_auto_execute_only_after_enough_confirmed_solves(env):
    s, deps, conn = env
    for _ in range(AUTO_EXECUTE_MIN_SOLVED):
        db.mark_solved(conn, "access-stuck-for-someone")
    r = run_ticket(conn, deps, "1002", apply=True)          # no --approve
    assert r.fixed and r.rule_id == "access-stuck-for-someone"
    assert r.reply.startswith("Hi there,") and "Dana Whitfield" in r.reply


def test_a_retry_that_fails_goes_to_a_human_with_the_reason(env):
    s, deps, conn = env
    pool = next(p for p in deps.app.store.data["seats"] if p["product"] == "Office Suite")
    pool["used"] = pool["total"]                       # someone took the last seat meanwhile
    r = run_ticket(conn, deps, "1001", apply=True, approve=True)
    assert not r.fixed and r.status == "HUMAN_REVIEW_REQUIRED"
    assert any("SEAT_POOL_EXHAUSTED" in why for why in r.route_reasons)
    assert r.reply is None                             # no "it's fixed" reply was drafted


def test_unconfirmed_fix_is_not_retried_in_a_loop(env, monkeypatch):
    s, deps, conn = env
    import mock_portal.app as portal_app
    monkeypatch.setattr(portal_app, "PROCESS_SECONDS", 600)   # the portal never finishes
    r1 = run_ticket(conn, deps, "1001", apply=True, approve=True)
    assert not r1.fixed and any("not confirmed" in why for why in r1.route_reasons)
    r2 = run_ticket(conn, deps, "1001", apply=True, approve=True)
    assert any("already tried j101 once" in why for why in r2.route_reasons)


def test_screenshot_only_ticket_is_read_and_routed(env):
    s, deps, conn = env
    r = run_ticket(conn, deps, "1004")
    assert (r.ticket_type, r.state, r.rule_id) == ("ACCOUNT_SUSPENDED", "account_suspended", "account-suspended")
    assert "Ana Ruiz" in r.reply


def test_login_page_trap_is_avoided(env):
    s, _, _ = env
    with Portal(s.portal_url, s.portal_user, s.portal_password) as p:
        p.ensure_session()
        p.ensure_session()            # a second probe must not log us out
        assert p.find_users("praman")[0]["id"] == "u1"
        p.page.goto(s.portal_url + "/login")   # what a naive script does...
        assert p.find_users("praman")[0]["id"] == "u1"   # ...and the client recovers once
