"""Future Trader — paper Coinbase Advanced Trade perpetual futures lane.

Isolated sqlite book. Long-only. Never places live futures or crypto orders.
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
