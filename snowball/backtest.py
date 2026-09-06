"""Offline backtest — does NOT place paper orders or write the ledger.

Usage:
  python -m snowball.backtest --csv tests/fixtures/ohlcv_sample.csv
  python -m snowball.backtest --csv path.csv --live-fetch   # optional manual fetch
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path


def sma(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    return sum(closes[-period:]) / float(period)


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if period <= 0 or len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(trs) < period:
        return None
    window = trs[-period:]
    return sum(window) / float(period)


def crossover_signal(closes: list[float], fast: int = 20, slow: int = 50) -> str:
    if len(closes) < slow + 1:
        return "hold"
    prev_fast = sma(closes[:-1], fast)
    prev_slow = sma(closes[:-1], slow)
    cur_fast = sma(closes, fast)
    cur_slow = sma(closes, slow)
    if None in (prev_fast, prev_slow, cur_fast, cur_slow):
        return "hold"
    assert prev_fast is not None and prev_slow is not None
    assert cur_fast is not None and cur_slow is not None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return "enter"
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return "exit"
    return "hold"


@dataclass
class Trade:
    entry_i: int
    entry_px: float
    exit_i: int | None = None
    exit_px: float | None = None
    reason: str = ""

    @property
    def pnl(self) -> float | None:
        if self.exit_px is None:
            return None
        return self.exit_px - self.entry_px


@dataclass
class BacktestResult:
    name: str
    trades: list[Trade] = field(default_factory=list)
    blocked_entries: int = 0

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_px is not None]
        pnls = [float(t.pnl) for t in closed if t.pnl is not None]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "name": self.name,
            "closed_trades": len(closed),
            "open_trades": sum(1 for t in self.trades if t.exit_px is None),
            "wins": wins,
            "win_rate": (wins / len(closed)) if closed else None,
            "realized_pnl": sum(pnls),
            "blocked_entries": self.blocked_entries,
        }


@dataclass
class OHLCV:
    ts: list[float]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]

    def __len__(self) -> int:
        return len(self.close)


def load_ohlcv_csv(path: Path) -> OHLCV:
    """CSV columns: ts,open,high,low,close,volume (header required)."""
    ts: list[float] = []
    o: list[float] = []
    h: list[float] = []
    l: list[float] = []
    c: list[float] = []
    v: list[float] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"ts", "open", "high", "low", "close", "volume"}
        if reader.fieldnames is None or not required.issubset({f.strip().lower() for f in reader.fieldnames}):
            raise ValueError(f"CSV must have columns {sorted(required)}")
        # normalize field access
        fields = {f.strip().lower(): f for f in reader.fieldnames}
        for row in reader:
            ts.append(float(row[fields["ts"]]))
            o.append(float(row[fields["open"]]))
            h.append(float(row[fields["high"]]))
            l.append(float(row[fields["low"]]))
            c.append(float(row[fields["close"]]))
            v.append(float(row[fields["volume"]]))
    return OHLCV(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def simulate_sma_with_gates(
    data: OHLCV,
    *,
    fast: int = 20,
    slow: int = 50,
    trend_filter_enabled: bool = True,
    scale_in_min_profit_pct: float = 0.005,
    max_lots: int = 2,
    notional: float = 100.0,
) -> BacktestResult:
    """SMA crossover entries/exits with trend filter + scale-in green gate.

    Position sizing is 1 unit for PnL simplicity (notional tracked in summary only).
    """
    del notional  # reserved for future notional-weighted reports
    result = BacktestResult(name="sma_gates")
    open_lots: list[Trade] = []

    for i in range(len(data)):
        closes = data.close[: i + 1]
        last = data.close[i]
        slow_v = sma(closes, slow)
        sig = crossover_signal(closes, fast, slow)

        # exits first
        if sig == "exit" and open_lots:
            for lot in open_lots:
                lot.exit_i = i
                lot.exit_px = last
                lot.reason = "sma_exit"
                result.trades.append(lot)
            open_lots = []
            continue

        want_entry = sig == "enter" or (
            sig == "hold"
            and slow_v is not None
            and sma(closes, fast) is not None
            and sma(closes, fast) > slow_v  # type: ignore[operator]
            and 0 < len(open_lots) < max_lots
        )
        if not want_entry:
            continue

        # trend filter
        if trend_filter_enabled:
            if slow_v is None or last <= slow_v:
                result.blocked_entries += 1
                continue

        # scale-in gate
        if open_lots:
            floor = 1.0 + max(0.0, scale_in_min_profit_pct)
            if any(last <= lot.entry_px * floor for lot in open_lots):
                result.blocked_entries += 1
                continue

        if len(open_lots) >= max_lots:
            result.blocked_entries += 1
            continue

        open_lots.append(Trade(entry_i=i, entry_px=last, reason="sma_enter"))

    # leave open trades marked open
    result.trades.extend(open_lots)
    return result


def simulate_atr_trail_candidate(
    data: OHLCV,
    *,
    fast: int = 20,
    slow: int = 50,
    atr_period: int = 14,
    atr_mult: float = 2.0,
    trend_filter_enabled: bool = True,
) -> BacktestResult:
    """Candidate: same SMA enter + trend filter, exit via ATR trail from peak.

    Exit when close < peak - atr_mult * ATR(atr_period), or hard stop
    close < entry - atr_mult * ATR at entry. Not wired into the live engine.
    """
    result = BacktestResult(name="atr_trail_candidate")
    open_trade: Trade | None = None
    peak = 0.0
    entry_atr = 0.0

    for i in range(len(data)):
        closes = data.close[: i + 1]
        highs = data.high[: i + 1]
        lows = data.low[: i + 1]
        last = data.close[i]
        slow_v = sma(closes, slow)
        sig = crossover_signal(closes, fast, slow)
        cur_atr = atr(highs, lows, closes, atr_period)

        if open_trade is not None:
            peak = max(peak, last)
            stop = None
            if cur_atr is not None:
                trail = peak - atr_mult * cur_atr
                hard = open_trade.entry_px - atr_mult * (entry_atr or cur_atr)
                stop = max(trail, hard)
            # also honour classic SMA death cross as soft exit
            exit_now = False
            reason = ""
            if stop is not None and last < stop:
                exit_now = True
                reason = "atr_trail"
            elif sig == "exit":
                exit_now = True
                reason = "sma_exit"
            if exit_now:
                open_trade.exit_i = i
                open_trade.exit_px = last
                open_trade.reason = reason
                result.trades.append(open_trade)
                open_trade = None
            continue

        if sig != "enter":
            continue
        if trend_filter_enabled and (slow_v is None or last <= slow_v):
            result.blocked_entries += 1
            continue
        if cur_atr is None:
            result.blocked_entries += 1
            continue
        open_trade = Trade(entry_i=i, entry_px=last, reason="sma_enter")
        peak = last
        entry_atr = cur_atr

    if open_trade is not None:
        result.trades.append(open_trade)
    return result


def fetch_ohlcv_live(product: str, timeframe: str, limit: int = 200) -> OHLCV:
    """Optional live fetch for manual use only (not used by offline tests)."""
    import ccxt  # local import so offline tests need no network

    ex = ccxt.coinbase({"enableRateLimit": True})
    rows = ex.fetch_ohlcv(product, timeframe=timeframe, limit=limit)
    return OHLCV(
        ts=[float(r[0]) for r in rows],
        open=[float(r[1]) for r in rows],
        high=[float(r[2]) for r in rows],
        low=[float(r[3]) for r in rows],
        close=[float(r[4]) for r in rows],
        volume=[float(r[5]) for r in rows],
    )


def _print_summary(rows: list[dict]) -> None:
    print(f"{'name':24} {'closed':>6} {'wins':>5} {'win_rate':>8} {'pnl':>12} {'blocked':>8}")
    for s in rows:
        wr = s["win_rate"]
        wr_s = f"{wr:.1%}" if wr is not None else "—"
        print(
            f"{s['name']:24} {s['closed_trades']:6d} {s['wins']:5d} {wr_s:>8} "
            f"{s['realized_pnl']:12.4f} {s['blocked_entries']:8d}"
        )


def run_backtest(
    data: OHLCV,
    *,
    fast: int = 20,
    slow: int = 50,
    trend_filter_enabled: bool = True,
    scale_in_min_profit_pct: float = 0.005,
) -> list[dict]:
    sma_res = simulate_sma_with_gates(
        data,
        fast=fast,
        slow=slow,
        trend_filter_enabled=trend_filter_enabled,
        scale_in_min_profit_pct=scale_in_min_profit_pct,
    )
    atr_res = simulate_atr_trail_candidate(
        data,
        fast=fast,
        slow=slow,
        trend_filter_enabled=trend_filter_enabled,
    )
    return [sma_res.summary(), atr_res.summary()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Snowball offline backtest (no paper ledger writes)")
    p.add_argument("--csv", type=Path, help="OHLCV CSV fixture (ts,open,high,low,close,volume)")
    p.add_argument("--live-fetch", action="store_true", help="Fetch live OHLCV via ccxt (manual only)")
    p.add_argument("--product", default="BTC-USD")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=50)
    p.add_argument("--no-trend-filter", action="store_true")
    p.add_argument("--scale-in-min-profit-pct", type=float, default=0.005)
    args = p.parse_args(argv)

    if args.live_fetch:
        data = fetch_ohlcv_live(args.product, args.timeframe, args.limit)
    elif args.csv:
        data = load_ohlcv_csv(args.csv)
    else:
        p.error("provide --csv PATH or --live-fetch")
        return 2

    summaries = run_backtest(
        data,
        fast=args.fast,
        slow=args.slow,
        trend_filter_enabled=not args.no_trend_filter,
        scale_in_min_profit_pct=args.scale_in_min_profit_pct,
    )
    print(f"bars={len(data)} product={args.product if args.live_fetch else args.csv}")
    _print_summary(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
