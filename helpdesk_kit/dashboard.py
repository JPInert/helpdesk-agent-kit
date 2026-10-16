"""Review dashboard: a tiny Flask app for the tickets that are waiting on a person.

    python -m helpdesk_kit dashboard --db data/demo.db        (needs DASHBOARD_TOKEN)

The queue lists every ticket the agent did not finish on its own. A ticket page shows three
columns side by side: the facts (each with the system that said it), the proposed fix and
why a person is needed, and the draft reply. A person can approve the fix, edit the draft
and send it. Approving records a per-ticket approval; the pipeline reads it on the next
`run --apply`, and safety.py still applies, so approval can never force a refused fix.

Auth. The server refuses to serve anything until DASHBOARD_TOKEN is set; there is no
default. The token travels in the `X-Dashboard-Token` header. A browser user signs in once at
/login with the same token and gets a cookie; form posts then carry a CSRF value derived
from the token. Two JSON routes are for a browser userscript that pastes canned replies, and
they accept the HEADER ONLY (a cookie is not enough), so another site can not drive them:

    GET  /api/draft/<ticket_id>        the draft as plain text and HTML, plus any sanity problems
    POST /api/draft/<ticket_id>/used   record that a person used the draft

Sending from the dashboard runs the send-stage checks (house style, sanity words, links,
sign-off) on whatever text is in the box, edited or not. Bind address is 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from flask import Flask, abort, jsonify, make_response, redirect, render_template_string, request, url_for

from . import db, proposals, safety, send
from .config import Settings
from .kb import load

OPEN_STATUSES = ("HUMAN_REVIEW_REQUIRED", "AWAITING_APPROVAL", "WOULD_APPLY", "REPLY_READY")

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>{{ title }} - Review dashboard</title>
<style>body{font-family:system-ui,sans-serif;margin:20px;color:#222}table{border-collapse:collapse}
td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}.cols{display:grid;
grid-template-columns:1fr 1fr 1fr;gap:16px}.col{border:1px solid #ddd;padding:10px;border-radius:6px}
.src{font-size:11px;background:#eef;border-radius:3px;padding:1px 5px;margin-left:6px}
textarea{width:100%;height:260px;font-family:inherit}.bad{color:#b00}.ok{color:#070}</style></head>
<body><nav><a href="{{ url_for('queue') }}">Queue</a> | <a href="{{ url_for('proposals_page') }}">Rule proposals</a></nav>
<hr>{% block body %}{% endblock %}</body></html>"""

QUEUE = BASE.replace("{% block body %}{% endblock %}", """<h1>Waiting on a person ({{ rows|length }})</h1>
<table><tr><th>Ticket</th><th>Status</th><th>Issues</th><th>Why</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('ticket', ticket_id=r.ticket_id) }}">#{{ r.ticket_id }}</a></td>
<td>{{ r.status }}</td><td>{{ r.issue_count }}</td><td>{{ r.why }}</td></tr>{% endfor %}</table>""")

TICKET = BASE.replace("{% block body %}{% endblock %}", """<h1>#{{ t.ticket_id }} {{ t.status }}</h1>
{% if msg %}<p class="{{ 'bad' if bad else 'ok' }}">{{ msg }}</p>{% endif %}
<div class="cols">
<div class="col"><h2>Facts</h2>
{% for u in units %}<h3>Unit {{ u.unit + 1 }}: {{ u.ticket_type }}{% if u.record_id %} ({{ u.record_id }}){% endif %}</h3>
<table>{% for k, f in u.facts.items() %}<tr><td>{{ k }}</td><td>{{ f.value }}<span class="src">{{ f.source }}</span></td></tr>
{% endfor %}{% for k, v in u.evidence.items() %}<tr><td>{{ k }}</td><td>{{ v }}</td></tr>{% endfor %}</table>
{% for f in u.findings %}<p>Ticket says <b>{{ f.claimed }}</b>, {{ f.system or 'record' }} says <b>{{ f.actual }}</b>
({{ f.field }})</p>{% endfor %}{% endfor %}</div>
<div class="col"><h2>Proposed fix</h2>
{% for u in units %}<p><b>Unit {{ u.unit + 1 }}</b>: state <code>{{ u.state }}</code>, rule <code>{{ u.rule_id }}</code>,
action <code>{{ u.action }}</code>, {{ u.status }}</p>
{% for why in u.reasons %}<p class="bad">{{ why }}</p>{% endfor %}{% endfor %}
<p>Approved by a person: <b>{{ 'yes (' + approved + ')' if approved else 'no' }}</b></p>
<form method="post" action="{{ url_for('approve', ticket_id=t.ticket_id) }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><button>Approve the fix</button></form>
{% if skips %}<h3>Rows skipped</h3>{% for s in skips %}<p>{{ s.record_id }}: {{ s.reason }}</p>{% endfor %}{% endif %}</div>
<div class="col"><h2>Draft reply</h2>
<form method="post" action="{{ url_for('edit', ticket_id=t.ticket_id) }}">
<input type="hidden" name="csrf" value="{{ csrf }}">
<textarea name="reply">{{ t.reply or '' }}</textarea><button>Save edit</button></form>
<form method="post" action="{{ url_for('send_reply', ticket_id=t.ticket_id) }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><button>Send to requester</button></form>
<p>Review: {{ t.review_verdict or 'none' }}{% if t.reply_edited %} (edited by a person){% endif %}</p></div></div>""")

