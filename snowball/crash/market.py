"""Crash Guard market — same Coinbase CFM CDE index perps as Future Trader."""

from __future__ import annotations

from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    PRODUCT_TO_CCXT,
    CoinbaseFuturesMarket,
    estimate_account_value_usd,
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
    parse_futures_order_fill,
    round_amount_down,
    to_futures_ccxt_symbol,
)

# Alias: Crash Guard trades the same index perps.
DEFAULT_CRASH_PRODUCTS = DEFAULT_FUTURES_PRODUCTS

# Re-export under crash names for clarity at call sites.
CrashMarket = CoinbaseFuturesMarket

__all__ = [
    "DEFAULT_CRASH_PRODUCTS",
    "PRODUCT_TO_CCXT",
    "CrashMarket",
    "CoinbaseFuturesMarket",
    "estimate_account_value_usd",
    "normalize_futures_product",
    "parse_futures_order_fill",
    "round_amount_down",
    "to_futures_ccxt_symbol",
    "is_cfm_product",
]
