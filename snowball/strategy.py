from __future__ import annotations

from snowball.models import PairSnapshot, Signal

SMA_15M = "sma_15m"
SMA_5M = "sma_5m"
SMA_1D = "sma_1d"

KNOWN_STRATEGY_IDS: frozenset[str] = frozenset({SMA_15M, SMA_5M, SMA_1D})

TIMEFRAME_BY_STRATEGY: dict[str, str] = {
    SMA_15M: "15m",
    SMA_5M: "5m",
    SMA_1D: "1d",
}


def parse_strategies(raw: str) -> list[str]:
    """Parse a comma list into unique strategy ids, preserving order."""
    items = [s.strip().lower() for s in raw.split(",") if s.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for sid in items:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def enabled_timeframes(strategy_ids: list[str]) -> list[str]:
    tfs: list[str] = []
    for sid in strategy_ids:
        tf = TIMEFRAME_BY_STRATEGY[sid]
        if tf not in tfs:
            tfs.append(tf)
    return tfs


def sma(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / float(period)


def crossover_signal(
    closes: list[float],
    fast: int = 20,
    slow: int = 50,
) -> Signal:
    """Long-only 20/50 SMA crossover on the last two completed windows.

    ENTER when fast crosses from <= slow to > slow.
    EXIT when fast crosses from >= slow to < slow.
    HOLD otherwise (including insufficient history).

    Used by both sma_15m and sma_5m on that timeframe's close series.
    """
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    if len(closes) < slow + 1:
        return Signal.HOLD
    prev_fast = sma(closes[:-1], fast)
    prev_slow = sma(closes[:-1], slow)
    cur_fast = sma(closes, fast)
    cur_slow = sma(closes, slow)
    if None in (prev_fast, prev_slow, cur_fast, cur_slow):
        return Signal.HOLD
    assert prev_fast is not None and prev_slow is not None
    assert cur_fast is not None and cur_slow is not None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return Signal.ENTER
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return Signal.EXIT
    return Signal.HOLD


def in_uptrend(closes: list[float], fast: int = 20, slow: int = 50) -> bool:
    cur_fast = sma(closes, fast)
    cur_slow = sma(closes, slow)
    if cur_fast is None or cur_slow is None:
        return False
    return cur_fast > cur_slow


def signal_for_strategy(snap: PairSnapshot, strategy_id: str) -> tuple[Signal, bool]:
    """Return (signal, fast>slow uptrend) for a strategy on the pair snapshot."""
    if strategy_id == SMA_15M:
        up = (
            snap.sma_fast is not None
            and snap.sma_slow is not None
            and snap.sma_fast > snap.sma_slow
        )
        return Signal(snap.signal), up
    if strategy_id == SMA_5M:
        up = (
            snap.sma_fast_5m is not None
            and snap.sma_slow_5m is not None
            and snap.sma_fast_5m > snap.sma_slow_5m
        )
        return Signal(snap.signal_5m), up
    if strategy_id == SMA_1D:
        up = (
            snap.sma_fast_1d is not None
            and snap.sma_slow_1d is not None
            and snap.sma_fast_1d > snap.sma_slow_1d
        )
        return Signal(snap.signal_1d), up
    raise ValueError(f"unknown strategy {strategy_id}")
