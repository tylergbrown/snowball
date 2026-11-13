"""Regime classification + conditional base-rate probabilities (pure functions; easy to test).

Regime per horizon (moving-average structure + slope, with ADX and drawdown overrides):
  horizon  fast/slow SMA  slope window   extra rule
  1d       5 / 20         5 bars         ADX(14) < 15 and not a 3/3 score -> range
  1w       20 / 50        10 bars        ADX(14) < 15 and not a 3/3 score -> range
  1m       50 / 200       20 bars        >= 20% below the 1-year high and score <= 1 -> bear
  score = sign(close - slow) + sign(fast - slow) + sign(slow slope)  in [-3, 3]
  bull if score >= 2, bear if score <= -2, else range.

Horizon length in bars: equities/crude 1d=1, 1w=5, 1m=21 (trading days); crypto 1d=1, 1w=7,
1m=30 (calendar days).

Events graded for a forecast made at the close of bar i with horizon h bars:
  up        = close[i+h] > close[i]
  in band   = |close[i+h]/close[i] - 1| <= band, band = 60-day daily log-return stdev * sqrt(h)
  P(continue): bull -> P(up); bear -> P(down) = 1 - P(up); range -> P(in band)
  P(reversal): bull/bear -> 1 - P(continue) (closes the other way); range -> P(breakout) = 1 - P(in band)

Probability (no fitted model; counted frequencies):
  base   = unconditional frequency of the event over all past windows (+1/+2 Laplace)
  p_reg  = frequency in past windows that started in the SAME regime, shrunk toward base with
           A_PRIOR pseudo-observations; the sample is converted to an effective independent count
           n / h because h-bar windows overlap
  p      = p_reg. (Optional macro layer: frequency in windows with the same regime AND the same
           short-rate state, 13-week T-bill 63-day change tightening >= +25bp / easing <= -25bp /
           neutral, shrunk toward p_reg with A_MACRO. USE_MACRO=False: on the 2019-2022 validation
           period it made out-of-sample Brier WORSE on every horizon, so the macro state is shown
           as context only and does not move the probability.)
Shrinkage A_PRIOR=200 was picked on the 2019-2022 validation period (20/50/100/200 tried; more
shrinkage was monotonically better because the regime carries almost no out-of-sample edge);
2023-2026 is the untouched test period shown in the report.
Only windows whose outcome was already known at forecast time are counted (walk-forward), so the
out-of-sample calibration has no look-ahead.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

HORIZONS = ("1d", "1w", "1m")
H_BARS = {"equity": {"1d": 1, "1w": 5, "1m": 21}, "crypto": {"1d": 1, "1w": 7, "1m": 30}}
REGIME_PARAMS = {"1d": (5, 20, 5), "1w": (20, 50, 10), "1m": (50, 200, 20)}
A_PRIOR = 200.0
A_MACRO = 400.0
USE_MACRO = False
VALIDATION = (2019, 2023)  # [from, to) years used to choose A_PRIOR / USE_MACRO
TEST_FROM_YEAR = 2023
VOL_N = 60
ADX_N = 14
ADX_RANGE = 15.0
DD_BEAR = -0.20
MIN_TRAIN = 250
TIGHTEN_BP = 0.25
MACRO_LOOKBACK = 63
REL_BINS = (0.0, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 1.0001)


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def logit(p: float) -> float:
    p = min(0.999, max(0.001, float(p)))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def rolling_sma(c: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    s = 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_max(c: list[float], n: int) -> list[float]:
    from collections import deque

    out: list[float] = []
    dq: deque[int] = deque()
    for i, v in enumerate(c):
        while dq and c[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        out.append(c[dq[0]])
    return out


def rolling_vol(c: list[float], n: int = VOL_N) -> list[float | None]:
    lr = [0.0] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 and c[i] > 0 else 0.0 for i in range(1, len(c))]
    out: list[float | None] = [None] * len(c)
    s = s2 = 0.0
    for i in range(len(c)):
        x = lr[i]
        s += x
        s2 += x * x
        if i > n:
            y = lr[i - n]
            s -= y
            s2 -= y * y
        if i >= n:
            m = s / n
            out[i] = math.sqrt(max(0.0, s2 / n - m * m))
    return out


def adx(highs: list[float], lows: list[float], closes: list[float], n: int = ADX_N) -> list[float | None]:
    """Wilder ADX."""
    L = len(closes)
    out: list[float | None] = [None] * L
    if L < 2 * n + 1:
        return out
    tr_s = pdm_s = ndm_s = 0.0
    dx_vals: list[float] = []
    adx_v: float | None = None
    for i in range(1, L):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            ndm_s += ndm
            if i < n:
                continue
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            ndm_s = ndm_s - ndm_s / n + ndm
        if tr_s <= 0:
            continue
        pdi = 100.0 * pdm_s / tr_s
        ndi = 100.0 * ndm_s / tr_s
        dx = 100.0 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0.0
        if adx_v is None:
            dx_vals.append(dx)
            if len(dx_vals) == n:
                adx_v = sum(dx_vals) / n
                out[i] = adx_v
        else:
            adx_v = (adx_v * (n - 1) + dx) / n
            out[i] = adx_v
    return out


class Features:
    """Per-bar indicators for one market's daily bars."""

    def __init__(self, bars: list[list[float]], asset_class: str = "equity",
                 macro: list[str | None] | None = None) -> None:
        self.bars = bars
        self.asset_class = asset_class
        self.ts = [float(b[0]) for b in bars]
        self.o = [float(b[1]) for b in bars]
        self.h = [float(b[2]) for b in bars]
        self.l = [float(b[3]) for b in bars]
        self.c = [float(b[4]) for b in bars]
        self.sma = {n: rolling_sma(self.c, n) for n in (5, 20, 50, 200)}
        self.adx = adx(self.h, self.l, self.c)
        self.vol = rolling_vol(self.c)
        self.hi_1y = rolling_max(self.c, 365 if asset_class == "crypto" else 252)
        self.macro = macro or [None] * len(bars)
        self._reg: dict[str, list[tuple[str, dict] | None]] = {}

    def __len__(self) -> int:
        return len(self.c)

    def h_bars(self, horizon: str) -> int:
        return H_BARS["crypto" if self.asset_class == "crypto" else "equity"][horizon]

    def regime(self, horizon: str, i: int) -> tuple[str, dict] | None:
        if horizon not in self._reg:
            self._reg[horizon] = [self._regime_at(horizon, k) for k in range(len(self.c))]
        return self._reg[horizon][i]

    def _regime_at(self, horizon: str, i: int) -> tuple[str, dict] | None:
        fast, slow, sl = REGIME_PARAMS[horizon]
        sf, ss = self.sma[fast], self.sma[slow]
        if i < slow - 1 + sl or ss[i] is None or ss[i - sl] is None or sf[i] is None:
            return None
        c = self.c[i]
        s1 = _sign(c - ss[i])
        s2 = _sign(sf[i] - ss[i])
        s3 = _sign(ss[i] - ss[i - sl])
        score = s1 + s2 + s3
        reg = "bull" if score >= 2 else ("bear" if score <= -2 else "range")
        a = self.adx[i]
        dd = c / self.hi_1y[i] - 1.0 if self.hi_1y[i] > 0 else 0.0
        why = []
        if horizon in ("1d", "1w") and a is not None and a < ADX_RANGE and abs(score) < 3 and reg != "range":
            why.append(f"ADX {a:.0f} < {ADX_RANGE:.0f} (weak trend) -> range")
            reg = "range"
        if horizon == "1m" and dd <= DD_BEAR and score <= 1 and reg != "bear":
            why.append(f"{dd:.0%} from 1y high -> bear")
            reg = "bear"
        inputs = {"close": round(c, 6), f"sma{fast}": round(sf[i], 6), f"sma{slow}": round(ss[i], 6),
                  "slope_pct": round(ss[i] / ss[i - sl] - 1.0, 5), "score": score,
                  "adx14": None if a is None else round(a, 1), "dd_1y": round(dd, 4),
                  "vol60_daily": None if self.vol[i] is None else round(self.vol[i], 5)}
        if why:
            inputs["override"] = "; ".join(why)
        return reg, inputs

    def outcome(self, i: int, h: int) -> float | None:
        j = i + h
        if j >= len(self.c) or self.c[i] <= 0:
            return None
        return self.c[j] / self.c[i] - 1.0

    def band(self, i: int, h: int) -> float | None:
        v = self.vol[i]
        return None if v is None else v * math.sqrt(h)


