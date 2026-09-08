from __future__ import annotations

from snowball.models import PairSnapshot, Signal

SMA_15M = "sma_15m"
SMA_5M = "sma_5m"
SMA_1D = "sma_1d"
EMA_15M = "ema_15m"
DONCHIAN_1D = "donchian_1d"

EMA_FAST = 12
EMA_SLOW = 26
DONCHIAN_ENTRY = 20
DONCHIAN_EXIT = 10

KNOWN_STRATEGY_IDS: frozenset[str] = frozenset(
    {SMA_15M, SMA_5M, SMA_1D, EMA_15M, DONCHIAN_1D}
)

TIMEFRAME_BY_STRATEGY: dict[str, str] = {
    SMA_15M: "15m",
    SMA_5M: "5m",
    SMA_1D: "1d",
    EMA_15M: "15m",
    DONCHIAN_1D: "1d",
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


def ema(closes: list[float], period: int) -> float | None:
    """Last EMA of `closes`. Seed is the SMA of the first `period` bars."""
    if period <= 0 or len(closes) < period:
        return None
    k = 2.0 / (period + 1.0)
    value = sum(closes[:period]) / float(period)
    for price in closes[period:]:
        value = (price - value) * k + value
    return value


def ema_crossover_signal(
    closes: list[float],
    fast: int = EMA_FAST,
    slow: int = EMA_SLOW,
) -> Signal:
    """Long-only EMA crossover on the last two completed windows.

    ENTER when fast crosses from <= slow to > slow.
    EXIT when fast crosses from >= slow to < slow.
    HOLD otherwise (including insufficient history).
    """
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    if len(closes) < slow + 1:
        return Signal.HOLD
    prev_fast = ema(closes[:-1], fast)
    prev_slow = ema(closes[:-1], slow)
    cur_fast = ema(closes, fast)
    cur_slow = ema(closes, slow)
    if None in (prev_fast, prev_slow, cur_fast, cur_slow):
        return Signal.HOLD
    assert prev_fast is not None and prev_slow is not None
    assert cur_fast is not None and cur_slow is not None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return Signal.ENTER
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return Signal.EXIT
    return Signal.HOLD


def donchian_channels(
    highs: list[float],
    lows: list[float],
    *,
    entry_lookback: int = DONCHIAN_ENTRY,
    exit_lookback: int = DONCHIAN_EXIT,
) -> tuple[float | None, float | None]:
    """Prior entry high and exit low, excluding the current (last) bar."""
    n = min(len(highs), len(lows))
    high = None
    low = None
    if n >= entry_lookback + 1 and entry_lookback > 0:
        high = max(highs[n - 1 - entry_lookback : n - 1])
    if n >= exit_lookback + 1 and exit_lookback > 0:
        low = min(lows[n - 1 - exit_lookback : n - 1])
    return high, low


def donchian_breakout_signal(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    *,
    entry_lookback: int = DONCHIAN_ENTRY,
    exit_lookback: int = DONCHIAN_EXIT,
) -> Signal:
    """Long-only Donchian breakout on the last close vs the prior channel.

    The current bar is excluded from the channel. ENTER when last close
    crosses above the prior `entry_lookback` high. EXIT when last close
    crosses below the prior `exit_lookback` low. HOLD if history is too
    short to compare the previous bar's channel with the current one.
    """
    if entry_lookback < 1 or exit_lookback < 1:
        raise ValueError("Donchian lookbacks must be positive")
    series_high = list(closes if highs is None else highs)
    series_low = list(closes if lows is None else lows)
    n = len(closes)
    if len(series_high) != n or len(series_low) != n:
        return Signal.HOLD
    # Need the current bar plus the previous bar, each with a full prior window.
    if n < entry_lookback + 2 or n < exit_lookback + 2:
        return Signal.HOLD

    prev_high = max(series_high[n - 2 - entry_lookback : n - 2])
    cur_high = max(series_high[n - 1 - entry_lookback : n - 1])
    prev_low = min(series_low[n - 2 - exit_lookback : n - 2])
    cur_low = min(series_low[n - 1 - exit_lookback : n - 1])
    prev_close = closes[-2]
    cur_close = closes[-1]
    if prev_close <= prev_high and cur_close > cur_high:
        return Signal.ENTER
    if prev_close >= prev_low and cur_close < cur_low:
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
    if strategy_id == EMA_15M:
        up = (
            snap.ema_fast_15m is not None
            and snap.ema_slow_15m is not None
            and snap.ema_fast_15m > snap.ema_slow_15m
        )
        return Signal(snap.signal_ema_15m), up
    if strategy_id == DONCHIAN_1D:
        # Still-broken-out: last above the prior 20-day high. Fade uses that
        # high as the fast line and the prior 10-day low as the slow line.
        up = (
            snap.last is not None
            and snap.donchian_high_1d is not None
            and snap.last > snap.donchian_high_1d
        )
        return Signal(snap.signal_donchian_1d), up
    raise ValueError(f"unknown strategy {strategy_id}")
