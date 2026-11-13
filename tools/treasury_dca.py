#!/usr/bin/env python3
"""Daily profit dividend — Robo Trader → Treasury (3pm CT). Buys BTC-USD once per CT date.

Tb 9:39 AM CT 10/2: cutoff moved 2 PM -> 3 PM CT (DIVIDEND_CUTOFF_HOUR_CT=15). The 10/2 window
is the 25 h transition (10/1 2 PM, 10/2 3 PM]; from 10/3 every window is 3 PM -> 3 PM. Run
the routine at/after 15:00 CT (a 14:00 run now returns too_early). See snowball/treasury/cutoff.py.

Tb's routine (daily, 15:00 America/Chicago — was 14:00, weekends included):

    docker exec snowball python /app/tools/treasury_dca.py --execute

Rule (see snowball/treasury/dca.py): window (yesterday's cutoff, today's cutoff] CT;
net = live closed realized P/L in the window across all lanes (unrealized never
counts). Net > 0 -> buy 100% of net (+ carry), capped at free USD, kind
``dividend``; below Coinbase's minimum order -> roll to the next day. Net <= 0 ->
flat $10 floor buy, kind ``red_day_floor`` (skip + log if free USD < $10;
Tb 2026-09-30: was $50 through the 9/30 dividend).

--usd is the red-day floor (default 10). Without --execute it is a dry run
(checks idempotency, window net, free USD; no order). Prints one JSON line:
status = bought | already_done | in_doubt | rolled_below_min |
skipped_insufficient_cash | too_early | dry_run | error.
Receive-only: never sells treasury BTC.

--catch-up (Task 17): with --execute, pay every unpaid dividend CT date from
2026-09-30 whose cutoff has passed (2 PM CT before 10/2, 3 PM CT from 10/2),
oldest first, each with its own window rule, per-date once-only claim, in-doubt handling and free-USD cap; low
cash stops the run and logs the remaining unpaid dates. Without --execute it
is a dry run of the same plan. status = caught_up | nothing_due | dry_run |
stopped_insufficient_cash | stopped_error. Without --catch-up: unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowball.treasury.dca import CATCH_UP_SINCE, DEFAULT_MIN_ORDER_USD, DEFAULT_USD, run_catch_up, run_daily_dca  # noqa: E402
from snowball.treasury.ledger import default_treasury_db  # noqa: E402


def _with_floor(res, floor: float) -> str:
    """Result JSON plus the red-day floor in effect (so the routine log shows it)."""
    out = json.loads(res.to_json())
    out.setdefault("red_day_floor_usd", float(floor))
    return json.dumps(out, default=str)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--usd", type=float, default=DEFAULT_USD, help=f"red-day floor USD (default {DEFAULT_USD:g})")
    p.add_argument("--execute", action="store_true", help="place the real spot buy")
    p.add_argument("--any-time", action="store_true", help="allow before the cutoff (15:00 CT; manual run)")
    p.add_argument("--db", type=Path, default=None, help="treasury sqlite (default: settings.treasury_sqlite_path)")
    p.add_argument("--catch-up", action="store_true",
                   help=f"pay every unpaid due CT date since {CATCH_UP_SINCE}, oldest first (with --execute)")
    p.add_argument("--since", default=CATCH_UP_SINCE, help=f"catch-up start CT date (never before {CATCH_UP_SINCE})")
    args = p.parse_args()

    from snowball.config import Settings
    from snowball.live import LiveBroker, parse_order_fill

    settings = Settings()
    broker_box: dict = {}

    def broker() -> LiveBroker:
        if "b" not in broker_box:
            broker_box["b"] = LiveBroker(settings)  # refuses unless MODE=live AND LIVE_ENABLED=true
        return broker_box["b"]

    def free_usd() -> float:
        return float(broker().fetch_free_usd())

    def min_order_usd() -> float:
        """Coinbase BTC-USD minimum quote size (ccxt limits.cost.min); fallback $1."""
        try:
            ex = broker()._exchange
            ex.load_markets()
            m = ex.market("BTC/USD")
            v = ((m.get("limits") or {}).get("cost") or {}).get("min")
            if v is None:
                v = (m.get("info") or {}).get("quote_min_size")
            v = float(v)
            return v if v > 0 else DEFAULT_MIN_ORDER_USD
        except Exception:  # noqa: BLE001
            return DEFAULT_MIN_ORDER_USD

    def buy(usd: float):
        order = broker().create_market_order("BTC-USD", "buy", 0.0, cost=float(usd))
        px, qty, fee = parse_order_fill(order)
        return float(qty), float(px), float(fee), str(order.get("id") or "")

    db = args.db or Path(getattr(settings, "treasury_sqlite_path", None) or default_treasury_db(ROOT / "data"))
    if args.catch_up:
        res = run_catch_up(
            db,
            data_dir=db.parent,
            usd=args.usd,
            execute=args.execute,
            since=args.since,
            free_usd=free_usd,
            buy=buy,
            min_order_usd=min_order_usd,
        )
        print(_with_floor(res, args.usd))
        return 0 if res.status in ("caught_up", "nothing_due", "dry_run", "stopped_insufficient_cash") else 1
    res = run_daily_dca(
        db,
        data_dir=db.parent,
        usd=args.usd,
        execute=args.execute,
        any_time=args.any_time,
        free_usd=free_usd,
        buy=buy,
        min_order_usd=min_order_usd,
    )
    print(_with_floor(res, args.usd))
    return 0 if res.status in ("bought", "already_done", "rolled_below_min", "skipped_insufficient_cash", "too_early", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