PROPOSALS = BASE.replace("{% block body %}{% endblock %}", """<h1>Rule proposals</h1>
<p>Proposed {{ s.proposed }}, accepted {{ s.accepted }}, rejected {{ s.rejected }}, pending {{ s.pending }},
acceptance rate {{ s.acceptance_rate if s.acceptance_rate is not none else 'n/a' }}</p>
{% for p in rows %}<div class="col"><h3>#{{ p.id }} {{ p.signature }} ({{ p.tickets|length }} ticket(s))</h3>
<pre>{{ p.draft_yaml }}</pre><pre>{{ p.evidence }}</pre>
<form method="post" action="{{ url_for('proposal_decide', pid=p.id, decision='approve') }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><button>Approve (starts in approve-each-fix mode)</button></form>
<form method="post" action="{{ url_for('proposal_decide', pid=p.id, decision='reject') }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><button>Reject</button></form></div>{% endfor %}""")

LOGIN = BASE.replace("{% block body %}{% endblock %}", """<h1>Sign in</h1>
<form method="post" action="{{ url_for('login') }}"><input type="password" name="token" placeholder="dashboard token">
<button>Sign in</button></form>{% if bad %}<p class="bad">That token is not right.</p>{% endif %}""")


def create_app(db_path, source, settings: Settings) -> Flask:
    app = Flask(__name__)
    token = settings.dashboard_token
    app.config["DB_PATH"] = str(db_path)
    kb = load(settings.kb_path, settings.approved_rules_path)

    def conn():
        return db.connect(app.config["DB_PATH"])

    def csrf_value() -> str:
        return hmac.new(token.encode(), b"dashboard-csrf", hashlib.sha256).hexdigest()

    def header_ok() -> bool:
        return bool(token) and hmac.compare_digest(request.headers.get("X-Dashboard-Token", ""), token)

    def cookie_ok() -> bool:
        return bool(token) and hmac.compare_digest(request.cookies.get("hdk_token", ""), token)

    @app.before_request
    def gate():
        if not token:
            abort(503, "dashboard disabled: DASHBOARD_TOKEN is not set")
        if request.path == "/login":
            return None
        if request.path.startswith("/api/"):
            return None if header_ok() else (jsonify({"error": "missing or wrong X-Dashboard-Token"}), 401)
        if header_ok():
            return None
        if not cookie_ok():
            return redirect(url_for("login"))
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), csrf_value()):
            abort(403)
        return None

    def _row(c, ticket_id: str) -> dict:
        row = db.get(c, "proposals", "ticket_id", ticket_id)
        if not row:
            abort(404)
        return row

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("token", ""), token):
                resp = make_response(redirect(url_for("queue")))
                resp.set_cookie("hdk_token", token, httponly=True, samesite="Strict")
                return resp
            return render_template_string(LOGIN, title="Sign in", bad=True), 401
        return render_template_string(LOGIN, title="Sign in", bad=False)

    @app.get("/")
    def queue():
        c = conn()
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY updated_at",
            OPEN_STATUSES)]
        for r in rows:
            r["why"] = "; ".join(json.loads(r["route_reasons_json"]))[:140] or r["status"]
        return render_template_string(QUEUE, title="Queue", rows=rows)

    @app.get("/ticket/<ticket_id>")
    def ticket(ticket_id):
        c = conn()
        t = _row(c, ticket_id)
        units = []
        for r in c.execute("SELECT * FROM unit_results WHERE ticket_id=? ORDER BY unit", (ticket_id,)):
            units.append({"unit": r["unit"], "ticket_type": r["ticket_type"], "state": r["state"],
                          "rule_id": r["rule_id"], "action": r["action"], "status": r["status"],
                          "record_id": r["record_id"], "reasons": json.loads(r["route_reasons_json"]),
                          "evidence": json.loads(r["evidence_json"]),
                          "facts": (json.loads(r["facts_json"]) or {}).get("facts", {}),
                          "findings": json.loads(r["findings_json"])})
        appr = c.execute("SELECT approved_by FROM ticket_approvals WHERE ticket_id=?", (ticket_id,)).fetchone()
        return render_template_string(TICKET, title=f"#{ticket_id}", t=t, units=units, csrf=csrf_value(),
                                      approved=appr["approved_by"] if appr else "", skips=db.skips(c, ticket_id),
                                      msg=request.args.get("msg"), bad=request.args.get("bad") == "1")

    def _back(ticket_id, msg, bad=False):
        return redirect(url_for("ticket", ticket_id=ticket_id, msg=msg, bad=int(bad)))

    @app.post("/ticket/<ticket_id>/approve")
    def approve(ticket_id):
        c = conn()
        t = _row(c, ticket_id)
        units = c.execute("SELECT * FROM unit_results WHERE ticket_id=?", (ticket_id,)).fetchall()
        fixable = [u for u in units if u["status"] in ("AWAITING_APPROVAL", "WOULD_APPLY")]
        if not fixable:
            return _back(ticket_id, "Nothing here is waiting for a fix approval.", True)
        for u in fixable:
            rule = kb.rule(u["rule_id"])
            if not safety.may_execute_with_approval(rule, u["state"]):
                return _back(ticket_id, f"Refused: {safety.may_execute_with_approval(rule, u['state']).reason}", True)
        db.approve_ticket(c, ticket_id, "dashboard")
        db.log_event(c, "approve", True, ticket_id, by="dashboard")
        return _back(ticket_id, "Approved. Run the ticket again with --apply to execute the fix.")

    @app.post("/ticket/<ticket_id>/edit")
    def edit(ticket_id):
        c = conn()
        _row(c, ticket_id)
        reply = request.form.get("reply", "").strip()
        c.execute("UPDATE proposals SET reply=?, reply_edited=1, updated_at=? WHERE ticket_id=?",
                  (reply, time.time(), ticket_id))
        db.log_event(c, "edit", True, ticket_id)
        problems = send.sanity_check(reply)
        return _back(ticket_id, "Saved." + (f" Not sendable yet: {'; '.join(problems)}" if problems else ""),
                     bool(problems))

    @app.post("/ticket/<ticket_id>/send")
    def send_reply(ticket_id):
        c = conn()
        t = _row(c, ticket_id)
        html, problems = send.prepare(t["reply"] or "", settings.sign_off)
        if problems:
            return _back(ticket_id, f"Not sent: {'; '.join(problems)}", True)
        source.add_comment(ticket_id, html, public=True, status="pending")
        c.execute("UPDATE proposals SET status='SENT', sent_at=? WHERE ticket_id=?", (time.time(), ticket_id))
        db.log_event(c, "post", True, ticket_id, status="SENT", by="dashboard")
        return redirect(url_for("queue"))

    @app.get("/proposals")
    def proposals_page():
        c = conn()
        rows = [{**r, "tickets": json.loads(r["tickets_json"]),
                 "evidence": json.dumps(json.loads(r["evidence_json"]), indent=1, sort_keys=True)}
                for r in proposals.pending(c)]
        return render_template_string(PROPOSALS, title="Proposals", rows=rows, s=proposals.stats(c), csrf=csrf_value())

    @app.post("/proposals/<int:pid>/<decision>")
    def proposal_decide(pid, decision):
        c = conn()
        try:
            if decision == "approve":
                proposals.approve(c, pid, "dashboard", settings.approved_rules_path)
            elif decision == "reject":
                proposals.reject(c, pid, "dashboard")
            else:
                abort(404)
        except ValueError as e:
            return str(e), 409
        return redirect(url_for("proposals_page"))

    # ---- the two routes the userscript calls (header token only) -------------------------------
    @app.get("/api/draft/<ticket_id>")
    def api_draft(ticket_id):
        row = db.get(conn(), "proposals", "ticket_id", ticket_id)
        if not row or not row["reply"]:
            return jsonify({"error": "no draft for this ticket"}), 404
        html, problems = send.prepare(row["reply"], settings.sign_off)
        return jsonify({"ticket_id": ticket_id, "status": row["status"], "text": row["reply"],
                        "html": html or send.to_html(row["reply"], settings.sign_off), "problems": problems})

    @app.post("/api/draft/<ticket_id>/used")
    def api_draft_used(ticket_id):
        c = conn()
        if not db.get(c, "proposals", "ticket_id", ticket_id):
            return jsonify({"error": "unknown ticket"}), 404
        c.execute("UPDATE proposals SET status='SENT', sent_at=? WHERE ticket_id=?", (time.time(), ticket_id))
        db.log_event(c, "post", True, ticket_id, status="SENT", by="userscript")
        return jsonify({"ok": True})

    return app
