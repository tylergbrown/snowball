#!/usr/bin/env python3
"""Run one Forward Guidance forecast now (idempotent per CT day: the first forecast of the day stands;
the run summary, grading and calibration refresh). Usage (in the container): python tools/forward_guidance_run.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

for p in ("/app",):
    if Path(p, "snowball").is_dir() and p not in sys.path:
        sys.path.insert(0, p)

from snowball.config import Settings  # noqa: E402
from snowball.forward.report import compact  # noqa: E402
from snowball.forward.runner import run_forward_guidance  # noqa: E402

if __name__ == "__main__":
    s = Settings()
    out = run_forward_guidance(s)
    c = compact(out)
    print(json.dumps({"run_date": c["run_date"], "inserted": out.get("inserted"), "graded_now": out.get("graded_now"),
                      "elapsed_s": out.get("elapsed_s"), "errors": out.get("errors"),
                      "regimes": {r["market"]: {h: (r[h] or {}).get("regime") for h in ("1d", "1w", "1m")} for r in c["regimes"]},
                      "spot_gate": c["spot_gate"]}, default=str, indent=1))
