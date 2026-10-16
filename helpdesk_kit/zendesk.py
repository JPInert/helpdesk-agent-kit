"""Read tickets from Zendesk's public REST API (or from fixture files shaped exactly like it).

Endpoints used (all documented, all public):
  GET  /api/v2/views/{id}/tickets.json            the queue
  GET  /api/v2/tickets/{id}.json?include=users     ticket + requester in one call
  GET  /api/v2/tickets/{id}/comments.json          the whole thread, attachments included
  PUT  /api/v2/tickets/{id}.json                   add a comment (internal note by default)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Protocol

import requests


class TicketSource(Protocol):
    def view_ticket_ids(self, view_id: str, limit: int = 50) -> list[str]: ...
    def get_ticket(self, ticket_id: str) -> dict: ...
    def get_comments(self, ticket_id: str) -> list[dict]: ...
    def fetch_attachment(self, url: str) -> bytes: ...
    def add_comment(self, ticket_id: str, html_body: str, public: bool = False,
                    status: str | None = None, cc_emails: list[str] = ()) -> dict: ...


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, api_token: str, session: requests.Session | None = None):
        if not (subdomain and email and api_token):
            raise ValueError("ZENDESK_SUBDOMAIN, ZENDESK_EMAIL and ZENDESK_API_TOKEN must all be set")
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.http = session or requests.Session()
        self.http.auth = (f"{email}/token", api_token)
        self.http.headers["Accept"] = "application/json"

    def _get(self, url: str) -> dict:
        r = self.http.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def _pages(self, url: str, key: str) -> Iterator[dict]:
        while url:
            page = self._get(url)
            yield from page.get(key) or []
            url = page.get("next_page")

    def view_ticket_ids(self, view_id: str, limit: int = 50) -> list[str]:
        ids: list[str] = []
        for t in self._pages(f"{self.base}/views/{view_id}/tickets.json", "tickets"):
            if t.get("status") in ("solved", "closed", "pending"):
                continue
            ids.append(str(t["id"]))
            if len(ids) >= limit:
                break
        return ids

    def get_ticket(self, ticket_id: str) -> dict:
        return self._get(f"{self.base}/tickets/{ticket_id}.json?include=users")

    def get_comments(self, ticket_id: str) -> list[dict]:
        return list(self._pages(f"{self.base}/tickets/{ticket_id}/comments.json", "comments"))

    def fetch_attachment(self, url: str) -> bytes:
        r = self.http.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def add_comment(self, ticket_id: str, html_body: str, public: bool = False,
                    status: str | None = None, cc_emails: list[str] = ()) -> dict:
        ticket: dict = {"comment": {"html_body": html_body, "public": public}}
        if status:
            ticket["status"] = status
        if cc_emails:
            ticket["email_ccs"] = [{"user_email": e, "action": "put"} for e in cc_emails]
        r = self.http.put(f"{self.base}/tickets/{ticket_id}.json", json={"ticket": ticket}, timeout=30)
        r.raise_for_status()
        return r.json()


class FixtureSource:
    """Same interface, reads fixtures/zendesk/<id>.json. Writes are recorded, not sent."""

    def __init__(self, fixtures_dir: Path):
        self.dir = Path(fixtures_dir)
        self.sent: list[dict] = []
        self._extra: dict[str, list[dict]] = {}      # comments added at run time (simulated replies)

    def _load(self, ticket_id: str) -> dict:
        return json.loads((self.dir / "zendesk" / f"{ticket_id}.json").read_text())

    def view_ticket_ids(self, view_id: str = "", limit: int = 50) -> list[str]:
        view = json.loads((self.dir / "zendesk" / "view.json").read_text())
        return [str(t["id"]) for t in view["tickets"]
                if t.get("status") not in ("solved", "closed", "pending")][:limit]

    def get_ticket(self, ticket_id: str) -> dict:
        data = self._load(ticket_id)
        return {"ticket": data["ticket"], "users": data.get("users", [])}

    def get_comments(self, ticket_id: str) -> list[dict]:
        return self._load(ticket_id).get("comments", []) + self._extra.get(ticket_id, [])

    def simulate_requester_reply(self, ticket_id: str, text: str, at: float) -> None:
        """Demo and test helper: the requester answers the ticket at time `at` (epoch seconds)."""
        from datetime import datetime, timezone
        requester = self._load(ticket_id)["ticket"]["requester_id"]
        self._extra.setdefault(ticket_id, []).append({
            "id": 900000 + len(self._extra.get(ticket_id, [])), "author_id": requester, "public": True,
            "body": text, "plain_body": text, "html_body": f"<p>{text}</p>", "attachments": [],
            "created_at": datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def fetch_attachment(self, url: str) -> bytes:
        if not url.startswith("fixture://"):
            raise ValueError(f"fixture source cannot fetch {url}")
        return (self.dir / url.removeprefix("fixture://")).read_bytes()

    def add_comment(self, ticket_id: str, html_body: str, public: bool = False,
                    status: str | None = None, cc_emails: list[str] = ()) -> dict:
        self.sent.append({"ticket_id": ticket_id, "html_body": html_body, "public": public,
                          "status": status, "cc_emails": list(cc_emails)})
        return {"ticket": {"id": ticket_id}}
