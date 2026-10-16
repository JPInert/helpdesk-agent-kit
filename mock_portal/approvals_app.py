"""A fake approvals (entitlements) system. Everything here is synthetic.

An approval lets one person use elevated access on a list of resources between two dates, up
to a weekly allowance of hours, counting weeks from a given day. Records are typed by hand,
so they look like it: free-text summaries in more than one format, and names and resources
with invisible Unicode in them (zero-width spaces, byte-order marks, non-breaking spaces).

Traps, so an adapter has to handle them:

* The JSON API answers 404 unless the request carries X-Requested-With: XMLHttpRequest.
* Sessions are short. When one expires the API answers 401 {"error": "session_expired"}.
  POST /internal/expire expires every session at once (test and demo convenience).

Run:  python -m mock_portal.approvals_app      (http://127.0.0.1:8751, login demo / demo-password)
"""

from __future__ import annotations

import os
import secrets
import time

from flask import Flask, abort, jsonify, redirect, request, session, url_for

APPROVALS_USER = os.environ.get("APPROVALS_USER", "demo")
APPROVALS_PASSWORD = os.environ.get("APPROVALS_PASSWORD", "demo-password")
SESSION_SECONDS = float(os.environ.get("APPROVALS_SESSION_SECONDS", "300"))

ZWSP, BOM, LRM, NBSP = "\u200b", "\ufeff", "\u200e", "\u00a0"

APPROVALS = [
    {"id": "ap-301", "holder": f"Marcus Bell{LRM}", "status": "active",
     "summary": "Approved 2026-09-14 to 2026-10-31, up to 10 hours per week. Week starts Monday.",
     "resources": "Build Server; Finance Shared Drive"},
    {"id": "ap-302", "holder": f"Priya{ZWSP} Raman", "status": "active",
     "summary": "Valid 21 Sep 2026 - 25 Oct 2026 | max 10 hrs/week | weeks run Monday to Sunday",
     "resources": f"Build{NBSP}Server; Finance Shared Drive"},
    {"id": "ap-303", "holder": f"Dana Whitfield{BOM}", "status": "active",
     "summary": "Approved from Sep 14, 2026 through Nov 1, 2026. Allowance: 10 h/week. Week starts: Sunday.",
     "resources": "Build Server"},
    {"id": "ap-304", "holder": "Sam Okafor", "status": "expired",
     "summary": "Approved 2026-09-21 to 2026-09-30, up to 6 hours per week. Week starts Monday.",
     "resources": "Build Server"},
    {"id": "ap-305", "holder": f"Kofi{NBSP}Mensah", "status": "active",
     "summary": "Approved 2026-09-14 to 2026-10-31, up to 20 hours per week. Week starts Monday.",
     "resources": "Build Server, Finance Shared Drive"},
    {"id": "ap-306", "holder": "Noor Haddad", "status": "active",
     "summary": "Approved 2026-09-14 to 2026-10-31, up to 10 hours per week. Week starts Monday.",
     "resources": "Build Server"},
    {"id": "ap-307", "holder": "Dana Whitley", "status": "expired",
     "summary": "Approved 2026-09-01 to 2026-09-30, up to 5 hours per week. Week starts Monday.",
     "resources": "Finance Shared Drive"},
]


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("PORTAL_SECRET_KEY") or secrets.token_hex(16)
    app.epoch = 0                      # bumped by /internal/expire to kill every live session
    app.logins = 0
    app.api_calls = 0

    def live() -> bool:
        return bool(session.get("user") and session.get("epoch") == app.epoch
                    and time.time() - session.get("t", 0) < SESSION_SECONDS)

    @app.get("/login")
    def login():
        return ('<!doctype html><title>Approvals - Sign in</title><h1>Approvals</h1>'
                '<form method="post" action="/login"><input name="username" id="username">'
                '<input name="password" id="password" type="password"><button id="signin">Sign in</button></form>')

    @app.post("/login")
    def login_post():
        if (request.form.get("username") == APPROVALS_USER and request.form.get("password") == APPROVALS_PASSWORD):
            session.update(user=APPROVALS_USER, epoch=app.epoch, t=time.time())
            app.logins += 1
            return redirect(url_for("home"))
        return "Invalid username or password.", 401

    @app.get("/")
    def home():
        if not live():
            return redirect(url_for("login"))
        rows = "".join(f"<tr><td>{a['id']}</td><td>{a['holder']}</td><td>{a['summary']}</td></tr>" for a in APPROVALS)
        return f"<!doctype html><title>Approvals</title><h1>Approvals</h1><table>{rows}</table>"

    @app.get("/api/approvals")
    def api_approvals():
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            abort(404)
        if not live():
            return jsonify({"error": "session_expired"}), 401
        app.api_calls += 1
        return jsonify({"approvals": APPROVALS})

    @app.post("/internal/expire")
    def expire():
        app.epoch += 1
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("APPROVALS_PORT", "8751")), debug=False)