def macro_series(market_ts: list[float], rate_bars: list[list[float]] | None,
                 lookback: int = MACRO_LOOKBACK, thr: float = TIGHTEN_BP) -> list[str | None]:
    """Short-rate state for each market bar: tightening / easing / neutral (None = no data)."""
    if not rate_bars:
        return [None] * len(market_ts)
    rts = [float(b[0]) for b in rate_bars]
    rc = [float(b[4]) for b in rate_bars]
    state: list[str | None] = []
    for t in market_ts:
        k = bisect_right(rts, t + 20 * 3600_000.0) - 1  # same calendar day or earlier
        if k < lookback:
            state.append(None)
            continue
        ch = rc[k] - rc[k - lookback]
        state.append("tightening" if ch >= thr else ("easing" if ch <= -thr else "neutral"))
    return state


class _Counter:
    def __init__(self) -> None:
        self.tot = [0, 0, 0]  # n, up, in band
        self.reg: dict[str, list[int]] = {}
        self.regm: dict[tuple[str, str], list[int]] = {}

    def add(self, reg: str, mac: str | None, up: bool, inb: bool) -> None:
        for arr in (self.tot, self.reg.setdefault(reg, [0, 0, 0]),
                    self.regm.setdefault((reg, mac or "none"), [0, 0, 0])):
            arr[0] += 1
            arr[1] += int(up)
            arr[2] += int(inb)


