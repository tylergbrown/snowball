#!/usr/bin/env python3
"""Task 10 (2026-09-30): move open STOCK-lane lots into the Futures (FT) lane DB.

Run with the bot STOPPED (no engine touching either DB):

    python /app/tools/migrate_stock_to_futures.py            # dry run
    python /app/tools/migrate_stock_to_futures.py --execute  # backup both DBs, migrate

* Each open stock lot is re-created in snowball_futures.db with its ORIGINAL
  product, side, qty, entry price, notional, opened_at and strategy, plus its
  original entry fill (price, fee, ts) whose reason gets
  ``:migrated_from_stock_lane:stock_pos=<id>:stock_fill=<id>`` appended (the
  original ``:live`` tag is kept, so closed-P/L reporting counts it as live).
* FT book cash is debited qty*price+fee exactly like a normal FT open; the stock
  archive is credited back the same amount and the lot is marked ``migrated``
  (NOT ``closed``: no fake realized P/L; closed stock history stays in the
  archive DB and is reported as "former Stock lane").
* Idempotent: meta ``migrated_stock_pos:<id>`` in the FT DB; re-runs are no-ops.
* No orders. Nothing on Coinbase changes (the exchange position is the same).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TAG = "migrated_from_stock_lane"
CT = ZoneInfo("America/Chicago")


def backup(db: Path, stamp: str) -> Path:
    dst = db.with_name(f"{db.name}.bak-merge-stock-{stamp}")
    src = sqlite3.connect(str(db))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def plan(stock_db: Path, fut_db: Path) -> list[dict]:
    s = sqlite3.connect(f"file:{stock_db}?mode=ro", uri=True)
    s.row_factory = sqlite3.Row
    f = sqlite3.connect(f"file:{fut_db}?mode=ro", uri=True)
    try:
        done = {r[0] for r in f.execute("SELECT key FROM meta WHERE key LIKE 'migrated_stock_pos:%'")}
        out = []
        for p in s.execute("SELECT * FROM positions WHERE status='open' ORDER BY id"):
            p = dict(p)
            fills = [dict(r) for r in s.execute(
                "SELECT * FROM fills WHERE position_id=? ORDER BY id", (p["id"],))]
            p["fills"] = fills
            p["already"] = f"migrated_stock_pos:{p['id']}" in done
            rb = None
            try:
                rb = s.execute("SELECT * FROM roll_basis WHERE position_id=?", (p["id"],)).fetchone()
            except sqlite3.Error:
                rb = None
            p["roll_basis"] = dict(rb) if rb else None
            out.append(p)
        return out
    finally:
        s.close()
        f.close()


def migrate_one(stock_db: Path, fut_db: Path, p: dict, now: datetime) -> dict:
    buys = [x for x in p["fills"] if x["side"] == ("buy" if p["side"] == "long" else "sell")]
    entry_fill = buys[0] if buys else None
    fee = float(entry_fill["fee_usd"]) if entry_fill else 0.0
    cost = float(p["qty"]) * float(p["entry_price"]) + fee if p["side"] == "long" else float(p["notional_usd"]) + fee
    f = sqlite3.connect(str(fut_db), isolation_level=None)
    try:
        f.execute("BEGIN IMMEDIATE")
        if f.execute("SELECT 1 FROM meta WHERE key=?", (f"migrated_stock_pos:{p['id']}",)).fetchone():
            f.execute("ROLLBACK")
            return {"stock_pos": p["id"], "status": "already_migrated"}
        f.execute("UPDATE account SET cash_usd = cash_usd - ?, updated_at = ? WHERE id = 1",
                  (cost, now.isoformat()))
        cur = f.execute(
            "INSERT INTO positions (product, side, qty, entry_price, notional_usd, opened_at, status, strategy)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (p["product"], p["side"], p["qty"], p["entry_price"], p["notional_usd"], p["opened_at"], p["strategy"]),
        )
        new_id = int(cur.lastrowid)
        fill_id = None
        if entry_fill:
            reason = f"{entry_fill['reason']}:{TAG}:stock_pos={p['id']}:stock_fill={entry_fill['id']}"
            cur = f.execute(
                "INSERT INTO fills (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps,"
                " ts, reason, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, entry_fill["product"], entry_fill["side"], entry_fill["qty"], entry_fill["price"],
                 entry_fill["notional_usd"], entry_fill["fee_usd"], entry_fill["slippage_bps"], entry_fill["ts"],
                 reason, entry_fill["strategy"]),
            )
            fill_id = int(cur.lastrowid)
        if p.get("roll_basis"):
            rb = p["roll_basis"]
            cols = [c for c in rb if c != "position_id"]
            f.execute(
                f"INSERT OR REPLACE INTO roll_basis (position_id, {', '.join(cols)}) VALUES (?, {', '.join('?' for _ in cols)})",
                [new_id] + [rb[c] for c in cols],
            )
        rec = {"stock_pos": p["id"], "futures_pos": new_id, "futures_fill": fill_id, "cost_usd": cost,
               "at": now.isoformat()}
        f.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"migrated_stock_pos:{p['id']}", json.dumps(rec)))
        f.execute("COMMIT")
    except Exception:
        try:
            f.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        f.close()
    s = sqlite3.connect(str(stock_db), isolation_level=None)
    try:
        s.execute("BEGIN IMMEDIATE")
        n = s.execute("UPDATE positions SET status='migrated', closed_at=? WHERE id=? AND status='open'",
                      (now.isoformat(), p["id"])).rowcount
        if n != 1:
            raise RuntimeError(f"stock position {p['id']} not open anymore")
        s.execute("UPDATE account SET cash_usd = cash_usd + ?, updated_at = ? WHERE id = 1", (cost, now.isoformat()))
        s.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                  (f"migrated_to_futures:{p['id']}", json.dumps(rec)))
        s.execute("COMMIT")
    except Exception:
        try:
            s.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        # undo the FT side so the lot never exists twice
        f = sqlite3.connect(str(fut_db))
        try:
            f.execute("DELETE FROM fills WHERE position_id=?", (rec["futures_pos"],))
            f.execute("DELETE FROM positions WHERE id=?", (rec["futures_pos"],))
            f.execute("UPDATE account SET cash_usd = cash_usd + ? WHERE id = 1", (cost,))
            f.execute("DELETE FROM meta WHERE key=?", (f"migrated_stock_pos:{p['id']}",))
            f.commit()
        finally:
            f.close()
        raise
    finally:
        s.close()
    rec["status"] = "migrated"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    stock_db = args.data_dir / "snowball_stocks.db"
    fut_db = args.data_dir / "snowball_futures.db"
    items = plan(stock_db, fut_db)
    print(json.dumps({"plan": [{k: v for k, v in p.items() if k != "fills"} | {"fills": [x["id"] for x in p["fills"]]}
                               for p in items]}, default=str))
    if not args.execute:
        return 0
    stamp = datetime.now(CT).strftime("%Y%m%d%H%M%S")
    print(json.dumps({"backup": [str(backup(stock_db, stamp)), str(backup(fut_db, stamp))]}))
    now = datetime.now(timezone.utc)
    for p in items:
        if p["already"]:
            print(json.dumps({"stock_pos": p["id"], "status": "already_migrated"}))
            continue
        print(json.dumps(migrate_one(stock_db, fut_db, p, now), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
