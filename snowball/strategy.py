from __future__ import annotations

from snowball.models import PairSnapshot, Signal

SMA_15M = "sma_15m"
SMA_5M = "sma_5m"
SMA_1D = "sma_1d"
EMA_15M = "ema_15m"
DONCHIAN_1D = "donchian_1d"
SESSION_DAY = "session_day"
MOMENTUM_15M = "momentum_15m"
RSI_15M = "rsi_15m"
RSI_1D = "rsi_1d"
BB_15M = "bb_15m"
BB_1D = "bb_1d"

EMA_FAST = 12
EMA_SLOW = 26
DONCHIAN_ENTRY = 20
DONCHIAN_EXIT = 10
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
BB_PERIOD = 20
BB_STD_MULT = 2.0

# Mean-reversion strategies: skip SMA trend filter (buy dips / lower band).
MEAN_REVERSION_STRATEGY_IDS: frozenset[str] = frozenset(
    {RSI_15M, RSI_1D, BB_15M, BB_1D}
)

KNOWN_STRATEGY_IDS: frozenset[str] = frozenset(
    {
        SMA_15M,
        SMA_5M,
        SMA_1D,
        EMA_15M,
        DONCHIAN_1D,
        SESSION_DAY,
        MOMENTUM_15M,
        RSI_15M,
        RSI_1D,
        BB_15M,
        BB_1D,
    }
)

