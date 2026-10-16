"""`python -m helpdesk_kit demo`: every feature, offline, against the three mock systems.

Nothing leaves the machine. The Zendesk source is the fixture folder (writes are recorded,
not sent), the classifier is the keyword classifier, vision reads a sidecar JSON, and the
reviewer is the deterministic one. Add --llm (with ANTHROPIC_API_KEY) to use Claude instead.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import db, followup, proposals, send, stats, triage
from .cli import build_deps, mock_systems, print_results
from .config import Settings
from .pipeline import run_ticket
from .reply import LintReviewer
from .revise import RuleReviser, review_and_revise

# A deliberately flawed draft, to show the reply stage catching and fixing it.
SAMPLE_DRAFT = ("Hello, your session s999 was closed at 2026-09-29 13:30 \u2014 it also shows as valid now.\n\n"
                "Steps if it still fails:\n- sign out\n- sign back in\n\nDocs: https://help.example-corp.test/sessions.")
SAMPLE_FACTS = {"record_id": "s201", "last_activity": "2026-09-29 13:30 UTC", "new_status": "valid"}


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def run_demo(base: Settings, args, use_llm: bool) -> int:
    demo_db = Path(base.db_path).with_name("demo.db")
    demo_rules = demo_db.with_name("demo-approved-rules.yaml")
    for path in [Path(str(demo_db) + x) for x in ("", "-wal", "-shm")] + [demo_rules]:
        path.unlink(missing_ok=True)

    with mock_systems() as ms:
        s = ms.settings(db_path=demo_db, approved_rules_path=demo_rules)
        deps = build_deps(s, live=False, use_llm=use_llm)
        conn = db.connect(s.db_path)
        print(f"systems registered: {', '.join(deps.systems_factory().names)} (mock, local)")

        def work(tid: str, post: str | None = None):
            return run_ticket(conn, deps, tid, apply=args.apply, approve=args.approve, post=post or args.post)

        _section("queue triage")
        items = triage.harvest(deps.source)
        fresh = triage.order(triage.unseen(conn, items))
        run_id = triage.new_run(conn, fresh)
        print(f"{len(items)} open tickets, {len(fresh)} new; run {run_id}; order: "
              + " ".join(i.ticket_id for i in fresh))
        results = triage.work(conn, deps.source, run_id, work)
        print("jobs:", json.dumps(triage.job_counts(conn, run_id)))

        _section("tickets")
        print_results(results)
        if args.show_replies:
            for r in results:
                if r.reply:
                    print(f"\n--- #{r.ticket_id} ({r.status}, review {r.review}) ---\n{r.reply}")

        _section("field-level findings (ticket claim vs system record)")
        for f in conn.execute("SELECT ticket_id, field, claimed, actual, extra_json FROM findings "
                              "WHERE extra_json LIKE '%\"system\"%' ORDER BY ticket_id, field"):
            system = json.loads(f["extra_json"]).get("system")
            print(f"#{f['ticket_id']}: ticket says {f['claimed']}, {system} says {f['actual']} ({f['field']})")

        _section("fix safety")
        solved = conn.execute("SELECT COUNT(*) FROM fix_ledger WHERE outcome='APPLIED'").fetchone()[0]
        print(f"ledger: {conn.execute('SELECT COUNT(*) FROM fix_ledger').fetchone()[0]} record(s) claimed, {solved} fixed")
        for sk in conn.execute("SELECT ticket_id, unit, record_id, reason FROM row_skips ORDER BY id"):
            print(f"skipped #{sk['ticket_id']}.{sk['unit'] + 1} {sk['record_id'] or '-'}: {_short(sk['reason'], 88)}")

        _section("rule proposals (unseen issues)")
        for p in proposals.pending(conn):
            print(f"#{p['id']} {p['signature']} from {len(json.loads(p['tickets_json']))} ticket(s); draft:")
            print("".join("    " + ln + "\n" for ln in p["draft_yaml"].splitlines()[:8]) + "    ...")
        print("stats:", json.dumps(proposals.stats(conn)))

        _section("reply stage (built-in sample draft)")
        rr = review_and_revise(LintReviewer(), RuleReviser(), SAMPLE_DRAFT, SAMPLE_FACTS)
        print(f"reviewer verdict after {len(rr.rounds)} revision round(s): {rr.outcome.verdict}")
        html, problems = send.prepare(rr.reply, s.sign_off)
        print("send problems:", problems or "none")
        print(html)

        _section("follow-up (tickets waiting on the requester)")
        for tid in ("1018", "1019"):
            r = work(tid, "public")
            print(f"#{tid}: {r.status}, reply recorded on the ticket (not sent anywhere)")
        now = time.time()
        deps.source.simulate_requester_reply("1019", "It was filed Monday, thanks.", now + 3600)
        for label, hours in (("+49h", 49), ("+100h", 100), ("+9d", 216)):
            done = followup.run(conn, deps.source, s.sign_off, now=now + hours * 3600)
            print(label, json.dumps(done) if done else "nothing due")
        again = triage.unseen(conn, triage.harvest(deps.source))
        print("queue after the requester answered:", [i.ticket_id for i in again] or "nothing new")

        _section("stats")
        print(stats.render(conn))
        print("\nreview dashboard: DASHBOARD_TOKEN=<pick one> python -m helpdesk_kit dashboard --db data/demo.db")
    return 0


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."
