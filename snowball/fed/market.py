"""Fed Desk market — same Coinbase INTX perps as Future Trader / Crash Guard."""

from __future__ import annotations

from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    PRODUCT_TO_CCXT,
    CoinbaseFuturesMarket,
    estimate_account_value_usd,
    normalize_futures_product,
    parse_futures_order_fill,
    round_amount_down,
    to_futures_ccxt_symbol,
)

DEFAULT_FED_PRODUCTS = DEFAULT_FUTURES_PRODUCTS
FedMarket = CoinbaseFuturesMarket

__all__ = [
    "DEFAULT_FED_PRODUCTS",
    "PRODUCT_TO_CCXT",
    "FedMarket",
    "CoinbaseFuturesMarket",
    "estimate_account_value_usd",
    "normalize_futures_product",
    "parse_futures_order_fill",
    "round_amount_down",
    "to_futures_ccxt_symbol",
]
