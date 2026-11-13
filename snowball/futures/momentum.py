"""Intraday fast-momentum helpers for Future Trader (momentum_15m).

Conservative breakout/impulse on 5m or 15m CFM bars during US cash hours.
Shares the FT lane budget with session_day; never places its own broker calls —
the futures engine opens/closes lots. Research/KPI framing only (~$100/day
aspirational); risk gates and CFM_MAX_CONTRACTS stay authoritative.
"""

from __future__ import annotations

from typing import Sequence


MOMENTUM_15M = "momentum_15m"


def momentum_breakout(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    opens: Sequence[float],
    *,
    lookback: int = 8,
    min_momentum_pct: float = 0.003,
) -> bool:
    """True on a green impulse that clears the prior lookback high.

    Requires:
      • enough bars (lookback + 1)
      • last bar green (close > open)
      • last close above prior-window high by ``min_momentum_pct``
      • last close above the lookback SMA (green structure)
    """
    n = int(lookback)
    if n < 2:
        return False
    need = n + 1
    if (
        len(closes) < need
        or len(highs) < need
        or len(lows) < need
        or len(opens) < need
    ):
        return False
    h = [float(x) for x in highs[-need:]]
    c = [float(x) for x in closes[-need:]]
    o = [float(x) for x in opens[-need:]]
    if any(x <= 0 for x in h + c + o):
        return False
    last_c = c[-1]
    last_o = o[-1]
    if last_c <= last_o:
        return False
    prior_high = max(h[:-1])
    floor = prior_high * (1.0 + max(0.0, float(min_momentum_pct)))
    if last_c < floor:
        return False
    window = c[:-1][-n:] if len(c) > n else c[:-1]
    if len(window) < n:
        window = c[-n:]
    sma = sum(window) / float(len(window))
    return last_c >= sma


def parse_ohlcv_ohlc(
    rows: Sequence[Sequence[float]],
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Extract open/high/low/close lists from OHLCV rows."""
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for r in rows:
        opens.append(float(r[1]))
        highs.append(float(r[2]))
        lows.append(float(r[3]))
        closes.append(float(r[4]))
    return opens, highs, lows, closes


def momentum_breakdown(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    opens: Sequence[float],
    *,
    lookback: int = 8,
    min_momentum_pct: float = 0.003,
) -> bool:
    """Mirror of :func:`momentum_breakout` for SHORT entries (FUTURES_SHORT_PRODUCTS).

    Requires:
      • enough bars (lookback + 1)
      • last bar red (close < open)
      • last close below prior-window low by ``min_momentum_pct``
      • last close at/below the lookback SMA (red structure)
    """
    n = int(lookback)
    if n < 2:
        return False
    need = n + 1
    if (
        len(closes) < need
        or len(highs) < need
        or len(lows) < need
        or len(opens) < need
    ):
        return False
    lo = [float(x) for x in lows[-need:]]
    c = [float(x) for x in closes[-need:]]
    o = [float(x) for x in opens[-need:]]
    if any(x <= 0 for x in lo + c + o):
        return False
    last_c = c[-1]
    last_o = o[-1]
    if last_c >= last_o:
        return False
    prior_low = min(lo[:-1])
    ceiling = prior_low * (1.0 - max(0.0, float(min_momentum_pct)))
    if last_c > ceiling:
        return False
    window = c[:-1][-n:] if len(c) > n else c[:-1]
    if len(window) < n:
        window = c[-n:]
    sma = sum(window) / float(len(window))
    return last_c <= sma


def side_pnl_pct(side: str, entry: float, mark: float | None) -> float | None:
    """Unrealized % for a long or short lot (positive = green)."""
    if mark is None or mark <= 0 or entry <= 0:
        return None
    if str(side).lower() == "short":
        return (entry - float(mark)) / entry
    return (float(mark) - entry) / entry


def price_stalled_short(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    mark: float | None,
    *,
    lookback: int = 4,
) -> bool:
    """Stall check for a SHORT: run gates.price_stalled on the reciprocal series
    (1/price flips highs<->lows), i.e. price no longer pressing new lows."""
    from snowball.gates import price_stalled

    if mark is None or mark <= 0:
        return False
    try:
        inv_h = [1.0 / float(x) for x in lows]
        inv_l = [1.0 / float(x) for x in highs]
        inv_c = [1.0 / float(x) for x in closes]
    except ZeroDivisionError:
        return False
    return price_stalled(inv_h, inv_l, inv_c, 1.0 / float(mark), lookback=lookback)


def momentum_values(highs, lows, closes, opens, *, lookback: int = 8,
                    min_momentum_pct: float = 0.003) -> dict:
    """The exact quantities momentum_breakout/breakdown compare (for fill context)."""
    n = int(lookback)
    need = n + 1
    if n < 2 or min(len(highs), len(lows), len(closes), len(opens)) < need:
        return {}
    h = [float(x) for x in highs[-need:]]
    lo = [float(x) for x in lows[-need:]]
    c = [float(x) for x in closes[-need:]]
    o = [float(x) for x in opens[-need:]]
    window = c[:-1][-n:] if len(c) > n else c[:-1]
    if len(window) < n:
        window = c[-n:]
    sma_v = sum(window) / float(len(window))
    prior_high = max(h[:-1])
    prior_low = min(lo[:-1])
    mp = max(0.0, float(min_momentum_pct))
    return {
        "last_open": o[-1], "last_close": c[-1],
        "prior_high": prior_high, "prior_low": prior_low,
        "breakout_level": prior_high * (1.0 + mp), "breakdown_level": prior_low * (1.0 - mp),
        "lookback_sma": sma_v, "lookback_bars": n, "min_momentum_pct": mp,
        "momentum_vs_prior_high_pct": (c[-1] / prior_high - 1.0) if prior_high > 0 else None,
        "momentum_vs_prior_low_pct": (c[-1] / prior_low - 1.0) if prior_low > 0 else None,
    }
