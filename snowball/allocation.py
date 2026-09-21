"""Cross-lane capital allocation from total Coinbase account value.

Default split (recomputed each tick/session from live account value):
  • Crypto live trader: 40%
  • Stock trader:       15%
  • Shared spot pool:   65% when CRYPTO_STOCK_SHARED_BUDGET=true
    (crypto + stock draw from one AV*(crypto_pct+stock_pct) pool;
     open notional = crypto open + stock open; either lane may use idle capital)
  • Future Trader:      40%  (session overnight roll + intraday momentum)
  • Crash Guard:        10%
  • Fed Desk:            5%

When sharing is off, crypto and stock keep independent per-lane budgets.
FT / Crash / Fed are never part of the spot pool.

Note: lane knobs are independent and need not sum to 100%; Tb may trim
crypto/stock later. FUTURES_ACCOUNT_BUDGET_PCT is the FT authority.

Philosophy: hold underwater; never sell red. These helpers only size *new*
entries / max open notional — they never force loss exits.
"""

from __future__ import annotations

from typing import Any


DEFAULT_CRYPTO_BUDGET_PCT = 0.40
DEFAULT_STOCK_BUDGET_PCT = 0.15
DEFAULT_FUTURES_BUDGET_PCT = 0.40
DEFAULT_CRASH_BUDGET_PCT = 0.10
DEFAULT_FED_BUDGET_PCT = 0.05
# Combined spot (crypto + stock) when CRYPTO_STOCK_SHARED_BUDGET is on.
DEFAULT_SPOT_SHARED_BUDGET_PCT = DEFAULT_CRYPTO_BUDGET_PCT + DEFAULT_STOCK_BUDGET_PCT


def lane_budget_pcts(
    *,
    crypto_pct: float = DEFAULT_CRYPTO_BUDGET_PCT,
    stock_pct: float = DEFAULT_STOCK_BUDGET_PCT,
    futures_pct: float = DEFAULT_FUTURES_BUDGET_PCT,
    crash_pct: float = DEFAULT_CRASH_BUDGET_PCT,
    fed_pct: float = DEFAULT_FED_BUDGET_PCT,
    crypto_stock_shared: bool = True,
) -> dict[str, float]:
    """Return lane fractions (does not force sum==1; callers own knobs).

    Also exposes ``spot`` (crypto+stock) and ``crypto_stock_shared`` (1.0/0.0)
    so snapshots can show the combined 65% pool when sharing is on.
    """
    crypto = float(crypto_pct)
    stock = float(stock_pct)
    shared_on = bool(crypto_stock_shared)
    return {
        "crypto": crypto,
        "stock": stock,
        "futures": float(futures_pct),
        "crash": float(crash_pct),
        "fed": float(fed_pct),
        "spot": crypto + stock,
        "crypto_stock_shared": 1.0 if shared_on else 0.0,
    }


def lane_budgets_usd(
    account_value_usd: float,
    *,
    crypto_pct: float = DEFAULT_CRYPTO_BUDGET_PCT,
    stock_pct: float = DEFAULT_STOCK_BUDGET_PCT,
    futures_pct: float = DEFAULT_FUTURES_BUDGET_PCT,
    crash_pct: float = DEFAULT_CRASH_BUDGET_PCT,
    fed_pct: float = DEFAULT_FED_BUDGET_PCT,
    crypto_stock_shared: bool = True,
) -> dict[str, float]:
    """Dollar budgets per lane from total account value."""
    av = max(0.0, float(account_value_usd))
    crypto = float(crypto_pct)
    stock = float(stock_pct)
    spot = spot_shared_budget_usd(av, crypto, stock)
    return {
        "account_value_usd": av,
        "crypto_usd": av * crypto,
        "stock_usd": av * stock,
        "spot_usd": spot,  # AV*(crypto+stock); used when sharing is on
        "futures_usd": av * float(futures_pct),
        "crash_usd": av * float(crash_pct),
        "fed_usd": av * float(fed_pct),
    }


def spot_shared_budget_usd(
    account_value_usd: float,
    crypto_pct: float = DEFAULT_CRYPTO_BUDGET_PCT,
    stock_pct: float = DEFAULT_STOCK_BUDGET_PCT,
) -> float:
    """Combined crypto+stock spot budget: AV * (crypto_pct + stock_pct)."""
    av = max(0.0, float(account_value_usd))
    return av * (float(crypto_pct) + float(stock_pct))


def spot_open_notional_usd(
    crypto_positions: list[Any] | None = None,
    stock_positions: list[Any] | None = None,
) -> float:
    """Sum of open notional across crypto and stock sibling ledgers."""
    return open_notional_usd(crypto_positions or []) + open_notional_usd(
        stock_positions or []
    )


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
