"""Probability of success for research proposals (Tb 3:45 AM CT 10/3).

Transparent, history-based scoring. No model is "trained"; every number is a counted
frequency from real price history (Yahoo 1h bars, the same public source the stock lane
already uses) or from the bot's own stored research history, shrunk toward the base rate
when the sample is small. Nothing is inflated: a family with no measured track record
gets NO credit (delta 0).

Definition of success (per proposal, using the bracket the lane would really trade):
  * futures (research_idea lane): target = FUTURES_MOMENTUM_TAKE_PROFIT_PCT (0.8%);
    stop = the real futures stop (closer of FUTURES_STOP_PCT 8% for US5/TEK and
    FUTURES_STOP_USD_PER_CONTRACT $250 net of fees, from snowball.futures.stops).
  * crypto spot: target = effective research_idea take-profit floor (MIN_TAKE_PROFIT_PCT
    + FEE_BUFFER_PCT); no stop (never-sell-red), so success = target touched in time.
  * non-tradable tickers (stocks etc., log only): reference bracket
    RESEARCH_PROB_REF_TARGET_PCT / RESEARCH_PROB_REF_STOP_PCT (3% / 3%).
  Success = target touched before stop within RESEARCH_PROB_HORIZON_HOURS (72h) of entry
  at the bar close. Both touched in the same 1h bar counts as a STOP (conservative). Neither
  touched in the horizon counts as NOT a success.

Score:
  1. p_base  = (wins + 1) / (n + 2) over every historical 1h start in the lookback.
  2. p_tech  = technical confirmation: same frequency restricted to historical starts whose
     trend state (close vs SMA50 of 1h closes) matches today's, shrunk toward p_base with
     TECH_PSEUDO effective observations (overlapping windows are converted to an
     effective independent count n * bar_hours / horizon_hours).
  3. research delta per agreeing source family = logit(acc_shrunk) - logit(base_mean),
     where acc is the family's measured directional hit rate on its own past calls
     (reference bracket, same horizon) plus closed research_idea trades from that
     family, shrunk with FAMILY_PSEUDO observations toward the base rate of those
     same calls. Each family delta is capped at +/-0.75 logit; the sum at +/-1.0.
  4. p = sigmoid(logit(p_tech) + sum(deltas)), clipped to [0.01, 0.99].
  Break-even p = stop / (target + stop) (+fees); EV per $ = p*target - (1-p)*stop - fees.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("snowball.research.probability")

TECH_PSEUDO = 10.0
FAMILY_PSEUDO = 20.0
FAMILY_DELTA_CAP = 0.75
TOTAL_DELTA_CAP = 1.0
SMA_BARS = 50
MIN_HISTORY_BARS = 200

# Research ticker / product family -> Yahoo symbol used for price history.
REF_SYMBOL = {"US5": "SPY", "TEK": "QQQ", "BIP": "BTC-USD", "NOL": "CL=F"}
TICKER_SYMBOL = {
    "SPY": "SPY", "SPX": "SPY", "ES": "SPY", "VOO": "SPY", "IVV": "SPY", "US500": "SPY",
    "QQQ": "QQQ", "NDX": "QQQ", "NQ": "QQQ", "US100": "QQQ",
    "BTC": "BTC-USD", "XBT": "BTC-USD", "OIL": "CL=F", "WTI": "CL=F", "CL": "CL=F", "CRUDE": "CL=F",
}

Bars = list[list[float]]  # [ts_ms, o, h, l, c, v]


def logit(p: float) -> float:
    p = min(0.999, max(0.001, float(p)))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def family_root(product: str | None) -> str:
    return str(product or "").upper().split("-", 1)[0]


def history_symbol(ticker: str, lane: str | None, product: str | None,
                   crypto_bases: set[str] | None = None) -> str:
    fam = family_root(product)
    if lane == "futures" and fam in REF_SYMBOL:
        return REF_SYMBOL[fam]
    if lane == "crypto" and product:
        return str(product).upper()
    t = str(ticker or "").upper().lstrip("$")
    if t in TICKER_SYMBOL:
        return TICKER_SYMBOL[t]
    if crypto_bases and t in crypto_bases:
        return f"{t}-USD"
    return t


# ----------------------------------------------------------------- outcomes --


def first_passage(bars: Bars, i: int, side: str, target_pct: float, stop_pct: float | None,
                  horizon_hours: float) -> bool | None:
    """True = target before stop within horizon (entry at close of bar i). None = no future bars."""
    if i >= len(bars) - 1:
        return None
    entry = float(bars[i][4])
    if entry <= 0:
        return None
    t_end = float(bars[i][0]) + horizon_hours * 3600_000.0
    long = side == "long"
    tgt = entry * (1 + target_pct) if long else entry * (1 - target_pct)
    stp = None
    if stop_pct is not None and stop_pct > 0:
        stp = entry * (1 - stop_pct) if long else entry * (1 + stop_pct)
    seen_future = False
    for j in range(i + 1, len(bars)):
        b = bars[j]
        if float(b[0]) > t_end:
            break
        seen_future = True
        hi, lo = float(b[2]), float(b[3])
        hit_t = hi >= tgt if long else lo <= tgt
        hit_s = stp is not None and (lo <= stp if long else hi >= stp)
        if hit_s:
            return False  # stop (also when both touched in the same bar: conservative)
        if hit_t:
            return True
    if not seen_future:
        return None
    if float(bars[-1][0]) < t_end:
        return None  # horizon not complete yet: unknown, excluded
    return False


def sma(closes: list[float], i: int, n: int = SMA_BARS) -> float | None:
    if i + 1 < n:
        return None
    w = closes[i + 1 - n:i + 1]
    return sum(w) / n


def trend_state(bars: Bars, i: int) -> str | None:
    closes = [float(b[4]) for b in bars]
    m = sma(closes, i)
    if m is None:
        return None
    return "up" if closes[i] > m else "down"


def bar_hours(bars: Bars) -> float:
    if len(bars) < 3:
        return 1.0
    gaps = sorted(float(bars[k + 1][0]) - float(bars[k][0]) for k in range(len(bars) - 1))
    return max(1 / 60, gaps[len(gaps) // 2] / 3600_000.0)


def base_rates(bars: Bars, side: str, target_pct: float, stop_pct: float | None,
               horizon_hours: float) -> dict[str, Any]:
    """Unconditional + trend-conditional success frequencies over the history."""
    closes = [float(b[4]) for b in bars]
    out = {"n": 0, "wins": 0, "by_trend": {"up": [0, 0], "down": [0, 0]}}
    for i in range(len(bars) - 1):
        r = first_passage(bars, i, side, target_pct, stop_pct, horizon_hours)
        if r is None:
            continue
        out["n"] += 1
        out["wins"] += int(r)
        m = sma(closes, i)
        if m is not None:
            k = "up" if closes[i] > m else "down"
            out["by_trend"][k][0] += int(r)
            out["by_trend"][k][1] += 1
    return out


def score(bars: Bars, *, side: str, target_pct: float, stop_pct: float | None,
          horizon_hours: float, family_deltas: dict[str, float] | None = None,
          fee_pct: float = 0.0) -> dict[str, Any]:
    """Full probability breakdown for one proposal (pure; easy to test)."""
    if len(bars) < MIN_HISTORY_BARS:
        return {"probability": None, "why": f"price history too short ({len(bars)} bars)"}
    br = base_rates(bars, side, target_pct, stop_pct, horizon_hours)
    n, w = br["n"], br["wins"]
    if n < 50:
        return {"probability": None, "why": f"too few completed historical windows ({n})"}
    p_base = (w + 1.0) / (n + 2.0)
    bh = bar_hours(bars)
    eff = lambda k: k * min(1.0, bh / max(bh, horizon_hours))  # noqa: E731
    state = trend_state(bars, len(bars) - 1)
    p_tech = p_base
    tech = {"state": state, "agrees": None, "wins": None, "n": None}
    if state is not None:
        tw, tn = br["by_trend"][state]
        tech.update({"wins": tw, "n": tn,
                     "agrees": (state == "up") == (side == "long")})
        ne = eff(tn)
        if tn > 0:
            p_tech = (tw / tn * ne + p_base * TECH_PSEUDO) / (ne + TECH_PSEUDO)
    deltas = {k: max(-FAMILY_DELTA_CAP, min(FAMILY_DELTA_CAP, float(v)))
              for k, v in (family_deltas or {}).items()}
    total = max(-TOTAL_DELTA_CAP, min(TOTAL_DELTA_CAP, sum(deltas.values())))
    p = min(0.99, max(0.01, sigmoid(logit(p_tech) + total)))
    out: dict[str, Any] = {
        "probability": round(p, 4), "p_base": round(p_base, 4), "p_tech": round(p_tech, 4),
        "base_wins": w, "base_n": n, "n_effective": round(eff(n), 1), "bar_hours": round(bh, 3),
        "tech": tech, "family_deltas": {k: round(v, 3) for k, v in deltas.items()},
        "research_delta": round(total, 3), "target_pct": target_pct, "stop_pct": stop_pct,
        "horizon_hours": horizon_hours, "history_bars": len(bars),
        "history_from": _iso_ms(bars[0][0]), "history_to": _iso_ms(bars[-1][0]),
    }
    if stop_pct is not None and stop_pct > 0:
        be = (stop_pct + fee_pct) / (target_pct + stop_pct)
        ev = p * target_pct - (1 - p) * stop_pct - fee_pct
        out.update({"break_even_p": round(be, 4), "ev_pct": round(ev, 5)})
    else:
        out.update({"break_even_p": None, "ev_pct": None})
    return out


def _iso_ms(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------- history provider --


class HistoryCache:
    """Yahoo 1h bars per symbol, cached (TTL), with a per-run fetch budget."""

    def __init__(self, fetch: Callable[[str], Bars] | None = None, ttl_sec: float = 6 * 3600.0,
                 range_: str = "180d") -> None:
        self._fetch = fetch
        self._ttl = ttl_sec
        self._range = range_
        self._cache: dict[str, tuple[float, Bars]] = {}
        self._lock = threading.RLock()
        self.budget = 10**9

    def _default_fetch(self, symbol: str) -> Bars:
        from snowball.stocks.market import YahooPaperMarket

        return YahooPaperMarket(timeout=15.0)._chart_ohlcv(symbol, interval="60m", range_=self._range)

    def get(self, symbol: str) -> Bars | None:
        with self._lock:
            hit = self._cache.get(symbol)
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
            if self.budget <= 0:
                return hit[1] if hit else None
            self.budget -= 1
        try:
            rows = (self._fetch or self._default_fetch)(symbol) or []
        except Exception as exc:  # noqa: BLE001
            log.info("history fetch failed", extra={"data": {"symbol": symbol, "error": str(exc)[:120]}})
            rows = []
        rows = [r for r in rows if r and len(r) >= 5 and float(r[4] or 0) > 0]
        with self._lock:
            if rows:
                self._cache[symbol] = (time.monotonic(), rows)
                return rows
            if hit is not None:
                return hit[1]
            self._cache[symbol] = (time.monotonic() - self._ttl + 1800.0, [])  # retry in 30 min
            return None


# ------------------------------------------------------- family track record --


def _index_at(bars: Bars, ts_ms: float) -> int | None:
    lo, hi = 0, len(bars) - 1
    if not bars or ts_ms < float(bars[0][0]):
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if float(bars[mid][0]) <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


def past_calls(main_db: Path, clerk_db: Path | None, *, max_calls: int = 400, max_clerk_calls: int = 150,
               max_clerk_tickers: int = 40) -> list[dict[str, Any]]:
    """Every stored directional call per family (information time, ticker, side)."""
    out: list[dict[str, Any]] = []
    try:
        c = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute(
                "SELECT t.video_id, t.ideas_json, v.published_at FROM yolo_transcripts t "
                "JOIN yolo_videos v ON v.video_id = t.video_id WHERE t.status='ok' "
                "AND v.published_at IS NOT NULL ORDER BY v.published_at DESC").fetchall()
        finally:
            c.close()
        for vid, ideas_raw, pub in rows:
            try:
                ideas = json.loads(ideas_raw or "[]")
            except ValueError:
                continue
            for it in ideas if isinstance(ideas, list) else []:
                bull, bear = int(it.get("bull") or 0), int(it.get("bear") or 0)
                t = str(it.get("ticker") or "").upper()
                if not t or bull == bear:
                    continue
                out.append({"family": "yolo_youtube", "ticker": t, "side": "long" if bull > bear else "short",
                            "at": str(pub), "ref": f"yt:{vid}"})
    except sqlite3.Error:
        pass
    if clerk_db is not None and Path(clerk_db).exists():
        try:
            c = sqlite3.connect(f"file:{clerk_db}?mode=ro", uri=True, timeout=5)
            try:
                rows = c.execute(
                    "SELECT ticker, tx_type, coalesce(filing_date, created_at) FROM clerk_transactions "
                    "WHERE ticker IS NOT NULL AND ticker != '' AND tx_type IN ('P','S')").fetchall()
            finally:
                c.close()
            dated = [(ms, str(t).upper(), tx, at) for t, tx, at in rows
                     if (ms := _parse_ts_ms(str(at))) is not None]
            dated.sort(key=lambda x: x[0], reverse=True)
            tickers: set[str] = set()
            n = 0
            for ms, t, tx, at in dated:
                if t not in tickers and len(tickers) >= max_clerk_tickers:
                    continue
                tickers.add(t)
                out.append({"family": "clerk", "ticker": t, "side": "long" if tx == "P" else "short",
                            "at": str(at), "ref": f"clerk:{t}:{at}"})
                n += 1
                if n >= max_clerk_calls:
                    break
        except sqlite3.Error:
            pass
    return out[:max_calls]


def _parse_ts_ms(s: str) -> float | None:
    try:
        st = str(s).strip().replace("Z", "+00:00")
        if "/" in st:  # House Clerk M/D/YYYY
            mo, dd, yy = st.split("/")[:3]
            st = f"{int(yy):04d}-{int(mo):02d}-{int(dd[:2]):02d}"
        if len(st) == 10:
            st += "T20:00:00+00:00"  # date only (filing date): count from that day's close
        d = datetime.fromisoformat(st)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp() * 1000.0
    except ValueError:
        return None


def family_track_record(calls: list[dict[str, Any]], history: HistoryCache, *, target_pct: float,
                        stop_pct: float, horizon_hours: float, crypto_bases: set[str] | None = None,
                        trade_outcomes: dict[str, list[int]] | None = None) -> dict[str, dict[str, Any]]:
    """Per family: measured hit rate of its past calls vs the base rate of those same windows."""
    fam: dict[str, dict[str, Any]] = {}
    for call in calls:
        f = fam.setdefault(call["family"], {"wins": 0, "n": 0, "base_sum": 0.0, "skipped": 0})
        sym = history_symbol(call["ticker"], None, None, crypto_bases)
        bars = history.get(sym)
        ts = _parse_ts_ms(call["at"])
        i = _index_at(bars, ts) if bars and ts is not None else None
        if i is None:
            f["skipped"] += 1
            continue
        r = first_passage(bars, i, call["side"], target_pct, stop_pct, horizon_hours)
        if r is None:
            f["skipped"] += 1
            continue
        # base for this window = unconditional frequency of the same side on that symbol
        key = (sym, call["side"])
        cache = f.setdefault("_base", {})
        if key not in cache:
            br = base_rates(bars, call["side"], target_pct, stop_pct, horizon_hours)
            cache[key] = (br["wins"] + 1.0) / (br["n"] + 2.0)
        f["wins"] += int(r)
        f["n"] += 1
        f["base_sum"] += cache[key]
    for name, (w, n) in (trade_outcomes or {}).items():
        f = fam.setdefault(name, {"wins": 0, "n": 0, "base_sum": 0.0, "skipped": 0})
        f["trade_wins"], f["trade_n"] = int(w), int(n)
    out: dict[str, dict[str, Any]] = {}
    for name, f in fam.items():
        f.pop("_base", None)
        n = f["n"] + int(f.get("trade_n", 0))
        w = f["wins"] + int(f.get("trade_wins", 0))
        base = (f["base_sum"] / f["n"]) if f["n"] else 0.5
        if n == 0:
            out[name] = {**f, "hit_rate": None, "base": None, "delta": 0.0, "why": "no measured track record → no credit"}
            continue
        acc = (w + FAMILY_PSEUDO * base) / (n + FAMILY_PSEUDO)
        delta = logit(acc) - logit(base)
        out[name] = {**f, "hit_rate": round(w / n, 4), "shrunk": round(acc, 4), "base": round(base, 4),
                     "delta": round(max(-FAMILY_DELTA_CAP, min(FAMILY_DELTA_CAP, delta)), 4)}
    return out
