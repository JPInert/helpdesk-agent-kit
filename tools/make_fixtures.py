"""Write the synthetic demo tickets and screenshots under fixtures/.

Every ticket is shaped exactly like Zendesk's API responses (ticket + sideloaded users +
comments), so the same ingest code reads fixtures and a live instance. All people, companies
and addresses are invented.

    python tools/make_fixtures.py          # JSON only
    python tools/make_fixtures.py --shots  # JSON + screenshots (needs playwright + chromium)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures"

USERS = {
    1: ("Priya Raman", "priya.raman@example-corp.test"),
    2: ("Tom Hale", "tom.hale@example-corp.test"),
    3: ("Marcus Bell", "marcus.bell@example-corp.test"),
    4: ("Lena Ortiz", "lena.ortiz@example-corp.test"),
    5: ("Sam Okafor", "sam.okafor@contractor-mail.test"),
    6: ("Ana Ruiz", "ana.ruiz@example-corp.test"),
    7: ("Uptime Monitor", "alerts@monitoring.example-corp.test"),
    8: ("Priya Raman", "priya.raman@example-crop.test"),   # typo'd domain, same person
    9: ("Help Desk Agent", "helpdesk@example-corp.test"),
    10: ("Dana Whitfield", "dana.whitfield@example-corp.test"),
    11: ("Noor Haddad", "noor.haddad@example-corp.test"),
}


def user(uid: int) -> dict:
    name, email = USERS[uid]
    return {"id": uid, "name": name, "email": email}


def comment(cid: int, author: int, body: str, html_body: str | None = None,
            attachments: list | None = None, public: bool = True) -> dict:
    return {"id": cid, "author_id": author, "public": public, "body": body,
            "html_body": html_body or f"<p>{body}</p>", "plain_body": body,
            "attachments": attachments or []}


def ticket(tid: int, requester: int, subject: str, description: str, created: str,
           channel: str = "email", tags: list | None = None, comments: list | None = None,
           extra_users: list | None = None, priority: str | None = None) -> dict:
    return {
        "ticket": {"id": tid, "subject": subject, "description": description,
                   "requester_id": requester, "status": "open", "created_at": created,
                   "via": {"channel": channel}, "tags": tags or [],
                   **({"priority": priority} if priority else {})},
        "users": [user(requester)] + [user(u) for u in extra_users or []],
        "comments": comments or [comment(tid * 10, requester, description)],
    }


def shot(name: str) -> dict:
    return {"content_url": f"fixture://screenshots/{name}", "content_type": "image/png",
            "file_name": name, "inline": False}


TICKETS = [
    ticket(1001, 1, "Office Suite never showed up",
           "Hi there, I requested Office Suite this morning and it still isn't on my laptop. "
           "Can someone check?", "2026-10-05T09:02:00Z"),
    ticket(1002, 2, "Access for Dana",
           "Hi there, Dana Whitfield still can't open the Finance Shared Drive. Her access request "
           "went in a couple of hours ago and she needs it for month end.", "2026-10-05T09:10:00Z"),
    ticket(1003, 3, "Design Suite",
           "My Design Suite license request failed and I need it for a client deck today.",
           "2026-10-05T09:20:00Z"),
    ticket(1004, 4, "Conversation with Lena Ortiz", "Conversation with Web User", "2026-10-05T09:31:00Z",
           channel="native_messaging", comments=[
               comment(10041, 4, "I can't get into anything since this morning"),
               comment(10042, 4, "uploaded: signin-error.png", attachments=[shot("signin-error.png")]),
           ]),
    ticket(1005, 5, "Office", "Getting this when I open Office, see below.", "2026-10-05T09:40:00Z",
           comments=[comment(10050, 5, "Getting this when I open Office, see below.",
                             html_body="<p>Getting this when I open Office, see below.</p>"
                                       "<p><img src=\"fixture://screenshots/office-activate.png\"></p>")]),
    ticket(1006, 6, "VPN", "Please sort out access for DaWh, she is stuck on the VPN group again.",
           "2026-10-05T09:45:00Z"),
    ticket(1007, 1, "MFA on personal phone",
           "Quick one: do I still need MFA on my personal phone now that I have a work laptop?",
           "2026-10-05T09:50:00Z"),
    ticket(1008, 7, "[ALERT] disk usage 91% on fileserver-02",
           "Automated alert: disk usage above threshold.", "2026-10-05T09:51:00Z", tags=["monitoring_alert"]),
    ticket(1009, 3, "License question",
           "The 3rd floor printer is jammed again and showing a paper error.", "2026-10-05T10:00:00Z"),
    ticket(1010, 8, "Office Suite never showed up",
           "Hi there, I requested Office Suite this morning and it still isn't on my laptop. "
           "Can someone check??", "2026-10-05T09:06:00Z"),
    ticket(1011, 3, "Charge on my account",
           "I was charged for Office Suite last month but I don't use it. Can that come off?",
           "2026-10-05T10:05:00Z",
           comments=[comment(10110, 3, "I was charged for Office Suite last month but I don't use it. "
                                       "Can that come off?"),
                     comment(10111, 9, "Checking with billing.", public=False)],
           extra_users=[9]),
    # ---- v2: session records, several issues in one ticket, requests with nothing on file ---------
    ticket(1012, 3, "Sessions rejected",
           "Hi there, two of my elevated access sessions from last week were rejected and I can't tell why. "
           "One was on the Build Server on Tuesday that I forgot to close out, the other on the Finance "
           "Console on Wednesday.", "2026-10-05T10:10:00Z", priority="high"),
    ticket(1013, 1, "Session rejected",
           "Hi there, my session from Friday was rejected. I only logged about 8 hours last week, well "
           "under the 10 hours a week I am approved for.", "2026-10-05T10:12:00Z"),
    ticket(1014, 10, "Session rejected as over the limit",
           "Hi there, my session from Sunday was rejected as over the weekly limit. My approval says weeks "
           "start on Sunday, so it should count toward next week.", "2026-10-05T10:14:00Z"),
    ticket(1015, 5, "Friday session rejected",
           "Hi there, my session from Friday was rejected even though my approval runs through 2026-10-31.",
           "2026-10-05T10:16:00Z", priority="urgent"),
    ticket(1016, 2, "Kofi's sessions",
           "Hi there, Kofi Mensah still has two rejected sessions from Thursday and the log will not say why.",
           "2026-10-05T10:18:00Z"),
    ticket(1017, 11, "Two problems",
           "Hi there, two problems:\n1. My Office Suite license request from this morning is still not "
           "showing up, it isn't on my laptop.\n2. My session from Monday was rejected and I can't see why.",
           "2026-10-05T10:19:00Z"),
    ticket(1018, 1, "Design Suite",
           "Hi there, I requested a Design Suite license last week and it still has not shown up.",
           "2026-10-05T10:20:00Z"),
    ticket(1019, 3, "Finance drive",
           "Hi there, I need access to the Finance Shared Drive for the audit but I am not sure anyone ever "
           "filed the request.", "2026-10-05T10:22:00Z"),
]

SCREENSHOTS = {
    "signin-error.png": (
        """<div style="font:16px system-ui;width:520px;padding:28px;border:1px solid #ccc;border-radius:8px">
