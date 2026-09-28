#!/usr/bin/env python3
"""Record a Bitcoin treasury contribution (friday_50 / pnl_sweep / manual).

Does not touch the CRYPTO trader lane. Secrets stay in env (none required).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowball.treasury.ledger import (  # noqa: E402
    default_treasury_db,
    ensure_schema,
    record_contribution,
    snapshot,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--usd", type=float, required=True)
    p.add_argument("--btc", type=float, help="BTC qty (else usd/price)")
    p.add_argument("--price", type=float, help="BTC-USD (else live spot)")
    p.add_argument(
        "--kind",
        default="manual",
        choices=("friday_50", "pnl_sweep", "manual", "seed"),
    )
    p.add_argument("--note", default=None)
    args = p.parse_args()
    db = default_treasury_db(args.data_dir)
    ensure_schema(db)
    price = args.price
    if price is None:
        snap = snapshot(db, write_mark=False)
        price = snap.mark_btc_usd
    if price is None or price <= 0:
        raise SystemExit("Need --price or a reachable BTC-USD spot")
    btc = args.btc if args.btc is not None else args.usd / price
    c = record_contribution(
        db,
        usd_amount=args.usd,
        btc_qty=btc,
        btc_price=price,
        kind=args.kind,
        ts=datetime.now(timezone.utc),
        note=args.note,
    )
    print(
        f"recorded id={c.id} kind={c.kind} usd={c.usd_amount:.2f} "
        f"btc={c.btc_qty:.8f} px={c.btc_price:.2f}"
    )


if __name__ == "__main__":
    main()
