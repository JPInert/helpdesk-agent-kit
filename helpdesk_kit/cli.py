"""Command line.

  python -m helpdesk_kit demo                 run every demo ticket offline, nothing leaves the machine
  python -m helpdesk_kit demo --apply         ...and let the agent write to the mock systems
  python -m helpdesk_kit portal               run the three mock systems (8750, 8751, 8752)
  python -m helpdesk_kit run --ticket 1001    one ticket (add --live to read your real Zendesk)
  python -m helpdesk_kit queue --live         triage and work your Zendesk view (resume: --resume RUN)
  python -m helpdesk_kit followup             remind or close tickets waiting on the requester
  python -m helpdesk_kit proposals            the rule proposals queue (approve N / reject N)
  python -m helpdesk_kit dashboard            the review dashboard (needs DASHBOARD_TOKEN)
  python -m helpdesk_kit stats                aggregate counts, no names or ids
  python -m helpdesk_kit events --ticket 1001 what each stage did
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import db, followup, proposals, stats, triage
from .classify import ClaudeClassifier, KeywordClassifier
from .config import Settings, llm_available
from .kb import load
from .pipeline import Deps, Result, run_ticket
from .reply import ClaudeReviewer, LintReviewer
from .revise import ClaudeReviser, RuleReviser
from .systems import SystemSet
from .vision import ClaudeVision, SidecarVision
from .zendesk import FixtureSource, ZendeskClient


MIN_TOKEN_LENGTH = 16


def build_deps(s: Settings, live: bool, use_llm: bool) -> Deps:
    source = ZendeskClient(s.zendesk_subdomain, s.zendesk_email, s.zendesk_api_token) if live \
        else FixtureSource(s.fixtures_dir)
    if use_llm:
        classifier, vis, reviewer = ClaudeClassifier(s.model), ClaudeVision(s.model), ClaudeReviewer(s.model)
        reviser = ClaudeReviser(s.model)
    else:
        classifier, vis, reviewer = KeywordClassifier(), SidecarVision(s.fixtures_dir), LintReviewer()
        reviser = RuleReviser()
    return Deps(settings=s, source=source, classifier=classifier, vision=vis, reviewer=reviewer,
                systems_factory=lambda: SystemSet.from_settings(s),
                kb=load(s.kb_path, s.approved_rules_path), reviser=reviser)


@contextmanager
def _serve(app, port: int = 0):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@contextmanager
def mock_portal(port: int = 0):
    """Run the mock provisioning portal on a background thread; yields (base URL, Flask app)."""
    import logging
    from mock_portal.app import create_app
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app()
    with _serve(app, port) as url:
        yield url, app


@dataclass
class MockSystems:
    portal: str
    approvals: str
    sessions: str
    portal_app: object
    approvals_app: object
    sessions_app: object

    def settings(self, **kw) -> Settings:
        return Settings(portal_url=self.portal, approvals_url=self.approvals, sessions_url=self.sessions, **kw)


@contextmanager
def mock_systems():
    """All three mock systems on free ports, on background threads."""
    import logging
    from contextlib import ExitStack
    from mock_portal import approvals_app, sessions_app
    from mock_portal.app import create_app
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    apps = (create_app(), approvals_app.create_app(), sessions_app.create_app())
    with ExitStack() as stack:
        urls = [stack.enter_context(_serve(a)) for a in apps]
        yield MockSystems(*urls, *apps)


def _short(text: str, n: int = 64) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."


def print_results(results: list[Result]) -> None:
    w = (8, 21, 22, 29, 6, 22)
    print(f"{'ticket':<{w[0]}} {'type':<{w[1]}} {'state':<{w[2]}} {'rule':<{w[3]}} {'fixed':<{w[4]}} "
          f"{'status':<{w[5]}} why")
    for r in results:
        if len(r.units) <= 1:
            why = _short("; ".join(r.route_reasons))
            print(f"{r.ticket_id:<{w[0]}} {(r.ticket_type or '-'):<{w[1]}} {(r.state or '-'):<{w[2]}} "
                  f"{(r.rule_id or '-'):<{w[3]}} {('yes' if r.fixed else '-'):<{w[4]}} {r.status:<{w[5]}} {why}")
            continue
        for u in r.units:
            label = f"{r.ticket_id}.{u.unit + 1}"
            print(f"{label:<{w[0]}} {u.ticket_type:<{w[1]}} {(u.state or '-'):<{w[2]}} {(u.rule_id or '-'):<{w[3]}} "
                  f"{('yes' if u.fixed else '-'):<{w[4]}} {u.status:<{w[5]}} {_short('; '.join(u.route_reasons))}")
        merged = "one merged reply" if r.reply else "no reply"
        print(f"{'':<{w[0]}} ticket {r.ticket_id}: {r.status} ({len(r.units)} units, {merged})")


def _common(sp, name: str) -> None:
    sp.add_argument("--apply", action="store_true", help="allow writes in the back-office systems")
    sp.add_argument("--approve", action="store_true", help="human approval: apply even below the auto threshold")
    sp.add_argument("--post", choices=("none", "note", "public"), default="none")
    sp.add_argument("--llm", action="store_true", help="use Claude even in the demo (needs ANTHROPIC_API_KEY)")
    sp.add_argument("--show-replies", action="store_true")
    if name != "demo":
        sp.add_argument("--live", action="store_true", help="read your real Zendesk instead of fixtures")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="helpdesk_kit")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "run", "queue"):
        sp = sub.add_parser(name)
        _common(sp, name)
        if name == "run":
            sp.add_argument("--ticket", required=True)
        if name == "queue":
            sp.add_argument("--resume", metavar="RUN_ID", help="finish an interrupted run ('latest' for the last one)")
    sub.add_parser("portal")
    ev = sub.add_parser("events")
    ev.add_argument("--ticket", required=True)
    fu = sub.add_parser("followup")
    fu.add_argument("--live", action="store_true")
    pr = sub.add_parser("proposals")
    pr.add_argument("action", nargs="?", choices=("list", "approve", "reject"), default="list")
    pr.add_argument("id", nargs="?", type=int)
    pr.add_argument("--by", default="cli")
    dash = sub.add_parser("dashboard")
    dash.add_argument("--db", type=Path)
    dash.add_argument("--port", type=int, default=8760)
    dash.add_argument("--live", action="store_true")
    st = sub.add_parser("stats")
    st.add_argument("--db", type=Path)
    args = p.parse_args(argv)

    s = Settings()
    if args.cmd == "portal":
        import logging
        from mock_portal import approvals_app, sessions_app
        from mock_portal.app import create_app
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        with _serve(approvals_app.create_app(), 8751), _serve(sessions_app.create_app(), 8752):
            print("approvals on http://127.0.0.1:8751, activity log on http://127.0.0.1:8752 "
                  "(demo / demo-password)")
            create_app().run(host="127.0.0.1", port=int(s.portal_url.rsplit(":", 1)[-1]))
        return 0
    if args.cmd == "events":
        for e in db.events(db.connect(s.db_path), args.ticket):
            print(f"{e['stage']:<9} {'ok ' if e['ok'] else 'ERR'} {json.dumps(e['detail'])}")
        return 0
    if args.cmd == "stats":
        print(stats.render(db.connect(args.db or s.db_path)))
        return 0
    if args.cmd == "proposals":
        conn = db.connect(s.db_path)
        if args.action == "list":
            for row in proposals.pending(conn):
                print(f"#{row['id']} {row['signature']} tickets={len(json.loads(row['tickets_json']))}")
                print(row["draft_yaml"])
            print(json.dumps(proposals.stats(conn)))
        elif args.id is None:
            print("give a proposal id", file=sys.stderr)
            return 2
        elif args.action == "approve":
            print("approved as rule", proposals.approve(conn, args.id, args.by, s.approved_rules_path))
        else:
            proposals.reject(conn, args.id, args.by)
            print("rejected")
        return 0
    if args.cmd == "followup":
        deps = build_deps(s, live=args.live, use_llm=False)
        done = followup.run(db.connect(s.db_path), deps.source, s.sign_off)
        print(json.dumps(done) if done else "nothing is due")
        return 0
    if args.cmd == "dashboard":
        from .dashboard import create_app as dashboard_app
        if not s.dashboard_token:
            print("set DASHBOARD_TOKEN first; the dashboard refuses to start without one", file=sys.stderr)
            return 2
        if len(s.dashboard_token) < MIN_TOKEN_LENGTH:
            print(f"DASHBOARD_TOKEN is too short ({len(s.dashboard_token)} characters); use at least "
                  f"{MIN_TOKEN_LENGTH}, for example the output of: python -c \"import secrets; "
                  f"print(secrets.token_urlsafe(24))\"", file=sys.stderr)
            return 2
        deps = build_deps(s, live=args.live, use_llm=False)
        dashboard_app(args.db or s.db_path, deps.source, s).run(host="127.0.0.1", port=args.port)
        return 0

    live = getattr(args, "live", False)
    use_llm = args.llm or live
    if use_llm and not llm_available():
        print(("--live" if live else "--llm") + " needs ANTHROPIC_API_KEY: the offline keyword classifier "
              "only knows the demo tickets", file=sys.stderr)
        return 2
    if args.cmd == "queue" and live and not s.zendesk_view_id:
        print("queue --live needs ZENDESK_VIEW_ID (the number at the end of the view's URL)", file=sys.stderr)
        return 2

    if args.cmd == "demo":
        from .demo import run_demo
        return run_demo(s, args, use_llm)

    deps = build_deps(s, live=live, use_llm=use_llm)
    conn = db.connect(s.db_path)

    def work(tid: str) -> Result:
        return run_ticket(conn, deps, tid, apply=args.apply, approve=args.approve, post=args.post)

    if args.cmd == "run":
        results = [work(args.ticket)]
    else:
        run_id = args.resume
        if run_id == "latest":
            run_id = triage.latest_run(conn)
        if not run_id:
            items = triage.order(triage.unseen(conn, triage.harvest(deps.source, s.zendesk_view_id)))
            run_id = triage.new_run(conn, items)
            print(f"queue run {run_id}: {len(items)} tickets to work (resume with --resume {run_id})")
        results = triage.work(conn, deps.source, run_id, work)
        print("jobs:", json.dumps(triage.job_counts(conn, run_id)))
    print_results(results)
    if args.show_replies:
        for r in results:
            if r.reply:
                print(f"\n--- #{r.ticket_id} ({r.status}, review {r.review}) ---\n{r.reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
