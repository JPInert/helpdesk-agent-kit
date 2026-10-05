"""A fake back-office provisioning portal with no public API.

It stands in for the kind of internal admin site a help desk has to work through by hand:
user accounts, license / group-access jobs, and seat pools. Everything here is synthetic.

It deliberately reproduces three things that make real portals awkward to automate:

* No public API. The JSON endpoints under /internal/api/ only answer requests that carry
  the session cookie AND the X-Requested-With header the portal's own pages send, so the
  agent has to call them from inside a logged-in page.
* Opening /login logs you out. A script that "checks whether it is logged in" by visiting
  the login page destroys the session it was checking. Probe with "/" instead.
* Stale reads. The internal API serves a cached snapshot for a few seconds, so a job you
  just retried still reads as pending. Callers have to re-check until it flips.

Run:  python -m mock_portal.app      (http://127.0.0.1:8750, login demo / demo-password)
"""

from __future__ import annotations

import copy
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import (Flask, abort, jsonify, redirect, render_template_string, request,
                   session, url_for)

PROCESS_SECONDS = float(os.environ.get("PORTAL_PROCESS_SECONDS", "2"))
STALE_SECONDS = float(os.environ.get("PORTAL_STALE_SECONDS", "3"))
PORTAL_USER = os.environ.get("PORTAL_USER", "demo")
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "demo-password")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(now: datetime | None = None) -> dict:
    """Synthetic accounts, jobs and seat pools. Times are relative to `now` so a stuck job
    is always stuck no matter when the demo runs."""
    now = now or datetime.now(timezone.utc)
    this_cycle = now.strftime("%Y-%m")
    last_cycle = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    users = [
        {"id": "u1", "name": "Priya Raman", "email": "priya.raman@example-corp.test",
         "username": "praman", "status": "active", "manager": "Tom Hale"},
        {"id": "u2", "name": "Dana Whitfield", "email": "dana.whitfield@example-corp.test",
         "username": "dwhitfield", "status": "active", "manager": "Tom Hale"},
        {"id": "u3", "name": "Marcus Bell", "email": "marcus.bell@example-corp.test",
         "username": "mbell", "status": "active", "manager": "Ana Ruiz"},
        {"id": "u4", "name": "Lena Ortiz", "email": "lena.ortiz@example-corp.test",
         "username": "lortiz", "status": "suspended", "manager": "Ana Ruiz"},
        {"id": "u5", "name": "Dana Whitley", "email": "dana.whitley@example-corp.test",
         "username": "dwhitley", "status": "active", "manager": "Ana Ruiz"},
        {"id": "u6", "name": "Sam Okafor", "email": "sam.okafor@contractor-mail.test",
         "username": "sokafor", "status": "active", "manager": "Tom Hale"},
    ]
    jobs = [
        {"id": "j101", "user_id": "u1", "kind": "license", "target": "Office Suite",
         "status": "pending", "error": None, "created_at": _iso(now - timedelta(hours=3)),
         "cycle": this_cycle, "invoiced": False},
        {"id": "j102", "user_id": "u2", "kind": "group", "target": "Finance Shared Drive",
         "status": "pending", "error": None, "created_at": _iso(now - timedelta(hours=2)),
         "cycle": this_cycle, "invoiced": False},
        {"id": "j103", "user_id": "u3", "kind": "license", "target": "Design Suite",
         "status": "failed", "error": "SEAT_POOL_EXHAUSTED",
         "created_at": _iso(now - timedelta(hours=5)), "cycle": this_cycle, "invoiced": False},
        {"id": "j104", "user_id": "u1", "kind": "group", "target": "VPN Users",
         "status": "done", "error": None, "created_at": _iso(now - timedelta(days=9)),
         "cycle": this_cycle, "invoiced": False},
        {"id": "j105", "user_id": "u6", "kind": "license", "target": "Office Suite",
         "status": "done", "error": None, "created_at": _iso(now - timedelta(days=2)),
         "cycle": this_cycle, "invoiced": False},
        {"id": "j106", "user_id": "u3", "kind": "license", "target": "Office Suite",
         "status": "done", "error": None, "created_at": _iso(now - timedelta(days=35)),
         "cycle": last_cycle, "invoiced": True},
        {"id": "j107", "user_id": "u4", "kind": "license", "target": "Office Suite",
         "status": "pending", "error": None, "created_at": _iso(now - timedelta(hours=4)),
         "cycle": this_cycle, "invoiced": False},
    ]
    seats = [
        {"product": "Office Suite", "total": 50, "used": 41},
        {"product": "Design Suite", "total": 10, "used": 10},
    ]
    return {"users": users, "jobs": jobs, "seats": seats}


