"""Future Trader — Coinbase Advanced Trade perpetual futures lane.

Session day-trade engine (America/New_York). Dual-gated live:
FUTURES_MODE=live AND FUTURES_LIVE_ENABLED=true. Paper path for tests.
"""

from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    PRODUCT_TO_CCXT,
    to_futures_ccxt_symbol,
)

__all__ = [
    "DEFAULT_FUTURES_PRODUCTS",
    "PRODUCT_TO_CCXT",
    "to_futures_ccxt_symbol",
]
