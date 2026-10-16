"""The approvals (entitlements) system. Plain HTTP, no browser.

Its quirks, which this adapter owns so nothing else has to know them:

* The JSON API answers 404 unless the request carries the XHR header its own pages send.
* Sessions are short. An expired session answers 401 with {"error": "session_expired"};
  the adapter signs in again once and retries, and gives up if that fails too.
* Names and resources arrive with invisible Unicode in them, so matching is done here on
  cleaned keys, never on the raw strings.
"""

from __future__ import annotations

import requests

from ..approval import ApprovalParseError, parse_approval
from ..classify import TicketType
from ..facts import Facts
from ..textclean import name_key
from .base import AdapterError, Subject, SystemAdapter

XHR = {"X-Requested-With": "XMLHttpRequest"}


class ApprovalsAdapter(SystemAdapter):
    name = "approvals"
    serves = frozenset({TicketType.SESSION_REJECTED.value})

    def __init__(self, base_url: str, username: str, password: str, http: requests.Session | None = None):
        self.base, self.username, self.password = base_url.rstrip("/"), username, password
        self.http = http or requests.Session()
        self.logins = 0

    def __exit__(self, *exc) -> None:
        self.http.close()

    def _login(self) -> None:
        r = self.http.post(self.base + "/login", data={"username": self.username, "password": self.password},
                           timeout=15, allow_redirects=False)
        if r.status_code not in (302, 303):
            raise AdapterError("approvals system login failed (check APPROVALS_USER / APPROVALS_PASSWORD)")
        self.logins += 1

    def _get_json(self, path: str, _retried: bool = False) -> dict:
        if not self.http.cookies:
            self._login()
        r = self.http.get(self.base + path, headers=XHR, timeout=15)
        if r.status_code == 401 and not _retried:
            self.http.cookies.clear()
            self._login()
            return self._get_json(path, _retried=True)
        if r.status_code != 200:
            raise AdapterError(f"approvals system answered {r.status_code} for {path}")
        return r.json()

    def collect(self, subject: Subject) -> Facts:
        if not subject.name:
            raise AdapterError("no person name to look approvals up by")
        wanted = name_key(subject.name)
        records = self._get_json("/api/approvals")["approvals"]
        mine = [r for r in records if name_key(r.get("holder")) == wanted]
        approvals, errors = [], []
        for r in mine:
            try:
                approvals.append(parse_approval(r).to_json())
            except ApprovalParseError as e:
                errors.append(str(e))
        facts = Facts()
        facts.set("approvals", approvals, self.name)
        if errors:
            facts.set("approval_parse_errors", errors, self.name)
        return facts
