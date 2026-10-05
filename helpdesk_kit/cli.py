"""Command line.

  python -m helpdesk_kit demo                 run every demo ticket offline, nothing leaves the machine
  python -m helpdesk_kit demo --apply         ...and let the agent press Retry in the mock portal
  python -m helpdesk_kit portal               just run the mock portal (http://127.0.0.1:8750)
  python -m helpdesk_kit run --ticket 1001    one ticket (add --live to read your real Zendesk)
  python -m helpdesk_kit queue --live         work your Zendesk view, oldest first
  python -m helpdesk_kit events --ticket 1001 what each stage did
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from . import db
from .classify import ClaudeClassifier, KeywordClassifier
from .config import Settings, llm_available
from .kb import load
from .pipeline import Deps, Result, run_ticket
from .portal import Portal
from .reply import ClaudeReviewer, LintReviewer
from .vision import ClaudeVision, SidecarVision
from .zendesk import FixtureSource, ZendeskClient


def build_deps(s: Settings, live: bool, use_llm: bool) -> Deps:
    source = ZendeskClient(s.zendesk_subdomain, s.zendesk_email, s.zendesk_api_token) if live \
        else FixtureSource(s.fixtures_dir)
    if use_llm:
        classifier, vis, reviewer = ClaudeClassifier(s.model), ClaudeVision(s.model), ClaudeReviewer(s.model)
    else:
        classifier, vis, reviewer = KeywordClassifier(), SidecarVision(s.fixtures_dir), LintReviewer()
    return Deps(settings=s, source=source, classifier=classifier, vision=vis, reviewer=reviewer,
                portal_factory=lambda: Portal(s.portal_url, s.portal_user, s.portal_password),
                kb=load(s.kb_path))


@contextmanager
def mock_portal(port: int = 0):
    """Run the mock portal on a background thread; yields (base URL, Flask app)."""
    import logging
    from werkzeug.serving import make_server
    from mock_portal.app import create_app
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()


def _print(results: list[Result]) -> None:
    w = (6, 21, 20, 25, 6, 22)
    print(f"{'ticket':<{w[0]}} {'type':<{w[1]}} {'state':<{w[2]}} {'rule':<{w[3]}} {'fixed':<{w[4]}} "
          f"{'status':<{w[5]}} why")
    for r in results:
        why = "; ".join(r.route_reasons)
        print(f"{r.ticket_id:<{w[0]}} {(r.ticket_type or '-'):<{w[1]}} {(r.state or '-'):<{w[2]}} "
              f"{(r.rule_id or '-'):<{w[3]}} {('yes' if r.fixed else '-'):<{w[4]}} {r.status:<{w[5]}} {why}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="helpdesk_kit")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "run", "queue"):
        sp = sub.add_parser(name)
        sp.add_argument("--apply", action="store_true", help="allow writes in the portal")
        sp.add_argument("--approve", action="store_true", help="human approval: apply even below the auto threshold")
        sp.add_argument("--post", choices=("none", "note", "public"), default="none")
        sp.add_argument("--llm", action="store_true", help="use Claude even in the demo (needs ANTHROPIC_API_KEY)")
        sp.add_argument("--show-replies", action="store_true")
        if name != "demo":
            sp.add_argument("--live", action="store_true", help="read your real Zendesk instead of fixtures")
        if name == "run":
            sp.add_argument("--ticket", required=True)
    sub.add_parser("portal")
    ev = sub.add_parser("events")
    ev.add_argument("--ticket", required=True)
    args = p.parse_args(argv)

    s = Settings()
    if args.cmd == "portal":
        from mock_portal.app import create_app
        create_app().run(host="127.0.0.1", port=int(s.portal_url.rsplit(":", 1)[-1]))
        return 0
    if args.cmd == "events":
        for e in db.events(db.connect(s.db_path), args.ticket):
            print(f"{e['stage']:<9} {'ok ' if e['ok'] else 'ERR'} {json.dumps(e['detail'])}")
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
        demo_db = Path(s.db_path).with_name("demo.db")
        for suffix in ("", "-wal", "-shm"):
            Path(str(demo_db) + suffix).unlink(missing_ok=True)
        with mock_portal() as (url, _):
            s = Settings(portal_url=url, db_path=demo_db)
            deps = build_deps(s, live=False, use_llm=use_llm)
            conn = db.connect(s.db_path)
            ids = deps.source.view_ticket_ids()
            results = [run_ticket(conn, deps, t, apply=args.apply, approve=args.approve, post=args.post)
                       for t in ids]
    else:
        deps = build_deps(s, live=args.live, use_llm=use_llm)
        conn = db.connect(s.db_path)
        ids = [args.ticket] if args.cmd == "run" else deps.source.view_ticket_ids(s.zendesk_view_id)
        results = [run_ticket(conn, deps, t, apply=args.apply, approve=args.approve, post=args.post)
                   for t in ids]

    _print(results)
    if args.show_replies:
        for r in results:
            if r.reply:
                print(f"\n--- #{r.ticket_id} ({r.status}, review {r.review}) ---\n{r.reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