<h2 style="margin:0 0 12px">Sign in</h2><div style="color:#555">lortiz@example-corp.test</div>
<div style="margin-top:16px;padding:12px;background:#fdecea;color:#a12622;border-radius:6px">
Your account has been suspended. Contact your administrator.</div></div>""",
        {"screen": "company sign-in page", "error_text": "'Your account has been suspended. Contact your administrator.'",
         "username": "lortiz", "record_id": None, "status": "suspended",
         "observations": "Sign-in form with the email filled in. No password field was visible."}),
    "office-activate.png": (
        """<div style="font:16px system-ui;width:520px;padding:28px;border:1px solid #ccc;border-radius:8px">
<h2 style="margin:0 0 12px">Office Suite</h2><p>Sign in to activate Office Suite.</p>
<button style="padding:8px 16px">Sign in</button></div>""",
        {"screen": "Office Suite activation prompt", "error_text": "'Sign in to activate Office Suite.'",
         "username": None, "record_id": None, "status": None,
         "observations": "Activation dialog asking the user to sign in. No license error was visible."}),
}


def main() -> None:
    (OUT / "zendesk").mkdir(parents=True, exist_ok=True)
    (OUT / "screenshots").mkdir(parents=True, exist_ok=True)
    view = {"tickets": []}
    for t in TICKETS:
        tid = t["ticket"]["id"]
        (OUT / "zendesk" / f"{tid}.json").write_text(json.dumps(t, indent=2) + "\n")
        view["tickets"].append({"id": tid, "status": t["ticket"]["status"]})
    # The queue is read oldest first, as a person would work it.
    view["tickets"].sort(key=lambda v: next(t["ticket"]["created_at"] for t in TICKETS if t["ticket"]["id"] == v["id"]))
    (OUT / "zendesk" / "view.json").write_text(json.dumps(view, indent=2) + "\n")
    for name, (_, fields) in SCREENSHOTS.items():
        (OUT / "screenshots" / f"{name}.json").write_text(json.dumps(fields, indent=2) + "\n")
    if "--shots" in sys.argv:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 600, "height": 260})
            for name, (markup, _) in SCREENSHOTS.items():
                page.set_content(f"<body style='margin:20px;background:#fff'>{markup}</body>")
                page.screenshot(path=str(OUT / "screenshots" / name))
            browser.close()
    print(f"wrote {len(TICKETS)} tickets, {len(SCREENSHOTS)} screenshots")


if __name__ == "__main__":
    main()
