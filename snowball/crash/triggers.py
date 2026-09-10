"""Crash Guard entry triggers + short cover gate.

Enter short when flat on that index if ANY of:
  • daily return ≤ −2% vs prior daily close
  • RSI(14) daily ≤ 30 with dump context (daily return < 0)
  • close below Bollinger lower (20,2) with band width expanding vs prior bar

Cover short only when short PnL is green enough (never cover red).
"""

from __future__ import annotations

from dataclasses import dataclass

from snowball.allocation import effective_take_profit_floor
from snowball.strategy import RSI_OVERSOLD, RSI_PERIOD, bollinger_bands, rsi

DAILY_DUMP_PCT = -0.02  # −2%
BTC_TWIN_LOG_PCT = -0.05  # optional crypto twin log-only


@dataclass(frozen=True)
class TriggerResult:
    fire: bool
    reasons: tuple[str, ...]
    daily_return: float | None = None
    rsi_1d: float | None = None
    bb_lower: float | None = None
    bb_width: float | None = None
    bb_width_prior: float | None = None


def daily_return_pct(closes: list[float]) -> float | None:
    """(last - prior) / prior using last two daily closes."""
    if len(closes) < 2:
        return None
    prior = float(closes[-2])
    last = float(closes[-1])
    if prior <= 0:
        return None
    return (last - prior) / prior


def evaluate_crash_triggers(
    closes: list[float],
    *,
    rsi_period: int = RSI_PERIOD,
    bb_period: int = 20,
    bb_std_mult: float = 2.0,
    dump_pct: float = DAILY_DUMP_PCT,
    rsi_oversold: float = RSI_OVERSOLD,
) -> TriggerResult:
    """Evaluate SPY/QQQ daily crash triggers from daily close series."""
    reasons: list[str] = []
    dret = daily_return_pct(closes)
    rsi_v = rsi(closes, rsi_period) if len(closes) >= rsi_period + 1 else None

    upper, mid, lower = bollinger_bands(closes, bb_period, bb_std_mult)
    prior_u, prior_m, prior_l = bollinger_bands(closes[:-1], bb_period, bb_std_mult) if len(closes) > bb_period else (None, None, None)

    width = None
    width_prior = None
    if upper is not None and lower is not None:
        width = float(upper) - float(lower)
    if prior_u is not None and prior_l is not None:
        width_prior = float(prior_u) - float(prior_l)

    last = float(closes[-1]) if closes else None

    if dret is not None and dret <= dump_pct:
        reasons.append(f"daily_dump:{dret:.4f}")

    # RSI ≤ 30 after dump context (daily return negative)
    if (
        rsi_v is not None
        and float(rsi_v) <= float(rsi_oversold)
        and dret is not None
        and dret < 0.0
    ):
        reasons.append(f"rsi_oversold_dump:{rsi_v:.2f}")

    if (
        last is not None
        and lower is not None
        and last < float(lower)
        and width is not None
        and width_prior is not None
        and width > width_prior
    ):
        reasons.append("bb_lower_expanding")

    return TriggerResult(
        fire=bool(reasons),
        reasons=tuple(reasons),
        daily_return=dret,
        rsi_1d=rsi_v,
        bb_lower=lower,
        bb_width=width,
        bb_width_prior=width_prior,
    )


def short_unrealized_pnl_pct(entry_price: float, mark: float | None) -> float | None:
    """Short PnL fraction: (entry - mark) / entry. Positive when mark below entry."""
    if mark is None or mark <= 0 or entry_price <= 0:
        return None
    return (entry_price - float(mark)) / float(entry_price)


def short_cover_allowed(
    entry_price: float,
    mark: float | None,
    *,
    min_take_profit_pct: float = 0.06,
    fee_buffer_pct: float = 0.01,
    never_cover_red: bool = True,
) -> tuple[bool, str]:
    """Cover short only when short PnL is green enough (default ≥7%)."""
    pnl = short_unrealized_pnl_pct(entry_price, mark)
    if pnl is None:
        return False, "no_mark"
    if never_cover_red and pnl < 0:
        return False, "never_cover_red"
    floor = effective_take_profit_floor(min_take_profit_pct, fee_buffer_pct)
    if pnl < floor:
        return False, "below_take_profit"
    return True, "ok"


def btc_24h_twin_log(change_24h: float | None) -> str | None:
    """Optional crypto twin: log-only when BTC 24h ≤ −5%. Does not require a short."""
    if change_24h is None:
        return None
    if float(change_24h) <= BTC_TWIN_LOG_PCT:
        return f"btc_twin_dump_24h:{float(change_24h):.4f}"
    return None