class Store:
    """In-memory state plus a deliberately stale snapshot for the internal API."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.data = seed()
            self.snapshot = copy.deepcopy(self.data)
            self.snapshot_at = time.monotonic()

    def _settle(self) -> None:
        """Advance queued jobs once their processing time has passed."""
        for job in self.data["jobs"]:
            if job["status"] != "queued" or time.monotonic() - job["_queued_at"] < PROCESS_SECONDS:
                continue
            pool = next((s for s in self.data["seats"] if s["product"] == job["target"]), None)
            if job["kind"] == "license" and pool and pool["used"] >= pool["total"]:
                job["status"], job["error"] = "failed", "SEAT_POOL_EXHAUSTED"
            else:
                job["status"], job["error"] = "done", None
                if job["kind"] == "license" and pool:
                    pool["used"] += 1

    def live(self) -> dict:
        with self.lock:
            self._settle()
            return self.data

    def stale(self) -> dict:
        with self.lock:
            self._settle()
            if time.monotonic() - self.snapshot_at >= STALE_SECONDS:
                self.snapshot = copy.deepcopy(self.data)
                self.snapshot_at = time.monotonic()
            return self.snapshot

    def retry(self, job_id: str) -> dict | None:
        with self.lock:
            job = next((j for j in self.data["jobs"] if j["id"] == job_id), None)
            if job is None or job["status"] not in ("pending", "failed"):
                return None
            job["status"], job["error"] = "queued", None
            job["_queued_at"] = time.monotonic()
            return job


def _public(obj):
    """Drop internal bookkeeping keys (leading underscore) before serving."""
    if isinstance(obj, list):
        return [_public(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if not k.startswith("_")}
    return obj


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{{ title }} - Provisioning Admin</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse}
td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}.pending{background:#eee}.failed{background:#fdd}
.done{background:#dfd}.queued{background:#ffd}.err{color:#b00;font-weight:600}</style></head><body>
<nav>Provisioning Admin {% if session.get('user') %}| <a href="/users">Users</a> | <a href="/seats">Seats</a>
| signed in as {{ session['user'] }}{% endif %}</nav><hr>{{ body|safe }}
<script>
// The portal's own pages call the internal API like this; nothing else is supported.
window.portalApi = (path) => fetch(path, {headers: {"X-Requested-With": "XMLHttpRequest"},
  credentials: "same-origin"}).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
</script></body></html>"""


