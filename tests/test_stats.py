"""Aggregate-only stats script (inventory item 10)."""

from __future__ import annotations

import subprocess
import sys

from helpdesk_kit import db, proposals, stats
from helpdesk_kit.cli import build_deps
from helpdesk_kit.config import Settings
from helpdesk_kit.pipeline import run_ticket

from conftest import ROOT


def _populated(tmp_path):
    s = Settings(db_path=tmp_path / "s.db", approved_rules_path=tmp_path / "a.yaml")
    conn, deps = db.connect(s.db_path), build_deps(s, live=False, use_llm=False)
    for tid in ("1007", "1008", "1009"):
        run_ticket(conn, deps, tid)
    proposals.propose(conn, "1007", "SESSION_REJECTED", "overlapping_sessions", {}, [])
    db.claim_fix(conn, "sessions", "s201", "1012", "close_session")
    db.record_fix_outcome(conn, "sessions", "s201", "APPLIED")
    db.record_skip(conn, "1012", 1, "sessions", "s202", "needs a person")
    return conn


def test_counts_and_averages_by_stage(tmp_path):
    out = stats.render(_populated(tmp_path), tests=123)
    assert "tests collected: 123" in out
    assert "tickets ingested: 3 (duplicates 0, skipped 1, routed 2)" in out
    for stage in ("ingest", "classify", "route"):
        assert stage in out
    assert "fixes claimed in the ledger: 1, confirmed: 1" in out and "rows skipped with a recorded reason: 1" in out
    assert "proposed 1, accepted 0" in out and "avg per ticket" in out


def test_no_name_or_identifier_ever_appears(tmp_path):
    conn = _populated(tmp_path)
    out = stats.render(conn, tests=1)
    forbidden = ["1007", "1008", "1009", "s201", "s202", "1012", "Priya", "Marcus", "example-corp", "overlapping_sessions",
                 "needs a person", "hardware-vendor", "policy-question", "@"]
    assert [x for x in forbidden if x in out] == []


def test_collect_is_all_numbers_and_status_counts(tmp_path):
    d = stats.collect(_populated(tmp_path))
    assert d["tickets"] == 3 and d["routed"] == 2 and d["units"] == 2 and d["multi_issue"] == 0
    assert d["proposals"]["pending"] == 1


def test_an_empty_database_prints_without_dividing_by_zero(tmp_path):
    out = stats.render(db.connect(tmp_path / "empty.db"), collect_tests=False)
    assert "tests collected: n/a" in out and "issues per routed ticket (avg): n/a" in out and "acceptance rate n/a" in out


def test_the_test_count_is_what_pytest_collects():
    n = stats.count_tests(ROOT)
    assert isinstance(n, int) and n > 48


def test_the_script_runs_standalone(tmp_path):
    db.connect(tmp_path / "x.db")
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "stats.py"), "--db", str(tmp_path / "x.db")],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0 and "aggregate stats" in out.stdout and "tests collected:" in out.stdout
