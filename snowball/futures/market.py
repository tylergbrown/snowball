"""Coinbase perpetual futures marks for Future Trader (paper). Never places orders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from snowball.models import Ticker

log = logging.getLogger("snowball.futures.market")

# Coinbase Advanced Trade product_id → ccxt unified symbol
PRODUCT_TO_CCXT: dict[str, str] = {
    "SPY-PERP-INTX": "SPY/USDC:USDC",
    "QQQ-PERP-INTX": "QQQ/USDC:USDC",
}

DEFAULT_FUTURES_PRODUCTS: tuple[str, ...] = ("SPY-PERP-INTX", "QQQ-PERP-INTX")


def _expand_env_newlines(value: str) -> str:
    """Turn .env-style \\n escapes into real newlines (CDP PEM). Local copy — no live import."""
    return (value or "").replace("\\n", "\n").replace("\\r", "\r")


def normalize_futures_product(product: str) -> str:
    """Normalize to Coinbase product id (e.g. SPY-PERP-INTX)."""
    p = product.strip().upper().replace("_", "-")
    if p in PRODUCT_TO_CCXT:
        return p
    # Accept ccxt unified form
    for pid, unified in PRODUCT_TO_CCXT.items():
        if p == unified.upper() or p.replace("/", "-") == unified.upper().replace("/", "-"):
            return pid
        base = unified.split("/")[0].upper()
        if p in (base, f"{base}-PERP", f"{base}-PERP-INTX"):
            return pid
    return p


def to_futures_ccxt_symbol(product: str) -> str:
    pid = normalize_futures_product(product)
    if pid in PRODUCT_TO_CCXT:
        return PRODUCT_TO_CCXT[pid]
    # Fallback: do not use crypto spot to_ccxt_symbol (would mangle SPY-PERP-INTX)
    if "/" in pid:
        return pid
    raise ValueError(f"unknown futures product {product!r}; known: {sorted(PRODUCT_TO_CCXT)}")


class CoinbaseFuturesMarket:
    """Public (optionally authenticated) Coinbase perp OHLCV/tickers. Never creates orders."""

    mark_source = "coinbase_perp"

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        exchange: object | None = None,
        timeout: float = 20000,
    ) -> None:
        self._timeout = timeout
        if exchange is not None:
            self._exchange = exchange
            return
        import ccxt  # lazy so unit tests can inject a fake

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": int(timeout),
        }
        key = (api_key or "").strip()
        secret = _expand_env_newlines(api_secret or "").strip()
        if key and secret:
            opts["apiKey"] = key
            opts["secret"] = secret
            if api_passphrase:
                opts["password"] = api_passphrase
            log.info("futures market using authenticated Coinbase (marks only)")
        else:
            log.info("futures market using public Coinbase (marks only)")
        self._exchange = ccxt.coinbase(opts)

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        symbol = to_futures_ccxt_symbol(product)
        rows = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)  # type: ignore[attr-defined]
        return [list(map(float, row[:6])) for row in rows]

    def fetch_ticker(self, product: str) -> Ticker:
        pid = normalize_futures_product(product)
        symbol = to_futures_ccxt_symbol(pid)
        raw = self._exchange.fetch_ticker(symbol)  # type: ignore[attr-defined]
        last = raw.get("last")
        bid = raw.get("bid")
        ask = raw.get("ask")
        ts_ms = raw.get("timestamp")
        if ts_ms:
            ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Ticker(
            product=pid,
            last=float(last) if last is not None else None,
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            ts=ts,
        )
