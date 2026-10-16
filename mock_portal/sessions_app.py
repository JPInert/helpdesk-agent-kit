"""A fake activity (session) log. Everything here is synthetic.

It records elevated-access sessions (who, which resource, start, end) and validates each one
overnight. A record that fails validation shows status "rejected" with a generic reject code
and no reason; working out why is the agent's job.

Traps, so an adapter has to handle them:

* Opening /login signs you out. Probe "/" to see whether you are signed in.
* Writes need a CSRF token (GET /api/csrf) sent as X-CSRF-Token, and the XHR header.
* After a record is changed it shows "revalidating" for a few seconds before it settles.

Dates are a fixed reference week (Monday 2026-09-28 to Sunday 2026-10-04), so the diagnoses
are the same on any day the demo runs.

Run:  python -m mock_portal.sessions_app      (http://127.0.0.1:8752, login demo / demo-password)
"""

from __future__ import annotations

import copy
import os
import secrets
import threading
import time

from flask import Flask, abort, jsonify, redirect, request, session, url_for

SESSIONS_USER = os.environ.get("SESSIONS_USER", "demo")
SESSIONS_PASSWORD = os.environ.get("SESSIONS_PASSWORD", "demo-password")
PROCESS_SECONDS = float(os.environ.get("SESSIONS_PROCESS_SECONDS", "1"))
WEEK_STARTS = "Monday"


def _s(sid, user, resource, day, start, end, status="valid", last=None):
    iso = lambda hm: f"2026-{day}T{hm}:00Z" if hm else None
    return {"id": sid, "username": user, "resource": resource, "start": iso(start), "end": iso(end),
            "last_activity": iso(last or end), "status": status,
            "reject_code": "VALIDATION_FAILED" if status == "rejected" else None}


def seed() -> list[dict]:
    return [
        _s("s200", "mbell", "Build Server", "09-28", "09:00", "11:00"),
        _s("s201", "mbell", "Build Server", "09-29", "09:00", None, "rejected", last="13:30"),
        _s("s202", "mbell", "Finance Console", "09-30", "14:00", "15:00", "rejected"),
        _s("s210", "praman", "Build Server", "09-28", "09:00", "14:00"),
        _s("s211", "praman", "Build Server", "09-30", "09:00", "13:00"),
        _s("s212", "praman", "Build Server", "10-02", "09:00", "12:00", "rejected"),
        _s("s220", "dwhitfield", "Build Server", "09-30", "09:00", "12:00"),
        _s("s221", "dwhitfield", "Build Server", "10-02", "09:00", "13:00"),
        _s("s222", "dwhitfield", "Build Server", "10-04", "10:00", "14:00", "rejected"),
        _s("s230", "sokafor", "Build Server", "09-29", "10:00", "13:00"),
        _s("s231", "sokafor", "Build Server", "10-02", "10:00", "13:00", "rejected"),
        _s("s240", "kmensah", "Build Server", "10-01", "10:00", "12:00", "rejected"),
        _s("s241", "kmensah", "Build Server", "10-01", "11:00", "13:00", "rejected"),
        _s("s250", "nhaddad", "Build Server", "09-28", "08:00", None, "rejected", last="12:00"),
    ]


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("PORTAL_SECRET_KEY") or secrets.token_hex(16)
    app.lock = threading.Lock()
    app.rows = seed()
    app.epoch = 0
    app.logins = 0
    app.login_page_hits = 0

    def settle() -> None:
        for r in app.rows:
            if r["status"] == "revalidating" and time.monotonic() - r["_closed_at"] >= PROCESS_SECONDS:
                r["status"] = "valid" if r["end"] else "rejected"
                r["reject_code"] = None if r["end"] else "VALIDATION_FAILED"

    def live() -> bool:
        return bool(session.get("user") and session.get("epoch") == app.epoch)

    def xhr() -> None:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            abort(404)

    @app.get("/login")
    def login():
        session.clear()                # the trap: visiting the login page ends the session
        app.login_page_hits += 1
        return ('<!doctype html><title>Activity log - Sign in</title><h1>Activity log</h1>'
                '<form method="post" action="/login"><input name="username" id="username">'
                '<input name="password" id="password" type="password"><button id="signin">Sign in</button></form>')

    @app.post("/login")
    def login_post():
        if request.form.get("username") == SESSIONS_USER and request.form.get("password") == SESSIONS_PASSWORD:
            session.update(user=SESSIONS_USER, epoch=app.epoch, csrf=secrets.token_hex(8))
            app.logins += 1
            return redirect(url_for("home"))
        return "Invalid username or password.", 401

    @app.get("/")
    def home():
        if not live():
            return redirect(url_for("login"))
        with app.lock:
            settle()
            rows = "".join(f"<tr id='{r['id']}'><td>{r['id']}</td><td>{r['username']}</td><td>{r['resource']}</td>"
                           f"<td>{r['status']}</td></tr>" for r in app.rows)
        return f"<!doctype html><title>Activity log</title><h1>Activity log</h1><table>{rows}</table>"

    @app.get("/api/csrf")
    def csrf():
        xhr()
        if not live():
            return jsonify({"error": "signed_out"}), 401
        return jsonify({"token": session["csrf"]})

    @app.get("/api/sessions")
    def sessions():
        xhr()
        if not live():
            return jsonify({"error": "signed_out"}), 401
        user = request.args.get("user", "")
        with app.lock:
            settle()
            rows = [_public(r) for r in app.rows if r["username"] == user]
        return jsonify({"week_starts": WEEK_STARTS, "sessions": rows})

    @app.post("/api/sessions/<sid>/close")
    def close(sid):
        xhr()
        if not live():
            return jsonify({"error": "signed_out"}), 401
        if request.headers.get("X-CSRF-Token") != session.get("csrf"):
            abort(403)
        end = (request.get_json(silent=True) or {}).get("end")
        with app.lock:
            row = next((r for r in app.rows if r["id"] == sid), None)
            if row is None or row["end"] is not None or not end:
                abort(409)
            row.update(end=end, status="revalidating", _closed_at=time.monotonic())
        return jsonify({"ok": True})

    @app.post("/internal/expire")
    def expire():
        app.epoch += 1
        return jsonify({"ok": True})

    @app.post("/internal/reset")
    def reset():
        with app.lock:
            app.rows = seed()
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("SESSIONS_PORT", "8752")), debug=False)
