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
