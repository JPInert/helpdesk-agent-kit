"""The activity (session) log. Plain HTTP, no browser.

Its quirks, owned here:

* Opening its login page signs you out. A script that "checks whether it is signed in" by
  visiting /login destroys the session it was checking, so the adapter probes "/" instead
  and only signs in when "/" redirects.
* Writes need a CSRF token, fetched from the JSON API after sign-in, sent as a header.
* A record you just changed shows "revalidating" for a few seconds before it settles, so a
  fix is only believed once a re-read shows the final status.
"""

from __future__ import annotations

import time

import requests

from ..classify import TicketType
from ..facts import Facts
from ..portal import FlipResult
from .base import AdapterError, Subject, SystemAdapter
from .entitlements import XHR


class SessionLogAdapter(SystemAdapter):
    name = "sessions"
    serves = frozenset({TicketType.SESSION_REJECTED.value})

    def __init__(self, base_url: str, username: str, password: str, http: requests.Session | None = None):
        self.base, self.username, self.password = base_url.rstrip("/"), username, password
        self.http = http or requests.Session()
        self.csrf = ""
        self.logins = 0

    def __exit__(self, *exc) -> None:
        self.http.close()

    def ensure_session(self) -> None:
        probe = self.http.get(self.base + "/", timeout=15, allow_redirects=False)   # NOT /login
        if probe.status_code == 200 and self.csrf:
            return
        if probe.status_code in (301, 302, 303, 307):
            r = self.http.post(self.base + "/login", data={"username": self.username, "password": self.password},
                               timeout=15, allow_redirects=False)
            if r.status_code not in (302, 303):
                raise AdapterError("activity log login failed (check SESSIONS_USER / SESSIONS_PASSWORD)")
            self.logins += 1
        self.csrf = self._raw_get("/api/csrf")["token"]

    def _raw_get(self, path: str) -> dict:
        r = self.http.get(self.base + path, headers=XHR, timeout=15)
        if r.status_code != 200:
            raise AdapterError(f"activity log answered {r.status_code} for {path}")
        return r.json()

    def _get_json(self, path: str, _retried: bool = False) -> dict:
        self.ensure_session()
        r = self.http.get(self.base + path, headers=XHR, timeout=15)
        if r.status_code == 401 and not _retried:
            self.csrf = ""
            return self._get_json(path, _retried=True)
        if r.status_code != 200:
            raise AdapterError(f"activity log answered {r.status_code} for {path}")
        return r.json()

    def sessions(self, username: str) -> dict:
        return self._get_json(f"/api/sessions?user={requests.utils.quote(username)}")

    def collect(self, subject: Subject) -> Facts:
        if not subject.username:
            raise AdapterError("no username to look sessions up by")
        data = self.sessions(subject.username)
        facts = Facts()
        facts.set("sessions", data["sessions"], self.name)
        facts.set("log_week_start_day", data["week_starts"], self.name)
        return facts

    # write
    def close_session(self, session_id: str, end_iso: str, _retried: bool = False) -> None:
        self.ensure_session()
        r = self.http.post(self.base + f"/api/sessions/{session_id}/close", json={"end": end_iso},
                           headers=XHR | {"X-CSRF-Token": self.csrf}, timeout=15)
        if r.status_code == 401 and not _retried:
            self.csrf = ""
            return self.close_session(session_id, end_iso, _retried=True)
        if r.status_code != 200:
            raise AdapterError(f"close failed for {session_id}: {r.status_code}")

    def wait_for_flip(self, username: str, session_id: str, timeout_s: float = 20, interval_s: float = 0.5) -> FlipResult:
        start = time.monotonic()
        while True:
            row = next((s for s in self.sessions(username)["sessions"] if s["id"] == session_id), {})
            if row.get("status") != "revalidating":
                return FlipResult(True, row.get("status", "missing"), row.get("reject_code"), time.monotonic() - start)
            if time.monotonic() - start >= timeout_s:
                return FlipResult(False, row.get("status", "missing"), row.get("reject_code"), time.monotonic() - start)
            time.sleep(interval_s)
