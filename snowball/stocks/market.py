"""Public equity marks for STOCK PAPER — Yahoo Finance chart API (no orders).

Coinbase Advanced Trade currently exposes equity *perps* (e.g. NVDA-PERP-INTX),
not spot US equities under the CDP key we use for crypto. Paper marks therefore
come from Yahoo public data and are labeled ``yahoo_paper``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from snowball.models import Ticker
from snowball.stocks.universe import normalize_symbol

log = logging.getLogger("snowball.stocks.market")

_UA = "SnowballStockPaper/1.0 (paper marks; no orders)"

# Yahoo interval → snowball timeframe
_INTERVAL = {
    "15m": ("15m", "10d"),
    "5m": ("5m", "5d"),
    "1d": ("1d", "1y"),
}


class YahooPaperMarket:
    """Fetch public OHLCV / tickers for US equities. Never places orders."""

    mark_source = "yahoo_paper"

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._cache: dict[tuple[str, str], list[list[float]]] = {}

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        sym = normalize_symbol(product)
        tf = timeframe.strip().lower()
        if tf not in _INTERVAL:
            # Map unknown to daily
            tf = "1d"
        interval, range_ = _INTERVAL[tf]
        rows = self._chart_ohlcv(sym, interval=interval, range_=range_)
        if not rows and tf != "1d":
            # Fall back to daily when intraday is empty (weekend / after-hours / thin)
            rows = self._chart_ohlcv(sym, interval="1d", range_="1y")
        if limit > 0:
            rows = rows[-limit:]
        return rows

    def fetch_ticker(self, product: str) -> Ticker:
        sym = normalize_symbol(product)
        meta = self._chart_meta(sym)
        last = meta.get("regularMarketPrice")
        if last is None:
            last = meta.get("previousClose")
        # Yahoo chart meta rarely has bid/ask; leave None
        ts_raw = meta.get("regularMarketTime")
        if isinstance(ts_raw, (int, float)) and ts_raw > 0:
            ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Ticker(
            product=sym,
            last=float(last) if last is not None else None,
            bid=None,
            ask=None,
            ts=ts,
        )

    def _chart_meta(self, symbol: str) -> dict[str, Any]:
        data = self._fetch_chart(symbol, interval="1d", range_="5d")
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return {}
        return dict(result[0].get("meta") or {})

    def _chart_ohlcv(self, symbol: str, *, interval: str, range_: str) -> list[list[float]]:
        key = (symbol, interval)
        data = self._fetch_chart(symbol, interval=interval, range_=range_)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return []
        block = result[0]
        ts_list = block.get("timestamp") or []
        quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        vols = quote.get("volume") or []
        rows: list[list[float]] = []
        for i, ts in enumerate(ts_list):
            try:
                c = closes[i]
                if c is None:
                    continue
                o = opens[i] if i < len(opens) and opens[i] is not None else c
                h = highs[i] if i < len(highs) and highs[i] is not None else c
                low = lows[i] if i < len(lows) and lows[i] is not None else c
                v = vols[i] if i < len(vols) and vols[i] is not None else 0.0
                rows.append(
                    [
                        float(ts) * 1000.0,
                        float(o),
                        float(h),
                        float(low),
                        float(c),
                        float(v),
                    ]
                )
            except (TypeError, ValueError, IndexError):
                continue
        self._cache[key] = rows
        return rows

    def _fetch_chart(self, symbol: str, *, interval: str, range_: str) -> dict[str, Any]:
        q = urllib.parse.urlencode({"interval": interval, "range": range_})
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "yahoo chart fetch failed",
                extra={"data": {"symbol": symbol, "interval": interval, "error": str(exc)}},
            )
            return {}


def resolve_coinbase_equity_ids(wanted: list[str]) -> dict[str, str]:
    """Best-effort map ticker → Coinbase product id if spot equity exists.

    Current CDP universe typically has *-PERP-INTX swaps only — those are NOT
    used for this paper equity lane. Returns empty or sparse map; callers keep Yahoo.
    """
    out: dict[str, str] = {}
    try:
        import ccxt  # lazy
    except ImportError:
        return out
    try:
        ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 15000})
        markets = ex.load_markets()
    except Exception as exc:  # noqa: BLE001
        log.info("coinbase equity probe skipped", extra={"data": {"error": str(exc)}})
        return out
    want = {normalize_symbol(w) for w in wanted}
    # Crypto spot often reuses ticker strings (AI, META, …). US equity spot is not
    # listed on Advanced Trade for this key — only *-PERP-INTX swaps showed up in
    # probes — so we refuse to treat any Coinbase spot market as an equity id.
    for _sym, m in markets.items():
        pid = str(m.get("id") or "")
        if "PERP" in pid.upper() or m.get("type") == "swap":
            continue
        # Explicit equity product types only (none observed today).
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        ptype = str(info.get("product_type") or info.get("productType") or "").lower()
        if "equity" not in ptype and "stock" not in ptype:
            continue
        base = normalize_symbol(str(m.get("base") or ""))
        if base in want and m.get("spot"):
            out[base] = pid
    return out
