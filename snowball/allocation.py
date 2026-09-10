"""Cross-lane capital allocation from total Coinbase account value.

Default split (recomputed each tick/session from live account value):
  • Crypto live trader: 35%
  • Stock trader:       35%
  • Future Trader:      20%
  • Crash Guard:        10%

Philosophy: hold underwater; never sell red. These helpers only size *new*
entries / max open notional — they never force loss exits.
"""

from __future__ import annotations

from typing import Any


DEFAULT_CRYPTO_BUDGET_PCT = 0.35
DEFAULT_STOCK_BUDGET_PCT = 0.35
DEFAULT_FUTURES_BUDGET_PCT = 0.20
DEFAULT_CRASH_BUDGET_PCT = 0.10


def lane_budget_pcts(
    *,
    crypto_pct: float = DEFAULT_CRYPTO_BUDGET_PCT,
    stock_pct: float = DEFAULT_STOCK_BUDGET_PCT,
    futures_pct: float = DEFAULT_FUTURES_BUDGET_PCT,
    crash_pct: float = DEFAULT_CRASH_BUDGET_PCT,
) -> dict[str, float]:
    """Return normalized lane fractions (does not force sum==1; callers own knobs)."""
    return {
        "crypto": float(crypto_pct),
        "stock": float(stock_pct),
        "futures": float(futures_pct),
        "crash": float(crash_pct),
    }


def lane_budgets_usd(
    account_value_usd: float,
    *,
    crypto_pct: float = DEFAULT_CRYPTO_BUDGET_PCT,
    stock_pct: float = DEFAULT_STOCK_BUDGET_PCT,
    futures_pct: float = DEFAULT_FUTURES_BUDGET_PCT,
    crash_pct: float = DEFAULT_CRASH_BUDGET_PCT,
) -> dict[str, float]:
    """Dollar budgets per lane from total account value."""
    av = max(0.0, float(account_value_usd))
    return {
        "account_value_usd": av,
        "crypto_usd": av * float(crypto_pct),
        "stock_usd": av * float(stock_pct),
        "futures_usd": av * float(futures_pct),
        "crash_usd": av * float(crash_pct),
    }


def remaining_budget_usd(budget_usd: float, open_notional_usd: float) -> float:
    return max(0.0, float(budget_usd) - max(0.0, float(open_notional_usd)))


def leg_notional_usd(
    *,
    budget_usd: float,
    open_notional_usd: float,
    max_notional_usd: float,
    target_legs: int = 8,
) -> float:
    """Derive a long-only leg size from book budget (leverage=1, no crazy sizing).

    Per-leg size is min(max_notional_usd, remaining_budget). ``target_legs`` is
    reserved for callers that want a soft spread hint; it does not shrink a leg
    below max_notional when remaining budget still covers a full leg.
    Returns 0 when the lane book is full.
    """
    remaining = remaining_budget_usd(budget_usd, open_notional_usd)
    if remaining <= 1e-9:
        return 0.0
    _ = target_legs  # API stability; sizing is budget-remaining + max_notional
    cap = float(max_notional_usd) if max_notional_usd > 0 else remaining
    return max(0.0, min(cap, remaining))


def open_notional_usd(positions: list[Any]) -> float:
    total = 0.0
    for pos in positions:
        try:
            total += float(getattr(pos, "notional_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def effective_take_profit_floor(
    min_take_profit_pct: float, fee_buffer_pct: float
) -> float:
    """Mark vs entry floor for strategy/fade exits: min TP + round-trip fee buffer.

    Example: MIN_TAKE_PROFIT_PCT=0.06 and FEE_BUFFER_PCT=0.01 → 0.07 (7%).
    A print that is only +6% green would be refused so the exit stays green after ~1% fees.
    """
    return max(0.0, float(min_take_profit_pct)) + max(0.0, float(fee_buffer_pct))
