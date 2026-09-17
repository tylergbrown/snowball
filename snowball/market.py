from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Protocol

from snowball.config import Settings
from snowball.models import Ticker
from snowball import rate_limit as public_rl

log = logging.getLogger("snowball.market")


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
    """Public Coinbase data via ccxt. Never places orders.

    Uses enableRateLimit plus a process-wide public spacer and 429 backoff so
    crypto / stock / futures / crash / fed lanes sharing one egress IP do not
    stampede Coinbase public REST.
    """

    def __init__(self, settings: Settings, exchange: object | None = None) -> None:
        self._settings = settings
        self._min_interval = max(
            0.05,
            float(getattr(settings, "ccxt_rate_limit_ms", 250) or 250) / 1000.0,
        )
        self._ohlcv_cache: dict[tuple[str, str, int], tuple[float, list[list[float]]]] = {}
        self._ohlcv_ttl = max(
            5.0, float(getattr(settings, "ohlcv_cache_ttl_sec", 45) or 45)
        )
        if exchange is not None:
            self._exchange = exchange
            return
        import ccxt  # lazy so tests can skip ccxt

        klass = getattr(ccxt, settings.exchange_id)
        rate_ms = int(getattr(settings, "ccxt_rate_limit_ms", 250) or 250)
        self._exchange = klass(
            {
                "enableRateLimit": True,
                "rateLimit": max(50, rate_ms),
                "timeout": 20000,
            }
        )

    def _call(self, fn, *args, **kwargs):
        """Throttle + retry on RateLimitExceeded / 429."""
        import ccxt

        attempts = int(getattr(self._settings, "public_fetch_retries", 4) or 4)
        last_exc: Exception | None = None
        for attempt in range(max(1, attempts)):
            public_rl.wait_turn(self._min_interval)
            try:
                return fn(*args, **kwargs)
            except ccxt.RateLimitExceeded as exc:
                last_exc = exc
                backoff = min(30.0, (2 ** attempt) * 1.5)
                log.warning(
                    "coinbase public 429; backoff %.1fs (attempt %s/%s)",
                    backoff,
                    attempt + 1,
                    attempts,
                )
                public_rl.penalize(backoff)
                time.sleep(backoff)
            except Exception as exc:
                # Some ccxt builds surface 429 as ExchangeNotAvailable / NetworkError
                msg = str(exc).lower()
                if "429" in msg or "rate limit" in msg or "too many requests" in msg:
                    last_exc = exc
                    backoff = min(30.0, (2 ** attempt) * 1.5)
                    log.warning(
                        "coinbase public rate soft-fail; backoff %.1fs (%s)",
                        backoff,
                        type(exc).__name__,
                    )
                    public_rl.penalize(backoff)
                    time.sleep(backoff)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        key = (product, timeframe, int(limit))
        now = time.monotonic()
        cached = self._ohlcv_cache.get(key)
        if cached is not None and (now - cached[0]) < self._ohlcv_ttl:
            return [list(row) for row in cached[1]]

        symbol = to_ccxt_symbol(product)
        rows = self._call(
            self._exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit
        )
        out = [list(map(float, row[:6])) for row in rows]
        self._ohlcv_cache[key] = (now, out)
        return [list(row) for row in out]

    def fetch_ticker(self, product: str) -> Ticker:
        symbol = to_ccxt_symbol(product)
        raw = self._call(self._exchange.fetch_ticker, symbol)
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
