#!/usr/bin/env python3
"""One-shot: record exchange live fills missing from the ledger, sync cash, clear false daily kill.

Uses real Coinbase fills via ccxt. Never prints secrets.
Does NOT place orders or sell anything.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowball.config import Settings
from snowball.live import LiveBroker, parse_order_fill
from snowball.market import to_ccxt_symbol
from snowball.paper import PaperLedger


DUST_USD = 1.0  # ignore exchange dust below this notional


def _parse_ts(value: str | None, fallback_ms: int | None = None) -> datetime:
    if value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if fallback_ms:
        return datetime.fromtimestamp(fallback_ms / 1000.0, tz=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_recent_buys(ex, product: str, since_ms: int) -> list[dict]:
    symbol = to_ccxt_symbol(product)
    trades: list = []
    try:
        trades = ex.fetch_my_trades(symbol, since=since_ms, limit=50) or []
    except Exception as exc:  # noqa: BLE001
        print(f"warn fetch_my_trades {product}: {type(exc).__name__}: {exc}"[:240])
    buys = []
    for t in trades:
        if str(t.get("side") or "").lower() != "buy":
            continue
        px = float(t.get("price") or 0)
        qty = float(t.get("amount") or 0)
        cost = t.get("cost")
        fee = t.get("fee") if isinstance(t.get("fee"), dict) else {}
        fee_usd = float(fee.get("cost") or 0) if fee else 0.0
        currency = str(fee.get("currency") or "USD").upper()
        if currency not in ("", "USD", "USDT", "USDC") and px > 0:
            fee_usd = fee_usd * px
        if qty <= 0 or px <= 0:
            continue
        notional = float(cost) if cost is not None else qty * px
        buys.append(
            {
                "product": product,
                "order_id": t.get("order"),
                "trade_id": t.get("id"),
                "fill_px": px,
                "fill_qty": qty,
                "notional_usd": notional,
                "fee_usd": fee_usd,
                "ts": _parse_ts(t.get("datetime"), t.get("timestamp")),
            }
        )
    if buys:
        return buys
    # Fallback: closed orders
    try:
        orders = ex.fetch_orders(symbol, since=since_ms, limit=50) or []
    except Exception:
        try:
            orders = ex.fetch_closed_orders(symbol, since=since_ms, limit=50) or []
        except Exception as exc:  # noqa: BLE001
            print(f"warn fetch_orders {product}: {type(exc).__name__}: {exc}"[:240])
            return []
    for o in orders:
        if str(o.get("side") or "").lower() != "buy":
            continue
        px, qty, fee_usd = parse_order_fill(o if isinstance(o, dict) else {})
        if qty <= 0 or px <= 0:
            continue
        buys.append(
            {
                "product": product,
                "order_id": o.get("id"),
                "trade_id": None,
                "fill_px": px,
                "fill_qty": qty,
                "notional_usd": qty * px,
                "fee_usd": fee_usd,
                "ts": _parse_ts(o.get("datetime"), o.get("timestamp")),
            }
        )
    return buys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24.0, help="Lookback for exchange fills")
    ap.add_argument("--apply", action="store_true", help="Write ledger (default dry-run)")
    ap.add_argument("--clear-daily-kill", action="store_true", help="Clear today's daily_state.killed")
    ap.add_argument(
        "--strategy",
        default="sma_5m",
        help="Strategy tag for orphan lots (journal showed sma_5m for NEAR)",
    )
    ap.add_argument("--product", action="append", default=None, help="Limit to product(s)")
    args = ap.parse_args()

    settings = Settings()
    if not settings.live_orders_permitted():
        print("refusing: MODE/LIVE_ENABLED do not permit live broker")
        return 2

    broker = LiveBroker(settings)
    ex = broker.exchange
    ledger = PaperLedger(Path(settings.sqlite_path), settings.bankroll_usd)

    free_usd = broker.fetch_free_usd()
    bal = ex.fetch_balance()
    total = bal.get("total") or {}
    products = args.product or list(settings.product_list())
    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=args.hours)).timestamp() * 1000)

    orphans: list[dict] = []
    for product in products:
        base = product.split("-")[0].upper()
        qty_ex = float(total.get(base) or 0)
        if qty_ex <= 0:
            continue
        open_lots = ledger.open_positions(product)
        ledger_qty = sum(p.qty for p in open_lots)
        # significant exchange qty with little/no ledger coverage
        try:
            ticker = ex.fetch_ticker(to_ccxt_symbol(product))
            mark = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception:
            mark = 0.0
        gap_qty = qty_ex - ledger_qty
        gap_notional = gap_qty * mark if mark > 0 else 0.0
        if gap_qty <= 1e-8 or gap_notional < DUST_USD:
            continue
        buys = fetch_recent_buys(ex, product, since_ms)
        # Prefer buys whose qty approximates the gap
        best = None
        for b in sorted(buys, key=lambda x: x["ts"], reverse=True):
            if abs(b["fill_qty"] - gap_qty) / max(gap_qty, 1e-9) < 0.05 or abs(
                b["fill_qty"] - gap_qty
            ) < 0.05:
                best = b
                break
        if best is None and buys:
            # take most recent buy if gap is roughly that notional
            best = buys[0]
        if best is None:
            print(
                json.dumps(
                    {
                        "orphan_without_fill": True,
                        "product": product,
                        "exchange_qty": qty_ex,
                        "ledger_qty": ledger_qty,
                        "gap_qty": gap_qty,
                        "mark": mark,
                    }
                )
            )
            continue
        best["exchange_qty"] = qty_ex
        best["ledger_qty"] = ledger_qty
        best["gap_qty"] = gap_qty
        best["mark"] = mark
        orphans.append(best)

    print("===ORPHANS===")
    print(json.dumps(orphans, indent=2, default=str))
    print("exchange_free_usd", free_usd)
    print("ledger_cash_before", ledger.cash_usd())

    applied = []
    if args.apply:
        for o in orphans:
            # Cash already reflects the exchange spend (live cash sync).
            # Temporarily credit cost so open_buy can debit without going negative wrongly.
            cost = float(o["notional_usd"]) + float(o["fee_usd"])
            ledger.set_cash_usd(ledger.cash_usd() + cost)
            pos, fill = ledger.open_buy(
                product=o["product"],
                fill_px=float(o["fill_px"]),
                notional_usd=float(o["notional_usd"]),
                slippage_bps=0.0,
                fee_usd=float(o["fee_usd"]),
                reason=f"reconcile:orphan_live:{o.get('order_id') or o.get('trade_id')}",
                ts=o["ts"],
                strategy=args.strategy,
            )
            applied.append(
                {
                    "product": o["product"],
                    "position_id": pos.id,
                    "fill_id": fill.id,
                    "qty": pos.qty,
                    "entry_price": pos.entry_price,
                    "notional_usd": fill.notional_usd,
                    "fee_usd": fill.fee_usd,
                    "order_id": o.get("order_id"),
                }
            )
        # Sync cash to exchange free USD after lots recorded
        free_usd = broker.fetch_free_usd()
        ledger.set_cash_usd(free_usd)
        print("===APPLIED===")
        print(json.dumps(applied, indent=2))
        print("ledger_cash_after_sync", ledger.cash_usd())

    if args.clear_daily_kill:
        utc_date = datetime.now(timezone.utc).date().isoformat()
        with ledger._lock:
            ledger._conn.execute(
                "UPDATE daily_state SET killed = 0 WHERE utc_date = ?",
                (utc_date,),
            )
            ledger._conn.commit()
        print("cleared_daily_kill", utc_date, "killed_now", ledger.is_daily_killed(datetime.now(timezone.utc)))

    open_now = [
        {
            "id": p.id,
            "product": p.product,
            "qty": p.qty,
            "entry_price": p.entry_price,
            "notional_usd": p.notional_usd,
            "strategy": p.strategy,
        }
        for p in ledger.open_positions()
    ]
    print("===OPEN_POSITIONS===")
    print(json.dumps(open_now, indent=2))
    print(
        "daily_killed",
        ledger.is_daily_killed(datetime.now(timezone.utc)),
        "cash",
        ledger.cash_usd(),
    )
    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