TIMEFRAME_BY_STRATEGY: dict[str, str] = {
    SMA_15M: "15m",
    SMA_5M: "5m",
    SMA_1D: "1d",
    EMA_15M: "15m",
    DONCHIAN_1D: "1d",
    SESSION_DAY: "1d",  # unused by session engine; marks-only
    MOMENTUM_15M: "15m",
    RSI_15M: "15m",
    RSI_1D: "1d",
    BB_15M: "15m",
    BB_1D: "1d",
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


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI of `closes`. Needs at least period+1 bars."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(delta if delta > 0.0 else 0.0)
        losses.append(-delta if delta < 0.0 else 0.0)
    avg_gain = sum(gains[:period]) / float(period)
    avg_loss = sum(losses[:period]) / float(period)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / float(period)
        avg_loss = (avg_loss * (period - 1) + losses[i]) / float(period)
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger_bands(
    closes: list[float],
    period: int = BB_PERIOD,
    stdev_mult: float = BB_STD_MULT,
) -> tuple[float | None, float | None, float | None]:
    """Return (upper, middle SMA, lower) using population stdev of the window."""
    mid = sma(closes, period)
    if mid is None or period <= 0:
        return None, None, None
    window = closes[-period:]
    var = sum((x - mid) ** 2 for x in window) / float(period)
    sd = var ** 0.5
    return mid + stdev_mult * sd, mid, mid - stdev_mult * sd


def rsi_cross_signal(
    closes: list[float],
    period: int = RSI_PERIOD,
    oversold: float = RSI_OVERSOLD,
    overbought: float = RSI_OVERBOUGHT,
) -> Signal:
    """Long-only RSI cross: ENTER up through oversold; EXIT down through overbought."""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    # Need prior and current RSI → period+2 closes.
    if len(closes) < period + 2:
        return Signal.HOLD
    prev = rsi(closes[:-1], period)
    cur = rsi(closes, period)
    if prev is None or cur is None:
        return Signal.HOLD
    if prev <= oversold and cur > oversold:
        return Signal.ENTER
    if prev >= overbought and cur < overbought:
        return Signal.EXIT
    return Signal.HOLD


def bb_cross_signal(
    closes: list[float],
    period: int = BB_PERIOD,
    stdev_mult: float = BB_STD_MULT,
) -> Signal:
    """Long-only Bollinger: ENTER cross up from <= lower; EXIT at/above upper."""
    if period <= 0:
        raise ValueError("Bollinger period must be positive")
    if len(closes) < period + 1:
        return Signal.HOLD
    _pu, _pm, prev_lower = bollinger_bands(closes[:-1], period, stdev_mult)
    cur_upper, _cm, cur_lower = bollinger_bands(closes, period, stdev_mult)
    if prev_lower is None or cur_lower is None or cur_upper is None:
        return Signal.HOLD
    prev_close = closes[-2]
    cur_close = closes[-1]
    # Prefer EXIT when at/above upper (spike through both bands).
    if cur_close >= cur_upper:
        return Signal.EXIT
    if prev_close <= prev_lower and cur_close > cur_lower:
        return Signal.ENTER
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
    if strategy_id == RSI_15M:
        rsi_v = getattr(snap, "rsi_15m", None)
        up = rsi_v is not None and RSI_OVERSOLD < float(rsi_v) < RSI_OVERBOUGHT
        return Signal(getattr(snap, "signal_rsi_15m", Signal.HOLD.value)), up
    if strategy_id == RSI_1D:
        rsi_v = getattr(snap, "rsi_1d", None)
        up = rsi_v is not None and RSI_OVERSOLD < float(rsi_v) < RSI_OVERBOUGHT
        return Signal(getattr(snap, "signal_rsi_1d", Signal.HOLD.value)), up
    if strategy_id == BB_15M:
        last = snap.last
        mid = getattr(snap, "bb_mid_15m", None)
        lower = getattr(snap, "bb_lower_15m", None)
        up = (
            last is not None
            and mid is not None
            and lower is not None
            and float(lower) <= float(last) <= float(mid)
        )
        return Signal(getattr(snap, "signal_bb_15m", Signal.HOLD.value)), up
    if strategy_id == BB_1D:
        last = snap.last
        mid = getattr(snap, "bb_mid_1d", None)
        lower = getattr(snap, "bb_lower_1d", None)
        up = (
            last is not None
            and mid is not None
            and lower is not None
            and float(lower) <= float(last) <= float(mid)
        )
        return Signal(getattr(snap, "signal_bb_1d", Signal.HOLD.value)), up
    raise ValueError(f"unknown strategy {strategy_id}")


def populate_rsi_bb(
    snap: PairSnapshot,
    closes: list[float],
    timeframe: str,
    *,
    wanted: set[str],
    filters_enabled: bool = True,
    rsi_period: int = RSI_PERIOD,
    bb_period: int = BB_PERIOD,
    bb_std_mult: float = BB_STD_MULT,
) -> None:
    """Fill RSI/BB snapshot fields for a timeframe when filters or strategies need them."""
    need_values = filters_enabled or bool(
        wanted
        & {
            RSI_15M,
            RSI_1D,
            BB_15M,
            BB_1D,
            SMA_15M,
            SMA_5M,
            SMA_1D,
            EMA_15M,
            DONCHIAN_1D,
        }
    )
    # Always compute values when filters are on or any strategy on this TF is active.
    if timeframe == "15m":
        tf_wanted = bool(wanted & {SMA_15M, EMA_15M, RSI_15M, BB_15M}) or filters_enabled
    elif timeframe == "5m":
        tf_wanted = bool(wanted & {SMA_5M}) or filters_enabled
    elif timeframe == "1d":
        tf_wanted = bool(wanted & {SMA_1D, DONCHIAN_1D, RSI_1D, BB_1D}) or filters_enabled
    else:
        return
    if not tf_wanted and not need_values:
        return

    rsi_v = rsi(closes, rsi_period)
    upper, mid, lower = bollinger_bands(closes, bb_period, bb_std_mult)

    if timeframe == "15m":
        snap.rsi_15m = rsi_v
        snap.bb_upper_15m = upper
        snap.bb_mid_15m = mid
        snap.bb_lower_15m = lower
        if RSI_15M in wanted:
            snap.signal_rsi_15m = rsi_cross_signal(closes, rsi_period).value
        if BB_15M in wanted:
            snap.signal_bb_15m = bb_cross_signal(closes, bb_period, bb_std_mult).value
    elif timeframe == "5m":
        snap.rsi_5m = rsi_v
        snap.bb_upper_5m = upper
        snap.bb_mid_5m = mid
        snap.bb_lower_5m = lower
    elif timeframe == "1d":
        snap.rsi_1d = rsi_v
        snap.bb_upper_1d = upper
        snap.bb_mid_1d = mid
        snap.bb_lower_1d = lower
        if RSI_1D in wanted:
            snap.signal_rsi_1d = rsi_cross_signal(closes, rsi_period).value
        if BB_1D in wanted:
            snap.signal_bb_1d = bb_cross_signal(closes, bb_period, bb_std_mult).value
