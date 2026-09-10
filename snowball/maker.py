"""Maker-limit pricing and settle policy (fee-saving post-only style).

Buy: rest at/near best bid (small offset inside the spread when wide), never cross ask.
Sell: rest at/near best ask, never cross bid.

Timeout: DEFAULT_MAKER_TIMEOUT_SEC (90s). Unfilled entry limits are canceled —
callers must not ledger a fake fill. Partial fills: keep filled qty, cancel rest.

Exits: prefer maker sells when green. Urgent Future Trader session close
(15:55–16:00 ET, last ~2 minutes) may cancel/replace once then carefully
fall back to market so the session does not miss the close window.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

DEFAULT_MAKER_TIMEOUT_SEC = 90.0
# Shorter wait before cancel/replace during FT session close.
URGENT_MAKER_TIMEOUT_SEC = 20.0
# Place this fraction of the spread inside (buy above bid / sell below ask).
INSIDE_SPREAD_FRAC = 0.25


def maker_buy_price(
    bid: float | None,
    ask: float | None,
    last: float | None = None,
) -> float | None:
    """Limit buy price that should rest as maker (at/near bid, strictly below ask)."""
    _ = last  # reserved; do not invent a book from last alone
    if bid is None or bid <= 0:
        return None
    px = float(bid)
    if ask is not None and ask > bid:
        inside = bid + (ask - bid) * INSIDE_SPREAD_FRAC
        # Stay strictly below ask so we provide liquidity (post-only safe).
        cap = ask * (1.0 - 1e-8)
        px = min(max(bid, inside), cap)
        if px >= ask:
            px = bid
    return px if px > 0 else None


def maker_sell_price(
    bid: float | None,
    ask: float | None,
    last: float | None = None,
) -> float | None:
    """Limit sell price that should rest as maker (at/near ask, strictly above bid)."""
    _ = last
    if ask is None or ask <= 0:
        return None
    px = float(ask)
    if bid is not None and ask > bid:
        inside = ask - (ask - bid) * INSIDE_SPREAD_FRAC
        floor = bid * (1.0 + 1e-8)
        px = max(min(ask, inside), floor)
        if px <= bid:
            px = ask
    return px if px > 0 else None


def bba_from_order_book(book: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Extract (best_bid, best_ask) from a ccxt-style order book."""
    if not isinstance(book, dict):
        return None, None
    bid = ask = None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if bids and isinstance(bids[0], (list, tuple)) and len(bids[0]) >= 1:
        try:
            bid = float(bids[0][0])
        except (TypeError, ValueError):
            bid = None
    if asks and isinstance(asks[0], (list, tuple)) and len(asks[0]) >= 1:
        try:
            ask = float(asks[0][0])
        except (TypeError, ValueError):
            ask = None
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask <= 0:
        ask = None
    return bid, ask


def session_close_urgent(
    now: datetime,
    *,
    exit_start: time | None = None,
    exit_end: time | None = None,
) -> bool:
    """True in the last ~2 minutes of the FT exit window (market fallback allowed)."""
    from snowball.futures.session import DEFAULT_EXIT_END, DEFAULT_EXIT_START, in_exit_window, to_et

    start = exit_start or DEFAULT_EXIT_START
    end = exit_end or DEFAULT_EXIT_END
    if not in_exit_window(now, start=start, end=end):
        return False
    t = to_et(now).time()
    # Urgent from (exit_end - 2 minutes) onward within the window.
    end_minutes = end.hour * 60 + end.minute
    t_minutes = t.hour * 60 + t.minute
    return t_minutes >= end_minutes - 2


SMA_STRATEGY_IDS: frozenset[str] = frozenset({"sma_5m", "sma_15m", "sma_1d"})


def min_take_profit_for_strategy(
    strategy_id: str,
    *,
    min_take_profit_pct: float,
    sma_min_take_profit_pct: float,
) -> float:
    """SMA ids use the higher SMA floor; everything else uses the global min TP."""
    sid = (strategy_id or "").strip().lower()
    if sid in SMA_STRATEGY_IDS:
        return float(sma_min_take_profit_pct)
    return float(min_take_profit_pct)
