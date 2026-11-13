"""Multi-year DAILY OHLC history with a small on-disk cache (public data only, no keys).

* Crypto (BTC, ETH, held spot alts): Coinbase Exchange public candles (the venue we trade;
  Yahoo crypto symbols are ambiguous for many alts), paginated 300 days per request.
* US5 / TEK / crude / macro: Yahoo chart API 1d bars, range 20y (SPY, QQQ, CL=F, ^IRX, ^VIX).

Rows are [ts_ms, open, high, low, close, volume], oldest first. Only COMPLETE daily bars are
returned (a bar is dropped while ts + 24h is still in the future), so a forecast never uses a
partial day.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("snowball.forward.history")

Bars = list[list[float]]
DAY_MS = 86_400_000.0
_UA = "SnowballForwardGuidance/1.0 (public daily candles; no orders)"
CRYPTO_START_TS = 1451606400  # 2016-01-01 UTC
CB_MAX_PER_REQ = 300


def complete_only(rows: Bars, now_ms: float | None = None) -> Bars:
    now_ms = time.time() * 1000.0 if now_ms is None else float(now_ms)
    return [r for r in rows if float(r[0]) + DAY_MS <= now_ms]


def _get_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_yahoo_daily(symbol: str, range_: str = "20y") -> Bars:
    q = urllib.parse.urlencode({"interval": "1d", "range": range_})
    d = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}")
    res = ((d or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    blk = res[0]
    ts = blk.get("timestamp") or []
    qt = ((blk.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, lo, c, v = (qt.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out: Bars = []
    for i, t in enumerate(ts):
        try:
            cl = c[i]
            if cl is None or float(cl) <= 0:
                continue
            op = o[i] if i < len(o) and o[i] is not None else cl
            hi = h[i] if i < len(h) and h[i] is not None else cl
            low = lo[i] if i < len(lo) and lo[i] is not None else cl
            vol = v[i] if i < len(v) and v[i] is not None else 0.0
            out.append([float(t) * 1000.0, float(op), float(max(hi, cl, op)), float(min(low, cl, op)), float(cl), float(vol)])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def fetch_coinbase_daily(product: str, since_ts: int | None = None, *, sleep_s: float = 0.25,
                         max_requests: int = 40) -> Bars:
    """Coinbase Exchange public daily candles [time, low, high, open, close, volume] -> Bars."""
    end = int(time.time())
    start_floor = int(since_ts if since_ts is not None else CRYPTO_START_TS)
    rows: dict[int, list[float]] = {}
    n_req = 0
    cur_end = end
    while cur_end > start_floor and n_req < max_requests:
        cur_start = max(start_floor, cur_end - (CB_MAX_PER_REQ - 1) * 86400)
        url = (f"https://api.exchange.coinbase.com/products/{urllib.parse.quote(product)}/candles?"
               + urllib.parse.urlencode({"granularity": 86400, "start": cur_start, "end": cur_end}))
        n_req += 1
        try:
            d = _get_json(url)
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc) or "400" in str(exc):
                break
            raise
        if not isinstance(d, list) or not d:
            break
        for r in d:
            try:
                t, low, hi, op, cl, vol = (float(x) for x in r[:6])
            except (TypeError, ValueError):
                continue
            if cl > 0:
                rows[int(t)] = [t * 1000.0, op, max(hi, op, cl), min(low, op, cl), cl, vol]
        oldest = min(int(r[0]) for r in d)
        if oldest > cur_start + 2 * 86400:
            break  # nothing older than this window: listing date reached
        cur_end = cur_start - 1
        if sleep_s:
            time.sleep(sleep_s)
    return [rows[k] for k in sorted(rows)]


class DailyHistory:
    """Cached daily history per source key ('cb:BTC-USD', 'yh:SPY'), refreshed every ttl."""

    def __init__(self, cache_dir: Path | None = None, *, ttl_sec: float = 6 * 3600.0,
                 fetch: Callable[[str], Bars] | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = float(ttl_sec)
        self._fetch = fetch
        self._mem: dict[str, tuple[float, Bars]] = {}
        self._lock = threading.RLock()
        self.errors: dict[str, str] = {}

    def _path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe = key.replace(":", "_").replace("^", "_").replace("=", "_").replace("/", "_")
        return self.cache_dir / f"{safe}.json"

    def _load_disk(self, key: str) -> tuple[float, Bars] | None:
        p = self._path(key)
        if p is None or not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
            return float(d.get("fetched_at") or 0.0), list(d.get("rows") or [])
        except (OSError, ValueError):
            return None

    def _save_disk(self, key: str, rows: Bars) -> None:
        p = self._path(key)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": time.time(), "key": key, "rows": rows}))
            tmp.replace(p)
        except OSError:
            log.info("forward history cache write failed", extra={"data": {"key": key}})

    def _default_fetch(self, key: str, cached: Bars | None) -> Bars:
        kind, sym = key.split(":", 1)
        if kind == "yh":
            return fetch_yahoo_daily(sym)
        if kind == "cb":
            since = None
            if cached:
                since = int(float(cached[-1][0]) / 1000.0) - 5 * 86400
            new = fetch_coinbase_daily(sym, since_ts=since)
            if cached and new:
                merged = {int(float(r[0])): r for r in cached}
                merged.update({int(float(r[0])): r for r in new})
                return [merged[k] for k in sorted(merged)]
            return new or list(cached or [])
        raise ValueError(f"unknown history source {kind}")

    def get(self, key: str, now_ms: float | None = None) -> Bars:
        with self._lock:
            hit = self._mem.get(key)
            if hit is None:
                hit = self._load_disk(key)
                if hit is not None:
                    self._mem[key] = hit
            if hit is not None and time.time() - hit[0] < self.ttl and hit[1]:
                return complete_only(hit[1], now_ms)
        try:
            rows = (self._fetch(key) if self._fetch else self._default_fetch(key, hit[1] if hit else None)) or []
            self.errors.pop(key, None)
        except Exception as exc:  # noqa: BLE001
            self.errors[key] = str(exc)[:160]
            log.info("forward history fetch failed", extra={"data": {"key": key, "error": str(exc)[:160]}})
            rows = []
        rows = [r for r in rows if r and len(r) >= 5 and float(r[4] or 0) > 0]
        with self._lock:
            if rows:
                self._mem[key] = (time.time(), rows)
                self._save_disk(key, rows)
                return complete_only(rows, now_ms)
            return complete_only(hit[1], now_ms) if hit else []