def _shrink(k: int, n: int, prior: float, pseudo: float, div: float) -> float:
    if n <= 0:
        return prior
    ne = n / max(1.0, div)
    return (k / n * ne + prior * pseudo) / (ne + pseudo)


def _forecast(cnt: _Counter, reg: str, mac: str | None, h: int) -> dict[str, Any]:
    n, up, inb = cnt.tot
    b_up = (up + 1.0) / (n + 2.0)
    b_in = (inb + 1.0) / (n + 2.0)
    r = cnt.reg.get(reg, [0, 0, 0])
    p_up_r = _shrink(r[1], r[0], b_up, A_PRIOR, h)
    p_in_r = _shrink(r[2], r[0], b_in, A_PRIOR, h)
    if not USE_MACRO:
        mac = None
    m = cnt.regm.get((reg, mac or "none"), [0, 0, 0]) if mac else [0, 0, 0]
    p_up = _shrink(m[1], m[0], p_up_r, A_MACRO, h) if mac else p_up_r
    p_in = _shrink(m[2], m[0], p_in_r, A_MACRO, h) if mac else p_in_r
    if reg == "bull":
        p_cont, base = p_up, b_up
    elif reg == "bear":
        p_cont, base = 1.0 - p_up, 1.0 - b_up
    else:
        p_cont, base = p_in, b_in
    return {"p_up": p_up, "p_in_band": p_in, "p_continue": p_cont, "p_reversal": 1.0 - p_cont,
            "base_continue": base, "base_up": b_up, "base_in_band": b_in, "p_up_regime_only": p_up_r,
            "n_total": n, "n_regime": r[0], "n_regime_macro": m[0],
            "n_eff_regime": round(r[0] / max(1.0, h), 1), "regime_up_rate": (r[1] / r[0]) if r[0] else None}


def continue_event(reg: str, ret: float, band: float) -> bool:
    if reg == "bull":
        return ret > 0
    if reg == "bear":
        return ret < 0
    return abs(ret) <= band


def walk_forward(feat: Features, horizon: str, *, test_from_ms: float | None = None,
                 keep_records: bool = True) -> dict[str, Any]:
    """Expanding-window walk-forward. Returns OOS records and the live forecast at the last bar."""
    h = feat.h_bars(horizon)
    cnt = _Counter()
    recs: list[tuple] = []
    live: dict[str, Any] | None = None
    L = len(feat)
    for i in range(L):
        j = i - h
        if j >= 0:
            rj = feat.regime(horizon, j)
            bj = feat.band(j, h)
            if rj is not None and bj is not None:
                ret = feat.c[i] / feat.c[j] - 1.0
                cnt.add(rj[0], feat.macro[j], ret > 0, abs(ret) <= bj)
        ri = feat.regime(horizon, i)
        bi = feat.band(i, h)
        if ri is None or bi is None or cnt.tot[0] < MIN_TRAIN:
            continue
        if i == L - 1:
            live = _forecast(cnt, ri[0], feat.macro[i], h)
            live.update({"regime": ri[0], "inputs": ri[1], "band": bi, "macro_state": feat.macro[i],
                         "asof_ts": feat.ts[i], "h_bars": h})
            continue
        if not keep_records or (test_from_ms is not None and feat.ts[i] < test_from_ms):
            continue
        ret = feat.outcome(i, h)
        if ret is None:
            continue
        f = _forecast(cnt, ri[0], feat.macro[i], h)
        recs.append((feat.ts[i], ri[0], f["p_continue"], int(continue_event(ri[0], ret, bi)),
                     f["base_continue"], f["p_up"], int(ret > 0), f["base_up"]))
    return {"horizon": horizon, "h_bars": h, "records": recs, "live": live}


def brier(rows: list[tuple[float, int]]) -> float | None:
    return (sum((p - y) ** 2 for p, y in rows) / len(rows)) if rows else None


