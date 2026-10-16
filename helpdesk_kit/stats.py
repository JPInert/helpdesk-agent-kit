"""Aggregate-only stats: counts and averages by stage, never names or identifiers.

Reads the local database and prints numbers. No ticket id, person, record id, reason text or
rule id is ever printed; the output is safe to paste into a report. It says how many tests
the repo collects so a stats paste also records which build produced it.

    python -m helpdesk_kit stats [--db path]        or        python tools/stats.py [--db path]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import proposals
from .config import ROOT
from .kb import AUTO_EXECUTE_MIN_SOLVED


def count_tests(root: Path = ROOT) -> int | None:
    """Collected test count from pytest itself, or None if pytest is not available."""
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=root, capture_output=True,
                             text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+) tests? collected", out) or re.search(r"^(\d+) tests", out, re.M)
    return int(m.group(1)) if m else None


def _one(conn, sql: str, *args) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0] or 0)


def _avg(total: float, n: int) -> str:
    return f"{total / n:.2f}" if n else "n/a"


def collect(conn) -> dict:
    tickets = _one(conn, "SELECT COUNT(DISTINCT ticket_id) FROM events WHERE stage='ingest'")
    stages = [dict(r) for r in conn.execute(
        "SELECT stage, COUNT(*) AS events, SUM(ok) AS ok, COUNT(DISTINCT ticket_id) AS tickets "
        "FROM events WHERE ticket_id IS NOT NULL GROUP BY stage ORDER BY MIN(id)")]
    routed = _one(conn, "SELECT COUNT(*) FROM proposals")
    units = _one(conn, "SELECT COUNT(*) FROM unit_results")
    issues = _one(conn, "SELECT COALESCE(SUM(issue_count), 0) FROM proposals")
    multi = _one(conn, "SELECT COUNT(*) FROM proposals WHERE issue_count > 1")
    return {
        "tickets": tickets,
        "duplicates": _one(conn, "SELECT COUNT(*) FROM tickets WHERE duplicate_of IS NOT NULL"),
        "skipped": _one(conn, "SELECT COUNT(*) FROM tickets WHERE skipped_reason IS NOT NULL"),
        "routed": routed, "units": units, "issues": issues, "multi_issue": multi,
        "stages": stages,
        "status": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM proposals GROUP BY status ORDER BY status")},
        "unit_status": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM unit_results GROUP BY status ORDER BY status")},
        "fixes_claimed": _one(conn, "SELECT COUNT(*) FROM fix_ledger"),
        "fixes_confirmed": _one(conn, "SELECT COUNT(*) FROM fix_ledger WHERE outcome='APPLIED'"),
        "row_skips": _one(conn, "SELECT COUNT(*) FROM row_skips"),
        "revision_rounds": _one(conn, "SELECT COALESCE(SUM(revisions), 0) FROM proposals"),
        "replies": _one(conn, "SELECT COUNT(*) FROM proposals WHERE reply IS NOT NULL"),
        "rules_tracked": _one(conn, "SELECT COUNT(*) FROM rule_stats"),
        "rules_at_gate": _one(conn, "SELECT COUNT(*) FROM rule_stats WHERE times_solved >= ?", AUTO_EXECUTE_MIN_SOLVED),
        "proposals": proposals.stats(conn),
        "queue_runs": _one(conn, "SELECT COUNT(*) FROM queue_runs"),
        "queue_jobs": {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) n FROM queue_jobs GROUP BY state ORDER BY state")},
        "seen": _one(conn, "SELECT COUNT(*) FROM seen_tickets"),
        "waiting": {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) n FROM waiting GROUP BY state ORDER BY state")},
    }


def render(conn, tests: int | None = None, collect_tests: bool = True) -> str:
    d = collect(conn)
    if tests is None and collect_tests:
        tests = count_tests()
    p = d["proposals"]
    lines = ["helpdesk-kit aggregate stats (counts and averages only; no names or identifiers)",
             f"tests collected: {tests if tests is not None else 'n/a'}",
             f"tickets ingested: {d['tickets']} (duplicates {d['duplicates']}, skipped {d['skipped']}, "
             f"routed {d['routed']})",
             f"issues per routed ticket (avg): {_avg(d['issues'], d['routed'])}; tickets with more than one issue: "
             f"{d['multi_issue']}",
             f"units per routed ticket (avg): {_avg(d['units'], d['routed'])}",
             "", f"{'stage':<18}{'events':>8}{'ok':>6}{'not ok':>8}{'avg per ticket':>16}"]
    for s in d["stages"]:
        lines.append(f"{s['stage']:<18}{s['events']:>8}{s['ok'] or 0:>6}{s['events'] - (s['ok'] or 0):>8}"
                     f"{_avg(s['events'], s['tickets']):>16}")
    lines += ["", "ticket status: " + (", ".join(f"{k} {v}" for k, v in d["status"].items()) or "none"),
              "unit status:   " + (", ".join(f"{k} {v}" for k, v in d["unit_status"].items()) or "none"),
              f"fixes claimed in the ledger: {d['fixes_claimed']}, confirmed: {d['fixes_confirmed']}",
              f"rows skipped with a recorded reason: {d['row_skips']}",
              f"reply revision rounds per reply (avg): {_avg(d['revision_rounds'], d['replies'])}",
              f"rules with a track record: {d['rules_tracked']}, at the auto gate "
              f"(>= {AUTO_EXECUTE_MIN_SOLVED} solves): {d['rules_at_gate']}",
              f"rule proposals: proposed {p['proposed']}, accepted {p['accepted']}, rejected {p['rejected']}, "
              f"pending {p['pending']}, acceptance rate "
              f"{p['acceptance_rate'] if p['acceptance_rate'] is not None else 'n/a'}",
              f"queue: {d['queue_runs']} runs, jobs " + (", ".join(f"{k} {v}" for k, v in d["queue_jobs"].items()) or "none")
              + f"; seen registry {d['seen']}",
              "waiting on requester: " + (", ".join(f"{k} {v}" for k, v in d["waiting"].items()) or "none")]
    return "\n".join(lines)