def create_app(store: Store | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("PORTAL_SECRET_KEY") or secrets.token_hex(16)
    app.store = store or Store()

    def page(title: str, body: str):
        return render_template_string(PAGE, title=title, body=body)

    def require_login():
        if not session.get("user"):
            return redirect(url_for("login"))
        return None

    def require_internal():
        if not session.get("user") or request.headers.get("X-Requested-With") != "XMLHttpRequest":
            abort(404)

    @app.get("/")
    def home():
        return require_login() or page("Home", "<h1>Provisioning Admin</h1><p>Search users from the Users tab.</p>")

    @app.get("/login")
    def login():
        # The trap: opening the login page ends whatever session you had.
        session.clear()
        return page("Sign in", """<h1>Sign in</h1><form method="post" action="/login">
<label>Username <input name="username" id="username"></label>
<label>Password <input name="password" id="password" type="password"></label>
<button type="submit" id="signin">Sign in</button></form>""")

    @app.post("/login")
    def login_post():
        if (request.form.get("username") == PORTAL_USER
                and request.form.get("password") == PORTAL_PASSWORD):
            session["user"] = PORTAL_USER
            session["csrf"] = secrets.token_hex(8)
            return redirect(url_for("home"))
        return page("Sign in", "<p class='err'>Invalid username or password.</p><a href='/login'>Try again</a>"), 401

    @app.get("/users")
    def users():
        if (r := require_login()):
            return r
        q = (request.args.get("q") or "").lower()
        rows = [u for u in app.store.live()["users"]
                if not q or q in u["name"].lower() or q in u["email"].lower() or q in u["username"]]
        trs = "".join(f"<tr><td><a href='/users/{u['id']}'>{u['name']}</a></td><td>{u['email']}</td>"
                      f"<td>{u['status']}</td></tr>" for u in rows)
        return page("Users", f"""<h1>Users</h1><form><input name="q" value="{q}"><button>Search</button></form>
<table><tr><th>Name</th><th>Email</th><th>Status</th></tr>{trs}</table>""")

    @app.get("/users/<uid>")
    def user_page(uid):
        if (r := require_login()):
            return r
        data = app.store.live()
        u = next((u for u in data["users"] if u["id"] == uid), None)
        if not u:
            abort(404)
        rows = []
        for j in (j for j in data["jobs"] if j["user_id"] == uid):
            action = ""
            if j["status"] in ("pending", "failed"):
                action = (f"<form method='post' action='/users/{uid}/jobs/{j['id']}/retry'>"
                          f"<input type='hidden' name='csrf' value='{session['csrf']}'>"
                          f"<button class='retry' data-job='{j['id']}'>Retry</button></form>")
            err = f"<span class='err'>{j['error']}</span>" if j["error"] else ""
            rows.append(f"<tr class='{j['status']}' id='job-{j['id']}'><td>{j['id']}</td><td>{j['kind']}</td>"
                        f"<td>{j['target']}</td><td class='status'>{j['status']}</td><td>{err}</td>"
                        f"<td>{j['created_at']}</td><td>{action}</td></tr>")
        return page(u["name"], f"""<h1>{u['name']}</h1><p>{u['email']} &middot; {u['username']}
&middot; account <b id="account-status">{u['status']}</b></p>
<table><tr><th>Job</th><th>Kind</th><th>Target</th><th>Status</th><th>Error</th><th>Created</th><th></th></tr>
{''.join(rows)}</table>""")

    @app.post("/users/<uid>/jobs/<jid>/retry")
    def retry(uid, jid):
        if (r := require_login()):
            return r
        if request.form.get("csrf") != session.get("csrf"):
            abort(403)
        if app.store.retry(jid) is None:
            abort(409)
        return redirect(url_for("user_page", uid=uid))

    @app.get("/seats")
    def seats():
        if (r := require_login()):
            return r
        trs = "".join(f"<tr><td>{s['product']}</td><td>{s['used']} / {s['total']}</td></tr>"
                      for s in app.store.live()["seats"])
        return page("Seats", f"<h1>Seat pools</h1><table><tr><th>Product</th><th>Used</th></tr>{trs}</table>")

    # ---- internal JSON, only reachable from inside a logged-in page ---------------------
    @app.get("/internal/api/users")
    def api_users():
        require_internal()
        q = (request.args.get("q") or "").lower()
        users = app.store.stale()["users"]
        return jsonify(_public([u for u in users
                                if q and (q in u["name"].lower() or q == u["email"].lower()
                                          or q == u["username"])]))

    @app.get("/internal/api/users/<uid>/jobs")
    def api_jobs(uid):
        require_internal()
        return jsonify(_public([j for j in app.store.stale()["jobs"] if j["user_id"] == uid]))

    @app.get("/internal/api/seats")
    def api_seats():
        require_internal()
        return jsonify(_public(app.store.stale()["seats"]))

    @app.post("/internal/reset")
    def reset():
        # Demo/test convenience only.
        app.store.reset()
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORTAL_PORT", "8750"))
    create_app().run(host="127.0.0.1", port=port, debug=False)