def calibration_metrics(records: list[tuple], h_bars: int = 1) -> dict[str, Any]:
    """Brier / skill vs base rate / hit rate / reliability for P(continue) and P(up)."""
    if not records:
        return {"n": 0}
    pc = [(r[2], r[3]) for r in records]
    pb = [(r[4], r[3]) for r in records]
    pu = [(r[5], r[6]) for r in records]
    pub = [(r[7], r[6]) for r in records]
    bc, bb, bu, bub = brier(pc), brier(pb), brier(pu), brier(pub)
    rel = []
    for lo, hi in zip(REL_BINS[:-1], REL_BINS[1:]):
        sel = [(p, y) for p, y in pc if lo <= p < hi]
        if sel:
            rel.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(sel),
                        "mean_p": round(sum(p for p, _ in sel) / len(sel), 4),
                        "observed": round(sum(y for _, y in sel) / len(sel), 4)})
    hit = sum(1 for p, y in pc if (p >= 0.5) == bool(y)) / len(pc)
    base_hit = sum(1 for p, y in pb if (p >= 0.5) == bool(y)) / len(pb)
    return {
        "n": len(records), "n_effective": round(len(records) / max(1, h_bars), 1),
        "from": records[0][0], "to": records[-1][0],
        "brier_continue": round(bc, 5), "brier_base_continue": round(bb, 5),
        "skill_continue": round(1.0 - bc / bb, 4) if bb else None,
        "brier_up": round(bu, 5), "brier_base_up": round(bub, 5),
        "skill_up": round(1.0 - bu / bub, 4) if bub else None,
        "hit_rate": round(hit, 4), "base_hit_rate": round(base_hit, 4),
        "mean_p_continue": round(sum(p for p, _ in pc) / len(pc), 4),
        "observed_continue": round(sum(y for _, y in pc) / len(pc), 4),
        "reliability": rel,
    }


# ------------------------------------------------------------------ bracket EV --


def bracket_outcome(feat: Features, i: int, h: int, side: str, tgt: float, stop: float) -> tuple[str, float] | None:
    """First passage on daily bars (target vs stop within h bars; both in one bar = stop)."""
    if i + h >= len(feat):
        return None
    e = feat.c[i]
    long = side == "long"
    t_px = e * (1 + tgt) if long else e * (1 - tgt)
    s_px = e * (1 - stop) if long else e * (1 + stop)
    for j in range(i + 1, i + h + 1):
        hi, lo = feat.h[j], feat.l[j]
        hit_s = lo <= s_px if long else hi >= s_px
        hit_t = hi >= t_px if long else lo <= t_px
        if hit_s:
            return "stop", -stop
        if hit_t:
            return "target", tgt
    r = feat.c[i + h] / e - 1.0
    return "open", (r if long else -r)


def bracket_ev(feat: Features, horizon: str, regime_now: str, side: str, tgt: float, stop: float,
               fee_pct: float = 0.0) -> dict[str, Any]:
    """P(target first), P(stop first), mean open P&L, EV after fees, from all past same-regime starts."""
    h = feat.h_bars(horizon)
    tot = {"target": 0, "stop": 0, "open": 0, "open_sum": 0.0, "n": 0}
    reg = {"target": 0, "stop": 0, "open": 0, "open_sum": 0.0, "n": 0}
    for i in range(len(feat) - h):
        r = feat.regime(horizon, i)
        if r is None:
            continue
        o = bracket_outcome(feat, i, h, side, tgt, stop)
        if o is None:
            continue
        for d, ok in ((tot, True), (reg, r[0] == regime_now)):
            if not ok:
                continue
            d["n"] += 1
            d[o[0]] += 1
            if o[0] == "open":
                d["open_sum"] += o[1]
    if tot["n"] < MIN_TRAIN:
        return {"available": False, "why": f"too little history ({tot['n']} windows)"}
    b_t = (tot["target"] + 1.0) / (tot["n"] + 2.0)
    b_s = (tot["stop"] + 1.0) / (tot["n"] + 2.0)
    p_t = _shrink(reg["target"], reg["n"], b_t, A_PRIOR, h)
    p_s = _shrink(reg["stop"], reg["n"], b_s, A_PRIOR, h)
    p_o = max(0.0, 1.0 - p_t - p_s)
    mean_open = (reg["open_sum"] / reg["open"]) if reg["open"] else (
        tot["open_sum"] / tot["open"] if tot["open"] else 0.0)
    ev = p_t * tgt - p_s * stop + p_o * mean_open - fee_pct
    be = (stop + fee_pct) / (tgt + stop) if (tgt + stop) > 0 else None
    return {"available": True, "side": side, "horizon": horizon, "target_pct": tgt, "stop_pct": stop,
            "fee_pct": fee_pct, "p_target": round(p_t, 4), "p_stop": round(p_s, 4), "p_open": round(p_o, 4),
            "mean_open_pct": round(mean_open, 5), "ev_pct": round(ev, 5),
            "break_even_p": None if be is None else round(be, 4), "n_regime": reg["n"], "n_total": tot["n"]}
