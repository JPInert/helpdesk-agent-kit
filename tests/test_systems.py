"""Several systems per ticket (inventory item 1): the registry, the adapters and their session quirks.

The two new adapters talk plain HTTP to mock systems on local ports, so these tests need no browser.
"""

from __future__ import annotations

import pytest
import requests

from helpdesk_kit import db
from helpdesk_kit.classify import Classification, TicketType
from helpdesk_kit.cli import mock_systems
from helpdesk_kit.config import Settings
from helpdesk_kit.enrich import run as enrich_run
from helpdesk_kit.facts import Facts
from helpdesk_kit.systems import REGISTRY, AdapterError, Subject, SystemAdapter, SystemSet, register
from helpdesk_kit.systems.activity import SessionLogAdapter
from helpdesk_kit.systems.entitlements import ApprovalsAdapter

ZWSP = "\u200b"


@pytest.fixture
def ms(monkeypatch):
    import mock_portal.sessions_app as sessions_app
    monkeypatch.setattr(sessions_app, "PROCESS_SECONDS", 0.3)
    with mock_systems() as m:
        yield m


def approvals(ms) -> ApprovalsAdapter:
    return ApprovalsAdapter(ms.approvals, "demo", "demo-password")


def sessions(ms) -> SessionLogAdapter:
    return SessionLogAdapter(ms.sessions, "demo", "demo-password")


# ---- registry -------------------------------------------------------------------------------------------

def test_only_configured_systems_are_registered(ms):
    assert SystemSet.from_settings(Settings(portal_url="http://x")).names == ["provisioning"]
    assert SystemSet.from_settings(ms.settings()).names == ["provisioning", "approvals", "sessions"]


def test_a_system_is_only_read_for_the_ticket_types_it_serves(ms):
    s = SystemSet.from_settings(ms.settings())
    assert s.serving("SESSION_REJECTED") == ["approvals", "sessions"]
    assert s.serving("LICENSE_NOT_ASSIGNED") == ["provisioning"]
    assert s.serving("HARDWARE") == []


def test_adapters_open_lazily_and_close_together():
    log = []

    class Fake(SystemAdapter):
        name, serves = "fake", frozenset({"X"})

        def __enter__(self):
            log.append("open")
            return self

        def __exit__(self, *exc):
            log.append("close")

    systems = SystemSet({"fake": Fake()})
    with systems:
        assert log == []                       # nothing is opened until somebody asks
        assert systems.get("fake") is systems.get("fake") and log == ["open"]
        assert systems.get("nope") is None
    assert log == ["open", "close"]


def test_a_new_system_is_one_class_and_one_register_call():
    class Fake(SystemAdapter):
        name, serves = "fake", frozenset({"SESSION_REJECTED"})

        def collect(self, subject):
            f = Facts()
            f.set("fake.value", 42, self.name)
            return f

    register("fake", lambda s: Fake())
    try:
        assert "fake" in SystemSet.from_settings(Settings(portal_url="")).names
    finally:
        REGISTRY.pop("fake")


# ---- approvals: XHR header, short sessions, dirty names ---------------------------------------------------------

