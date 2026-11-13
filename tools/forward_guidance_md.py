#!/usr/bin/env python3
"""Emit the Forward Guidance Obsidian notes (Agent Writing Standard naming + frontmatter, no secrets).

In the container (writes under /app/reports/obsidian = /DATA/AppData/snowball/reports/obsidian on Brown-02):
  python tools/forward_guidance_md.py [--date YYYY-MM-DD] [--out DIR] [--tar]
Produces, relative to --out (default /app/reports/obsidian):
  Forward Guidance/YYYY/MM/YYYY-MM-DD Forward Guidance.md
  Forward Guidance/Forward Guidance Scorecard.md
--tar also writes fg-notes-YYYY-MM-DD.tgz (only those two paths) for copying into the vault folder
00 Inbox/Agents/Snowball/ with tools/fg_vault_install.sh on the box.
"""
from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

for p in ("/app",):
    if Path(p, "snowball").is_dir() and p not in sys.path:
        sys.path.insert(0, p)

from snowball.config import Settings  # noqa: E402
from snowball.forward.report import emit_notes  # noqa: E402
from snowball.forward.store import ForwardStore, default_path  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Forward Guidance Obsidian notes")
    ap.add_argument("--date", help="forecast CT date (default: latest run)")
    ap.add_argument("--out", default="/app/reports/obsidian")
    ap.add_argument("--db", help="snowball_forward.db path (default from settings)")
    ap.add_argument("--tar", action="store_true")
    a = ap.parse_args()
    db = Path(a.db) if a.db else default_path(Settings())
    if not db.exists():
        sys.exit(f"no forward guidance DB at {db}")
    st = ForwardStore(db)
    try:
        summary = st.run(a.date)
        if not summary:
            sys.exit(f"no forward guidance run for {a.date or 'any date'}")
        paths = emit_notes(summary, Path(a.out), st.scorecard())
    finally:
        st.close()
    for p in paths:
        print(p)
    if a.tar:
        tgz = Path(a.out) / f"fg-notes-{summary['run_date']}.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            for p in paths:
                t.add(p, arcname=str(p.relative_to(Path(a.out))))
        print(tgz)
