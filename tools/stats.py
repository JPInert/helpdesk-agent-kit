"""Aggregate-only stats from the local database. Same as `python -m helpdesk_kit stats`.

    python tools/stats.py [--db data/demo.db]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpdesk_kit import db, stats  # noqa: E402
from helpdesk_kit.config import Settings  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path)
    args = ap.parse_args()
    print(stats.render(db.connect(args.db or Settings().db_path)))
