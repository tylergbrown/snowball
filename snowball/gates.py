"""Entry / exit gates: scale-in, trend filter, never-sell-red / fade / min take-profit."""

from __future__ import annotations

from snowball.models import Position

# Reasons that may flatten at a loss (bypass never-sell-red / min TP).
EMERGENCY_FLATTEN_REASONS: frozenset[str] = frozenset(
    {"halt_flatten", "daily_loss_kill"}
)


def scale_in_allowed(
    open_lots: list[Position],
    mark: float | None,
    min_profit_pct: float,
) -> tuple[bool, str]:
    """Allow a second (or later) lot only if every open lot for the strategy is green.

    ``min_profit_pct`` is a fraction of entry (0.005 = 0.5%). First entries
    (no open lots) are not gated here — callers should skip this check.
    """
    if not open_lots:
        return True, "ok"
    if mark is None or mark <= 0:
        return False, "scale_in_no_mark"
    floor = 1.0 + max(0.0, float(min_profit_pct))
    for lot in open_lots:
        if mark <= lot.entry_price * floor:
            return False, "scale_in_not_green"
    return True, "ok"


def trend_filter_allows(
    last: float | None,
    sma_slow: float | None,
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """No new entries unless last > SMA slow. Missing SMA slow blocks when enabled."""
    if not enabled:
        return True, "ok"
    if sma_slow is None:
        return False, "trend_sma_missing"
    if last is None or last <= 0:
        return False, "trend_no_mark"
    if last <= sma_slow:
        return False, "trend_below_sma_slow"
    return True, "ok"


def sma_slow_for_strategy(snap: object, strategy_id: str) -> float | None:
    """Return the strategy timeframe's slow line from a PairSnapshot-like object.

    SMA strategies use SMA slow. ema_15m uses EMA 26. donchian_1d uses the
    prior 10-day low (exit channel) so fade/trend gates stay on the same path.
    rsi_*/bb_* use Bollinger middle on that timeframe for fade reference.
    """
    if strategy_id == "sma_5m":
        return getattr(snap, "sma_slow_5m", None)
    if strategy_id == "sma_1d":
        return getattr(snap, "sma_slow_1d", None) or getattr(snap, "sma_slow", None)
    if strategy_id == "ema_15m":
        return getattr(snap, "ema_slow_15m", None)
    if strategy_id == "donchian_1d":
        return getattr(snap, "donchian_low_1d", None)
    if strategy_id in ("rsi_15m", "bb_15m"):
        return getattr(snap, "bb_mid_15m", None)
    if strategy_id in ("rsi_1d", "bb_1d"):
        return getattr(snap, "bb_mid_1d", None)
    return getattr(snap, "sma_slow", None)


def sma_fast_for_strategy(snap: object, strategy_id: str) -> float | None:
    """Return the strategy timeframe's fast line from a PairSnapshot-like object.

    SMA strategies use SMA fast. ema_15m uses EMA 12. donchian_1d uses the
    prior 20-day high (entry channel) so fade is a pullback off the breakout.
    rsi_*/bb_* use Bollinger upper as the "fast" fade reference.
    """
    if strategy_id == "sma_5m":
        return getattr(snap, "sma_fast_5m", None)
    if strategy_id == "sma_1d":
        return getattr(snap, "sma_fast_1d", None) or getattr(snap, "sma_fast", None)
    if strategy_id == "ema_15m":
        return getattr(snap, "ema_fast_15m", None)
    if strategy_id == "donchian_1d":
        return getattr(snap, "donchian_high_1d", None)
    if strategy_id in ("rsi_15m", "bb_15m"):
        return getattr(snap, "bb_upper_15m", None)
    if strategy_id in ("rsi_1d", "bb_1d"):
        return getattr(snap, "bb_upper_1d", None)
    return getattr(snap, "sma_fast", None)


def indicator_snapshot_for_strategy(
    snap: object, strategy_id: str
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (rsi, bb_upper, bb_mid, bb_lower) for the strategy's timeframe."""
    from snowball.strategy import TIMEFRAME_BY_STRATEGY

    tf = TIMEFRAME_BY_STRATEGY.get(strategy_id, "15m")
    if tf == "5m":
        return (
            getattr(snap, "rsi_5m", None),
            getattr(snap, "bb_upper_5m", None),
            getattr(snap, "bb_mid_5m", None),
            getattr(snap, "bb_lower_5m", None),
        )
    if tf == "1d":
        return (
            getattr(snap, "rsi_1d", None),
            getattr(snap, "bb_upper_1d", None),
            getattr(snap, "bb_mid_1d", None),
            getattr(snap, "bb_lower_1d", None),
        )
    return (
        getattr(snap, "rsi_15m", None),
        getattr(snap, "bb_upper_15m", None),
        getattr(snap, "bb_mid_15m", None),
        getattr(snap, "bb_lower_15m", None),
    )


def indicator_filters_allow(
    *,
    last: float | None,
    rsi: float | None,
    bb_upper: float | None,
    bb_mid: float | None = None,
    enabled: bool = True,
    overbought: float = 70.0,
) -> tuple[bool, str]:
    """Lean-on RSI/BB entry filter for existing (and new) strategies.

    When enabled:
    - Block if RSI >= overbought (default 70) — overbought chase.
    - Block if last > Bollinger upper — buying the relative high.
    Missing RSI/BB values do not block (history still warming up).
    ``bb_mid`` is accepted for API symmetry / soft preference callers.
    """
    _ = bb_mid  # soft preference (close <= mid) is advisory only
    if not enabled:
        return True, "ok"
    if rsi is not None and float(rsi) >= float(overbought):
        return False, "indicator_rsi_overbought"
    if last is not None and bb_upper is not None and float(last) > float(bb_upper):
        return False, "indicator_above_bb_upper"
    return True, "ok"


def lot_unrealized_pnl_pct(lot: Position, mark: float | None) -> float | None:
    """(mark - entry) / entry, or None if mark/entry unusable."""
    if mark is None or mark <= 0 or lot.entry_price <= 0:
        return None
    return (mark - lot.entry_price) / lot.entry_price


def strategy_exit_allowed(
    lot: Position,
    mark: float | None,
    *,
    min_take_profit_pct: float,
    never_sell_red: bool,
    fee_buffer_pct: float = 0.0,
) -> tuple[bool, str]:
    """Normal (non-emergency) exit gate for any strategy lot.

    Never sell red when never_sell_red. Effective floor is
    min_take_profit_pct + fee_buffer_pct (default fee buffer 0 for unit tests;
    production passes FEE_BUFFER_PCT≈0.01 so a “6% green” that would be red
    after ~1% round-trip fees is refused). Caller must also require a
    death-cross EXIT or momentum fade. Emergency HALT/daily-kill bypasses this.
    """
    from snowball.allocation import effective_take_profit_floor

    pnl_pct = lot_unrealized_pnl_pct(lot, mark)
    if pnl_pct is None:
        return False, "swing_no_mark"
    if never_sell_red and pnl_pct < 0:
        return False, "never_sell_red"
    floor = effective_take_profit_floor(min_take_profit_pct, fee_buffer_pct)
    if pnl_pct < floor:
        return False, "below_take_profit"
    return True, "ok"


# Back-compat alias used by earlier swing patch drafts.
sma_15m_strategy_exit_allowed = strategy_exit_allowed


def is_emergency_flatten_reason(reason: str) -> bool:
    """True for HALT / daily-loss flatten reasons that may sell red."""
    base = reason.split(":", 1)[0]
    return reason in EMERGENCY_FLATTEN_REASONS or base in EMERGENCY_FLATTEN_REASONS


def momentum_fading(
    *,
    last: float | None,
    sma_fast: float | None,
    sma_slow: float | None,
) -> bool:
    """True when price loses the fast SMA while still above the slow SMA.

    Early fade of a positive run — before a full 20/50 death cross.
    """
    if last is None or sma_fast is None or sma_slow is None:
        return False
    if last <= 0 or sma_fast <= 0 or sma_slow <= 0:
        return False
    return last < sma_fast and last > sma_slow

