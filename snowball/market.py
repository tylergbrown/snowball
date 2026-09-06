from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from snowball.config import Settings
from snowball.models import Ticker


def to_ccxt_symbol(product: str) -> str:
    """Coinbase product BTC-USD -> ccxt BTC/USD."""
    p = product.strip().upper().replace("_", "-")
    if "/" in p:
        return p
    return p.replace("-", "/", 1)


def from_ccxt_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("/", "-")


class MarketData(Protocol):
    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        ...

    def fetch_ticker(self, product: str) -> Ticker:
        ...


class CcxtMarket:
    """Public Coinbase data via ccxt. Never places orders."""

    def __init__(self, settings: Settings, exchange: object | None = None) -> None:
        self._settings = settings
        if exchange is not None:
            self._exchange = exchange
            return
        import ccxt  # lazy so tests can skip ccxt

        klass = getattr(ccxt, settings.exchange_id)
        self._exchange = klass(
            {
                "enableRateLimit": True,
                "timeout": 20000,
            }
        )

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        symbol = to_ccxt_symbol(product)
        rows = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [list(map(float, row[:6])) for row in rows]

    def fetch_ticker(self, product: str) -> Ticker:
        symbol = to_ccxt_symbol(product)
        raw = self._exchange.fetch_ticker(symbol)
        last = raw.get("last")
        bid = raw.get("bid")
        ask = raw.get("ask")
        ts_ms = raw.get("timestamp")
        if ts_ms:
            ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Ticker(
            product=product,
            last=float(last) if last is not None else None,
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            ts=ts,
        )


def fill_price(ticker: Ticker, side: str, slippage_bps: float) -> float:
    ref = ticker.reference
    if ref is None:
        raise ValueError(f"no public price for {ticker.product}")
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return ref * (1.0 + slip)
    if side == "sell":
        return ref * (1.0 - slip)
    raise ValueError(f"bad side {side}")