def test_the_approvals_api_is_invisible_without_the_xhr_header(ms):
    s = requests.Session()
    s.post(ms.approvals + "/login", data={"username": "demo", "password": "demo-password"}, allow_redirects=False)
    assert s.get(ms.approvals + "/api/approvals").status_code == 404
    assert s.get(ms.approvals + "/api/approvals", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 200


def test_approvals_match_a_dirty_holder_name_after_cleaning(ms):
    s = requests.Session()
    s.post(ms.approvals + "/login", data={"username": "demo", "password": "demo-password"})
    raw = s.get(ms.approvals + "/api/approvals", headers={"X-Requested-With": "XMLHttpRequest"}).json()["approvals"]
    assert any(ZWSP in r["holder"] for r in raw)                      # the system really does serve dirty names
    with approvals(ms) as a:
        facts = a.collect(Subject(name="Priya Raman"))
    got = facts.get("approvals")
    assert [x["id"] for x in got] == ["ap-302"]
    assert got[0]["resources"] == ["Build Server", "Finance Shared Drive"]          # NBSP gone
    assert facts.source("approvals") == "approvals"


def test_another_person_with_a_similar_name_is_not_matched(ms):
    with approvals(ms) as a:
        assert [x["id"] for x in a.collect(Subject(name="Dana Whitfield")).get("approvals")] == ["ap-303"]
        assert [x["id"] for x in a.collect(Subject(name="dana whitley")).get("approvals")] == ["ap-307"]
        assert a.collect(Subject(name="Nobody Here")).get("approvals") == []


def test_an_expired_approvals_session_is_renewed_once_and_the_call_succeeds(ms):
    with approvals(ms) as a:
        a.collect(Subject(name="Priya Raman"))
        assert a.logins == 1
        requests.post(ms.approvals + "/internal/expire")
        assert a.collect(Subject(name="Priya Raman")).get("approvals")
        assert a.logins == 2


def test_a_wrong_approvals_password_is_a_clear_error(ms):
    with ApprovalsAdapter(ms.approvals, "demo", "nope") as a, pytest.raises(AdapterError, match="login failed"):
        a.collect(Subject(name="Priya Raman"))


def test_an_unparseable_approval_is_reported_not_guessed(ms):
    import mock_portal.approvals_app as mod
    mod.APPROVALS.append({"id": "ap-999", "holder": "Zed Broken", "resources": "A", "status": "active",
                          "summary": "approved, whenever, a few hours"})
    try:
        with approvals(ms) as a:
            facts = a.collect(Subject(name="Zed Broken"))
    finally:
        mod.APPROVALS.pop()
    assert facts.get("approvals") == [] and "ap-999" in facts.get("approval_parse_errors")[0]


# ---- activity log: the login-page trap, CSRF, revalidation ----------------------------------------------------------

def test_probing_never_visits_the_login_page_which_would_sign_us_out(ms):
    with sessions(ms) as a:
        a.collect(Subject(username="mbell"))
        hits = ms.sessions_app.login_page_hits
        for _ in range(3):
            a.collect(Subject(username="mbell"))
        assert ms.sessions_app.login_page_hits == hits and a.logins == 1       # one sign-in, no more login-page visits


def test_visiting_the_login_page_really_does_sign_you_out(ms):
    with sessions(ms) as a:
        a.collect(Subject(username="mbell"))
        a.http.get(ms.sessions + "/login")                       # what a naive "am I logged in?" check does
        assert a.collect(Subject(username="mbell")).get("sessions")      # the adapter notices and signs in again
        assert a.logins == 2


def test_an_expired_log_session_is_renewed(ms):
    with sessions(ms) as a:
        a.collect(Subject(username="mbell"))
        requests.post(ms.sessions + "/internal/expire")
        assert a.collect(Subject(username="mbell")).get("sessions")
        assert a.logins == 2


def test_session_facts_carry_the_log_week_start_and_provenance(ms):
    with sessions(ms) as a:
        facts = a.collect(Subject(username="praman"))
    assert {s["id"] for s in facts.get("sessions")} == {"s210", "s211", "s212"}
    assert facts.get("log_week_start_day") == "Monday" and facts.source("sessions") == "sessions"


def test_collect_needs_a_username(ms):
    with sessions(ms) as a, pytest.raises(AdapterError, match="username"):
        a.collect(Subject(name="x"))


def test_closing_a_session_needs_the_csrf_header_and_settles_through_revalidating(ms):
    with sessions(ms) as a:
        a.ensure_session()
        r = requests.Session()
        r.post(ms.sessions + "/login", data={"username": "demo", "password": "demo-password"}, allow_redirects=False)
        hdr = {"X-Requested-With": "XMLHttpRequest"}
        assert r.post(ms.sessions + "/api/sessions/s201/close", json={"end": "2026-09-29T13:30:00Z"}, headers=hdr).status_code == 403
        a.close_session("s201", "2026-09-29T13:30:00Z")
        flip = a.wait_for_flip("mbell", "s201", timeout_s=5, interval_s=0.1)
        assert flip.confirmed and flip.status == "valid"
        with pytest.raises(AdapterError, match="409"):
            a.close_session("s201", "2026-09-29T13:30:00Z")        # already has an end time


def test_a_flip_that_never_settles_is_reported_unconfirmed(ms, monkeypatch):
    import mock_portal.sessions_app as mod
    monkeypatch.setattr(mod, "PROCESS_SECONDS", 600)
    with sessions(ms) as a:
        a.close_session("s250", "2026-09-28T12:00:00Z")
        flip = a.wait_for_flip("nhaddad", "s250", timeout_s=0.4, interval_s=0.1)
    assert not flip.confirmed and flip.status == "revalidating"


# ---- enrich: one facts object, provenance per fact ---------------------------------------------------------------------

class FakeProvisioning(SystemAdapter):
    name = "provisioning"
    serves = frozenset()

    def find_users(self, query):
        return [{"id": "u3", "name": "Marcus Bell", "email": "marcus.bell@example-corp.test", "username": "mbell",
                 "status": "active", "manager": "Ana Ruiz"}]


def test_enrich_merges_every_serving_system_into_one_facts_object(ms):
    systems = SystemSet({"provisioning": FakeProvisioning(), "approvals": approvals(ms), "sessions": sessions(ms)})
    c = Classification(ticket_type=TicketType.SESSION_REJECTED, summary="x", submitter_kind="internal")
    with systems:
        e = enrich_run(db.connect(":memory:"), systems,
                       {"id": "1", "requester_email": "marcus.bell@example-corp.test", "requester_name": "Marcus Bell"}, c)
    facts = Facts.from_json(e["facts"])
    prov = facts.provenance()
    assert {k: prov[k] for k in ("person.status", "approvals", "sessions", "log_week_start_day")} == {
        "person.status": "provisioning", "approvals": "approvals", "sessions": "sessions",
        "log_week_start_day": "sessions"}
    assert [a["id"] for a in facts.get("approvals")] == ["ap-301"]
    assert {s["id"] for s in facts.get("sessions")} == {"s200", "s201", "s202"}
    assert e["system_errors"] == {}


def test_one_system_down_is_recorded_and_does_not_stop_the_others(ms):
    broken = ApprovalsAdapter("http://127.0.0.1:9", "demo", "demo-password")
    systems = SystemSet({"provisioning": FakeProvisioning(), "approvals": broken, "sessions": sessions(ms)})
    c = Classification(ticket_type=TicketType.SESSION_REJECTED, summary="x", submitter_kind="internal")
    with systems:
        e = enrich_run(db.connect(":memory:"), systems,
                       {"id": "1", "requester_email": "marcus.bell@example-corp.test", "requester_name": "Marcus Bell"}, c)
    assert "approvals" in e["system_errors"] and Facts.from_json(e["facts"]).get("sessions")
