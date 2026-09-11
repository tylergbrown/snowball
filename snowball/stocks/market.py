"""Stock marks + live Coinbase CFM index products.

Paper marks come from Yahoo Finance (``yahoo_paper``) for the broad equity
universe. Live stock trading uses Coinbase CFM CDE index perps
(``US5-19DEC30-CDE``, ``TEK-19DEC30-CDE``) via the shared futures market path —
not single-name ``*-PERP-INTX``. Broader names stay research/watch only.
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
            tf = "1d"
        interval, range_ = _INTERVAL[tf]
        rows = self._chart_ohlcv(sym, interval=interval, range_=range_)
        if not rows and tf != "1d":
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
    """Best-effort map ticker → Coinbase *spot* equity product id (usually empty).

    Advanced Trade does not list US equity spot for typical CDP keys. Prefer
    :func:`resolve_coinbase_equity_perps` for live stock trading.
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
    for _sym, m in markets.items():
        pid = str(m.get("id") or "")
        if "PERP" in pid.upper() or m.get("type") == "swap":
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        ptype = str(info.get("product_type") or info.get("productType") or "").lower()
        if "equity" not in ptype and "stock" not in ptype:
            continue
        base = normalize_symbol(str(m.get("base") or ""))
        if base in want and m.get("spot"):
            out[base] = pid
    return out


def resolve_coinbase_equity_perps(
    wanted: list[str],
    *,
    markets: dict[str, Any] | None = None,
    exclude_bases: set[str] | None = None,
) -> dict[str, str]:
    """Map watchlist ticker → ``{SYM}-PERP-INTX`` when that INTX equity perp exists.

    Returns only symbols with a listed Coinbase INTX perp. Callers skip names
    without a perp (research/watch only). ``exclude_bases`` skips tickers already
    reserved by Future Trader (e.g. SPY/QQQ) to avoid double-trading one product.
    """
    out: dict[str, str] = {}
    want = {normalize_symbol(w) for w in wanted}
    skip = {normalize_symbol(x) for x in (exclude_bases or set())}
    market_map = markets
    if market_map is None:
        try:
            import ccxt  # lazy

            ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
            market_map = ex.load_markets()
        except Exception as exc:  # noqa: BLE001
            log.info(
                "coinbase INTX equity perp probe skipped",
                extra={"data": {"error": str(exc)}},
            )
            return out
    assert market_map is not None
    for _sym, m in market_map.items():
        pid = str(m.get("id") or "").upper()
        if not pid.endswith("-PERP-INTX") and "PERP-INTX" not in pid:
            # Also accept type=swap with INTX in id
            if not (m.get("type") == "swap" and "INTX" in pid):
                continue
        base = normalize_symbol(str(m.get("base") or pid.split("-")[0]))
        if base in want and base not in skip:
            # Canonical Coinbase product id
            canon = pid if pid.endswith("-PERP-INTX") else f"{base}-PERP-INTX"
            out[base] = canon
    return out


def ticker_to_perp_product(ticker: str) -> str:
    """``AAPL`` → ``AAPL-PERP-INTX`` (does not prove the market exists)."""
    base = normalize_symbol(ticker)
    if base.endswith("-PERP-INTX"):
        return base
    if base.endswith("-PERP"):
        return f"{base}-INTX" if not base.endswith("-PERP-INTX") else base
    return f"{base}-PERP-INTX"


class StockMarkRouter:
    """Route marks: Coinbase CFM for mapped live products, Yahoo otherwise.

    Never places orders. Live orders go through ``CoinbaseFuturesMarket`` in the
    stock engine (same CFM CDE path as Future Trader / Crash / Fed).
    """

    def __init__(
        self,
        *,
        yahoo: YahooPaperMarket | None = None,
        coinbase: Any | None = None,
        perp_map: dict[str, str] | None = None,
        prefer_coinbase: bool = False,
    ) -> None:
        self.yahoo = yahoo or YahooPaperMarket()
        self.coinbase = coinbase
        self.perp_map = dict(perp_map or {})
        self.prefer_coinbase = bool(prefer_coinbase)

    @property
    def mark_source(self) -> str:
        if self.prefer_coinbase and self.perp_map and self.coinbase is not None:
            # Live stock is CFM CDE; keep legacy label only if map is INTX-shaped
            sample = next(iter(self.perp_map.values()), "")
            if str(sample).upper().endswith("-CDE"):
                return "coinbase_cfm_cde"
            return "coinbase_intx_perp"
        return getattr(self.yahoo, "mark_source", "yahoo_paper")

    def _perp_id(self, product: str) -> str | None:
        """Resolve order/mark product id (CFM ``*-CDE`` or legacy INTX)."""
        from snowball.futures.market import is_cfm_product, normalize_futures_product

        raw = (product or "").strip()
        if not raw:
            return None
        # Direct CFM / alias (SPY → US5-…)
        try:
            norm = normalize_futures_product(raw)
        except Exception:
            norm = raw.upper()
        if is_cfm_product(norm) and norm in self.perp_map.values():
            return norm
        if norm in self.perp_map:
            return self.perp_map[norm]
        sym = normalize_symbol(product)
        if sym in self.perp_map:
            return self.perp_map[sym]
        # Identity: live map keys are already CFM product ids
        if raw in self.perp_map:
            return self.perp_map[raw]
        if norm in self.perp_map:
            return self.perp_map[norm]
        return None

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        pid = self._perp_id(product)
        if self.prefer_coinbase and pid and self.coinbase is not None:
            try:
                return self.coinbase.fetch_ohlcv(pid, timeframe, limit)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "coinbase stock perp ohlcv failed; falling back to Yahoo",
                    extra={"data": {"product": product, "perp": pid, "error": str(exc)}},
                )
        return self.yahoo.fetch_ohlcv(product, timeframe, limit)

    def fetch_ticker(self, product: str) -> Ticker:
        sym = normalize_symbol(product)
        pid = self._perp_id(product)
        if self.prefer_coinbase and pid and self.coinbase is not None:
            try:
                t = self.coinbase.fetch_ticker(pid)
                return Ticker(
                    product=pid,
                    last=t.last,
                    bid=t.bid,
                    ask=t.ask,
                    ts=t.ts,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "coinbase stock perp ticker failed; falling back to Yahoo",
                    extra={"data": {"product": product, "perp": pid, "error": str(exc)}},
                )
        return self.yahoo.fetch_ticker(product)
