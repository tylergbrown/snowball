"""Daily Forward Guidance run: fetch history, classify, forecast, calibrate, playbook, persist, grade."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from snowball.forward import model as M
from snowball.forward.history import DailyHistory
from snowball.forward.store import ForwardStore, default_path

log = logging.getLogger("snowball.forward.runner")
CT = ZoneInfo("America/Chicago")
_RUN_LOCK = threading.Lock()

MAJORS: list[dict[str, Any]] = [
    {"market": "BTC", "key": "cb:BTC-USD", "cls": "crypto", "label": "BTC (spot) / BIP (BTC futures)",
     "spot": "BTC-USD", "futures": "BIP", "ref": "BTC-USD daily, Coinbase"},
    {"market": "ETH", "key": "cb:ETH-USD", "cls": "crypto", "label": "ETH (spot)", "spot": "ETH-USD",
     "ref": "ETH-USD daily, Coinbase"},
    {"market": "US5", "key": "yh:SPY", "cls": "equity", "label": "US5 (S&P 500)", "futures": "US5",
     "ref": "SPY daily, Yahoo"},
    {"market": "TEK", "key": "yh:QQQ", "cls": "equity", "label": "TEK (Nasdaq-100)", "futures": "TEK",
     "ref": "QQQ daily, Yahoo"},
    {"market": "NOL", "key": "yh:CL=F", "cls": "equity", "label": "NOL (WTI crude)", "futures": "NOL",
     "ref": "CL=F daily, Yahoo"},
]
MIN_BARS = 260
CAL_FROM_YEAR = 2019

# BLS schedule (bls.gov/schedule/2026, checked 2026-10-03), 8:30 AM ET = 7:30 AM CT unless noted.
# FOMC decisions come from the Fed Desk's FedWatch meeting list; this fallback is the Fed's 2026 calendar.
BLS_EVENTS = [
    ("2026-10-14", "7:30 AM CT", "CPI (September)"), ("2026-10-15", "7:30 AM CT", "PPI (September)"),
    ("2026-10-30", "7:30 AM CT", "Employment Cost Index (Q3)"), ("2026-11-03", "9:00 AM CT", "JOLTS (September)"),
    ("2026-11-06", "7:30 AM CT", "Jobs report (October)"), ("2026-11-10", "7:30 AM CT", "CPI (October)"),
    ("2026-11-13", "7:30 AM CT", "PPI (October)"), ("2026-12-01", "9:00 AM CT", "JOLTS (October)"),
    ("2026-12-04", "7:30 AM CT", "Jobs report (November)"), ("2026-12-10", "7:30 AM CT", "CPI (November)"),
    ("2026-12-15", "7:30 AM CT", "PPI (November)"),
]
FOMC_FALLBACK = ["2026-10-28", "2026-12-09"]

TICKER_MARKET = {
    "BTC": "BTC", "XBT": "BTC", "BITCOIN": "BTC", "IBIT": "BTC", "ETH": "ETH", "ETHEREUM": "ETH",
    "SPY": "US5", "SPX": "US5", "ES": "US5", "VOO": "US5", "IVV": "US5", "US500": "US5",
    "QQQ": "TEK", "NDX": "TEK", "NQ": "TEK", "US100": "TEK",
    "OIL": "NOL", "WTI": "NOL", "CL": "NOL", "CRUDE": "NOL", "USO": "NOL",
}


def ct_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(CT)


def _ro(path: Path) -> sqlite3.Connection | None:
    if not Path(path).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def held_spot_products(main_db: Path) -> list[str]:
    c = _ro(main_db)
    if c is None:
        return []
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT product FROM positions WHERE status='open' AND product LIKE '%-USD' ORDER BY product")]
    except sqlite3.Error:
        return []
    finally:
        c.close()


def market_list(main_db: Path) -> list[dict[str, Any]]:
    out = [dict(m) for m in MAJORS]
    have = {m.get("spot") for m in out}
    for p in held_spot_products(main_db):
        if p in have:
            continue
        base = p.split("-", 1)[0]
        out.append({"market": base, "key": f"cb:{p}", "cls": "crypto", "label": f"{base} (spot, held)",
                    "spot": p, "ref": f"{p} daily, Coinbase", "alt": True})
    return out


def fed_context(fed_db: Path) -> dict[str, Any]:
    c = _ro(fed_db)
    if c is None:
        return {"state": "unavailable", "why": "no Fed Desk DB"}
    try:
        r = c.execute("SELECT ts, payload_json FROM research_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return {"state": "unavailable", "why": str(exc)[:80]}
    finally:
        c.close()
    if not r:
        return {"state": "unavailable", "why": "no FedWatch snapshot yet"}
    try:
        d = json.loads(r["payload_json"] or "{}")
    except ValueError:
        return {"state": "unavailable", "why": "bad snapshot json"}
    meets = []
    for m in (d.get("meetings") or [])[:4]:
        meets.append({k: m.get(k) for k in ("date", "p_hold", "p_hike", "p_cut", "dominant")})
    return {"state": "ok", "at": r["ts"], "effr": d.get("effr"), "target": d.get("current_target"), "meetings": meets}


def research_stance(main_db: Path, now: datetime, days: int = 7) -> dict[str, dict[str, int]]:
    """Bull/bear ticker keyword hits from video transcripts published in the last `days` (context only)."""
    c = _ro(main_db)
    out: dict[str, dict[str, int]] = {}
    if c is None:
        return out
    since = (now - timedelta(days=days)).isoformat()
    try:
        rows = c.execute(
            "SELECT t.ideas_json FROM yolo_transcripts t JOIN yolo_videos v ON v.video_id=t.video_id "
            "WHERE t.status='ok' AND v.published_at >= ?", (since,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        c.close()
    for (raw,) in rows:
        try:
            ideas = json.loads(raw or "[]")
        except ValueError:
            continue
        seen: set[str] = set()
        for it in ideas if isinstance(ideas, list) else []:
            t = str(it.get("ticker") or "").upper().lstrip("$")
            mk = TICKER_MARKET.get(t, t)
            if not mk:
                continue
            d = out.setdefault(mk, {"bull": 0, "bear": 0, "videos": 0})
            d["bull"] += int(it.get("bull") or 0)
            d["bear"] += int(it.get("bear") or 0)
            if mk not in seen:
                d["videos"] += 1
                seen.add(mk)
    return out


def macro_events(fed: dict[str, Any], now: datetime, days: int = 60) -> list[dict[str, Any]]:
    today = ct_now(now).date()
    end = today + timedelta(days=days)
    ev: list[dict[str, Any]] = []
    fomc = [m.get("date") for m in (fed.get("meetings") or []) if m.get("date")] or FOMC_FALLBACK
    for m in fed.get("meetings") or [{"date": d} for d in FOMC_FALLBACK]:
        d = str(m.get("date") or "")
        if d and today <= datetime.fromisoformat(d).date() <= end:
            odds = ""
            if m.get("p_hold") is not None:
                odds = (f"FedWatch hold {float(m['p_hold']):.0%} / hike {float(m.get('p_hike') or 0):.0%} / "
                        f"cut {float(m.get('p_cut') or 0):.0%}")
            ev.append({"date": d, "time": "1:00 PM CT", "event": "FOMC rate decision", "detail": odds,
                       "source": "Fed Desk FedWatch" if m.get("p_hold") is not None else "Fed 2026 calendar"})
    del fomc
    for d, t, name in BLS_EVENTS:
        if today <= datetime.fromisoformat(d).date() <= end:
            ev.append({"date": d, "time": t, "event": name, "detail": "", "source": "BLS schedule"})
    ev.sort(key=lambda e: (e["date"], e["time"]))
    for e in ev:
        e["days_away"] = (datetime.fromisoformat(e["date"]).date() - today).days
    return ev


def _rate_context(irx: list[list[float]], vix: list[list[float]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if len(irx) > M.MACRO_LOOKBACK:
        ch = float(irx[-1][4]) - float(irx[-1 - M.MACRO_LOOKBACK][4])
        out.update({"tbill_13w": round(float(irx[-1][4]), 3), "tbill_63d_change": round(ch, 3),
                    "rate_state": "tightening" if ch >= M.TIGHTEN_BP else ("easing" if ch <= -M.TIGHTEN_BP else "neutral")})
    if vix:
        out["vix"] = round(float(vix[-1][4]), 2)
    return out


def futures_product(settings: Any, fam: str) -> str | None:
    for p in list(getattr(settings, "futures_product_list", []) or []):
        if str(p).upper().split("-", 1)[0] == fam:
            return p
    return None


def _mark_for(state: Any, product: str | None) -> float | None:
    if state is None or not product:
        return None
    for attr in ("futures_pairs", "stock_pairs"):
        snap = (getattr(state, attr, None) or {}).get(product)
        v = getattr(snap, "last", None) if snap is not None else None
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _last_fill_price(settings: Any, product: str) -> float | None:
    for attr in ("futures_sqlite_path", "crash_sqlite_path", "fed_sqlite_path"):
        p = getattr(settings, attr, None)
        c = _ro(Path(p)) if p else None
        if c is None:
            continue
        try:
            r = c.execute("SELECT price FROM fills WHERE product=? ORDER BY id DESC LIMIT 1", (product,)).fetchone()
            if r and r[0]:
                return float(r[0])
        except sqlite3.Error:
            pass
        finally:
            c.close()
    return None


def futures_bracket(settings: Any, product: str, mark: float | None, ref_close: float | None, fam: str) -> dict[str, Any]:
    """Real research_idea futures bracket: +TP vs the real stop (closer of 8% / $250 net), fees."""
    from snowball.futures.market import contract_size_for_product
    from snowball.futures.stops import round_trip_fee_per_contract, stop_distance_usd_per_contract

    cs = float(contract_size_for_product(product) or 1.0)
    src = "live mark"
    if mark is None:
        mark = _last_fill_price(settings, product)
        src = "last fill"
    if mark is None and fam in ("BIP", "NOL") and ref_close:
        mark, src = float(ref_close), "reference close"
    tgt = float(getattr(settings, "futures_momentum_take_profit_pct", 0.008) or 0.008)
    if mark is None:
        pct = float(getattr(settings, "futures_stop_pct", 0.08) or 0.08)
        return {"target_pct": tgt, "stop_pct": pct, "fee_pct": 0.0, "mark": None, "contract_size": cs,
                "stop_rule": f"FUTURES_STOP_PCT {pct:.0%} (no mark for the $ cap)", "mark_source": "none"}
    dist, leg = stop_distance_usd_per_contract(product, mark, settings)
    fee = round_trip_fee_per_contract(mark, product, settings)
    notional = mark * cs
    return {"target_pct": tgt, "stop_pct": round(dist / notional, 5), "fee_pct": round(fee / notional, 6),
            "mark": mark, "contract_size": cs, "notional_usd": round(notional, 2), "fee_usd": round(fee, 2),
            "stop_usd": round(dist, 2), "mark_source": src,
            "stop_rule": ("8% stop" if leg == "pct" else f"${float(getattr(settings, 'futures_stop_usd_per_contract', 250)):.0f}/contract net stop")
                         + f" (${dist:,.0f}) + ${float(getattr(settings, 'futures_liq_guard_usd', 50)):.0f} liquidation guard"}


def spot_gate_for(reg_1w: str | None, reg_1m: str | None, shrink: float) -> dict[str, Any]:
    b1, b2 = reg_1w == "bear", reg_1m == "bear"
    if b1 and b2:
        return {"action": "pause", "factor": 0.0, "why": "1w and 1m regimes both bear"}
    if b1 or b2:
        return {"action": "shrink", "factor": float(shrink), "why": f"{'1w' if b1 else '1m'} regime bear"}
    return {"action": "normal", "factor": 1.0, "why": "no bear regime"}


def build_playbook(m: dict[str, Any], fc: dict[str, dict[str, Any]], feat: M.Features, settings: Any,
                   state: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    r1w = (fc.get("1w") or {}).get("regime")
    r1m = (fc.get("1m") or {}).get("regime")
    fam = m.get("futures")
    if fam:
        prod = futures_product(settings, fam)
        short_fams = {str(p).upper().split("-", 1)[0] for p in (getattr(settings, "futures_short_product_list", []) or [])}
        if prod is None:
            out.append({"market": m["market"], "instrument": f"{fam} futures", "regime_1w": r1w, "regime_1m": r1m,
                        "action": "n/a", "detail": f"no {fam} product configured in FUTURES_PRODUCTS"})
        else:
            br = futures_bracket(settings, prod, _mark_for(state, prod), feat.c[-1] if len(feat) else None, fam)
            sides = ["long"] if r1w == "bull" else (["short"] if r1w == "bear" else ["long", "short"])
            evs = {}
            for side in sides:
                evs[side] = M.bracket_ev(feat, "1w", r1w or "range", side, br["target_pct"], br["stop_pct"], br["fee_pct"])
            best = max(evs, key=lambda s: float(evs[s].get("ev_pct") or -9)) if evs else None
            ev = evs.get(best) or {}
            if r1w == "bull":
                action = "LONG bias: futures longs (FT session/momentum/research lanes); no new shorts"
            elif r1w == "bear":
                action = "SHORT bias: futures shorts; no new longs"
            else:
                action = "RANGE: no directional futures bias; existing strategies unchanged"
            ev_usd = (float(ev["ev_pct"]) * float(br.get("notional_usd") or 0)) if ev.get("available") and br.get("notional_usd") else None
            qual = bool(ev.get("available") and float(ev.get("p_target") or 0) >= float(getattr(settings, "research_exec_min_prob", 0.70) or 0.70)
                        and float(ev.get("ev_pct") or 0) > 0)
            short_note = ""
            if best == "short" and fam not in short_fams:
                short_note = (f"research_idea shorts on {fam} are off (FUTURES_SHORT_PRODUCTS has "
                              f"{', '.join(sorted(short_fams)) or 'none'}); Crash Guard / Fed Desk short US5/TEK on their own triggers")
            out.append({"market": m["market"], "instrument": f"{fam} futures ({prod})", "regime_1w": r1w, "regime_1m": r1m,
                        "action": action, "side": best, "bracket": br, "ev": ev, "ev_by_side": evs,
                        "ev_usd_per_contract": None if ev_usd is None else round(ev_usd, 2),
                        "meets_auto_rule": qual, "note": short_note,
                        "auto": ("would meet P>=70% and EV>0 (research_idea executes only from source-backed proposals)"
                                 if qual else "below the auto rule (P>=70% and EV>0 after fees): not traded")})
    if m.get("spot"):
        shrink = float(getattr(settings, "forward_spot_bear_shrink", 0.5) or 0.5)
        g = spot_gate_for(r1w, r1m, shrink)
        try:
            floor = float(settings.effective_min_take_profit_pct_for("research_idea"))
        except Exception:  # noqa: BLE001
            floor = 0.07
        st = M.bracket_ev(feat, "1m", r1m or "range", "long", floor, 0.99, 0.0) if len(feat) >= MIN_BARS else {"available": False}
        if g["action"] == "pause":
            action = "BEAR: pause new spot buys (never-sell-red holds existing lots)"
        elif g["action"] == "shrink":
            action = f"PARTIAL BEAR: new spot buys at {shrink:.0%} size"
        else:
            action = "Spot buys normal (technical strategies; never-sell-red)"
        out.append({"market": m["market"], "instrument": f"{m['spot']} spot", "regime_1w": r1w, "regime_1m": r1m,
                    "action": action, "gate": g, "green_floor_pct": floor,
                    "p_green_floor_30d": st.get("p_target") if st.get("available") else None,
                    "note": "spot can't be shorted; held until green"})
    return out


def grade_due(store: ForwardStore, hist: DailyHistory, now_ms: float | None = None) -> int:
    n = 0
    bars_by: dict[str, list[list[float]]] = {}
    for f in store.ungraded():
        sym = f["symbol"]
        if sym not in bars_by:
            bars_by[sym] = hist.get(sym, now_ms)
        bars = bars_by[sym]
        if not bars:
            continue
        ts = [float(b[0]) for b in bars]
        k = bisect_right(ts, float(f["asof_ts"]) + 1.0) - 1
        if k < 0 or abs(ts[k] - float(f["asof_ts"])) > 36 * 3600_000.0:
            continue
        j = k + int(f["h_bars"])
        if j >= len(bars):
            continue
        c0, c1 = float(bars[k][4]), float(bars[j][4])
        ret = c1 / c0 - 1.0
        band = float(f["band"] or 0.0)
        y = int(M.continue_event(f["regime"], ret, band))
        up = int(ret > 0)
        pc, pb, pu = float(f["p_continue"]), float(f["base_continue"]), float(f["p_up"])
        store.grade(int(f["id"]), {"outcome_ts": int(ts[j]), "outcome_close": c1, "outcome_ret": ret,
                                   "outcome_continue": y, "outcome_up": up, "brier_continue": (pc - y) ** 2,
                                   "brier_base": (pb - y) ** 2, "brier_up": (pu - up) ** 2,
                                   "hit": int((pc >= 0.5) == bool(y))})
        n += 1
    return n


def _ms(y: int) -> float:
    return datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() * 1000.0


def _iso_date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date().isoformat()


def run_forward_guidance(settings: Any, *, state: Any = None, now: datetime | None = None,
                         history: DailyHistory | None = None, store: ForwardStore | None = None,
                         calibrate: bool = True) -> dict[str, Any]:
    with _RUN_LOCK:
        return _run(settings, state=state, now=now, history=history, store=store, calibrate=calibrate)


def _run(settings: Any, *, state: Any, now: datetime | None, history: DailyHistory | None,
         store: ForwardStore | None, calibrate: bool) -> dict[str, Any]:
    t0 = time.monotonic()
    now = now or datetime.now(timezone.utc)
    now_ms = now.timestamp() * 1000.0
    run_date = ct_now(now).date().isoformat()
    data_dir = Path(getattr(settings, "sqlite_path", "./data/snowball.db")).parent
    hist = history or DailyHistory(data_dir / "forward_cache")
    own_store = store is None
    store = store or ForwardStore(default_path(settings))
    try:
        main_db = Path(getattr(settings, "sqlite_path", data_dir / "snowball.db"))
        markets = market_list(main_db)
        irx = hist.get("yh:^IRX", now_ms)
        vix = hist.get("yh:^VIX", now_ms)
        stance = research_stance(main_db, now)
        errors: dict[str, str] = {}
        out_markets: list[dict[str, Any]] = []
        playbook: list[dict[str, Any]] = []
        records_pool: dict[tuple[str, str], list[tuple]] = {}
        cal_rows: dict[str, dict[str, Any]] = {}
        created = datetime.now(timezone.utc).isoformat()
        inserted = 0
        for m in markets:
            bars = hist.get(m["key"], now_ms)
            entry: dict[str, Any] = {k: m.get(k) for k in ("market", "label", "key", "cls", "spot", "futures", "ref", "alt")}
            entry["research_stance_7d"] = stance.get(m["market"])
            if len(bars) < MIN_BARS:
                entry["error"] = f"too little daily history ({len(bars)} bars < {MIN_BARS})"
                errors[m["market"]] = entry["error"] + (f"; {hist.errors.get(m['key'])}" if hist.errors.get(m["key"]) else "")
                out_markets.append(entry)
                continue
            feat = M.Features(bars, m["cls"])
            feat.macro = M.macro_series(feat.ts, irx)
            entry.update({"history_from": _iso_date(feat.ts[0]), "history_to": _iso_date(feat.ts[-1]),
                          "n_bars": len(feat), "last_close": feat.c[-1]})
            fc: dict[str, dict[str, Any]] = {}
            for hz in M.HORIZONS:
                wf = M.walk_forward(feat, hz, test_from_ms=_ms(CAL_FROM_YEAR) if calibrate else None, keep_records=calibrate)
                lv = wf["live"]
                if lv is None:
                    continue
                fc[hz] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in lv.items() if k != "inputs"}
                fc[hz]["inputs"] = lv["inputs"]
                if calibrate:
                    recs = wf["records"]
                    val = [r for r in recs if _ms(M.VALIDATION[0]) <= r[0] < _ms(M.VALIDATION[1])]
                    tst = [r for r in recs if r[0] >= _ms(M.TEST_FROM_YEAR)]
                    cm_t = M.calibration_metrics(tst, wf["h_bars"])
                    cm_v = M.calibration_metrics(val, wf["h_bars"])
                    store.save_calibration(m["market"], hz, "test", cm_t)
                    store.save_calibration(m["market"], hz, "validation", cm_v)
                    cal_rows.setdefault(m["market"], {})[hz] = {k: cm_t.get(k) for k in (
                        "n", "brier_continue", "brier_base_continue", "skill_continue", "skill_up", "hit_rate", "base_hit_rate")}
                    pool = "alts" if m.get("alt") else "majors"
                    records_pool.setdefault((pool, hz), []).extend(tst)
                inputs = dict(lv["inputs"])
                inputs.update({"macro_state": lv.get("macro_state"), "p_up_regime_only": round(lv["p_up_regime_only"], 4),
                               "n_eff_regime": lv["n_eff_regime"], "regime_up_rate": lv["regime_up_rate"],
                               "research_stance_7d": stance.get(m["market"]), "a_prior": M.A_PRIOR, "use_macro": M.USE_MACRO})
                ok = store.insert_forecast({
                    "run_date": run_date, "created_at": created, "market": m["market"], "symbol": m["key"],
                    "asset_class": m["cls"], "horizon": hz, "h_bars": lv["h_bars"], "asof_ts": int(lv["asof_ts"]),
                    "asof_date": _iso_date(lv["asof_ts"]), "asof_close": feat.c[-1], "regime": lv["regime"],
                    "p_continue": lv["p_continue"], "p_reversal": lv["p_reversal"], "p_up": lv["p_up"],
                    "p_in_band": lv["p_in_band"], "base_continue": lv["base_continue"], "base_up": lv["base_up"],
                    "band": lv["band"], "n_regime": lv["n_regime"], "n_total": lv["n_total"],
                    "inputs_json": json.dumps(inputs, default=str)})
                inserted += int(ok)
            entry["forecasts"] = fc
            out_markets.append(entry)
            try:
                playbook.extend(build_playbook(m, fc, feat, settings, state))
            except Exception as exc:  # noqa: BLE001
                log.exception("forward playbook failed", extra={"data": {"market": m["market"]}})
                errors[f"playbook:{m['market']}"] = str(exc)[:120]
        # pooled calibration
        pooled: dict[str, dict[str, Any]] = {}
        for (pool, hz), recs in records_pool.items():
            recs.sort(key=lambda r: r[0])
            cm = M.calibration_metrics(recs, M.H_BARS["equity"][hz])
            store.save_calibration(f"ALL_{pool.upper()}", hz, "test", cm)
            pooled.setdefault(pool, {})[hz] = cm
        if not calibrate:
            cal_all = store.calibration("test")
            cal_rows = {k: {h: {x: v.get(x) for x in ("n", "brier_continue", "brier_base_continue", "skill_continue",
                                                      "skill_up", "hit_rate", "base_hit_rate")} for h, v in d.items()}
                        for k, d in cal_all.items() if not k.startswith("ALL_")}
            pooled = {k[4:].lower(): d for k, d in cal_all.items() if k.startswith("ALL_")}
        # spot gate (BTC fallback for products without enough history)
        by_mk = {e["market"]: e for e in out_markets}
        btc = (by_mk.get("BTC") or {}).get("forecasts") or {}
        shrink = float(getattr(settings, "forward_spot_bear_shrink", 0.5) or 0.5)
        spot_gate: dict[str, Any] = {}
        products = set(getattr(settings, "product_list", []) or []) | {e["spot"] for e in out_markets if e.get("spot")}
        for prod in sorted(products):
            base = prod.split("-", 1)[0]
            fc = (by_mk.get(base) or {}).get("forecasts") or {}
            src = base
            if not fc.get("1w") or not fc.get("1m"):
                fc, src = btc, "BTC (fallback: too little own history)"
            if not fc.get("1w") or not fc.get("1m"):
                continue
            g = spot_gate_for(fc["1w"]["regime"], fc["1m"]["regime"], shrink)
            g.update({"source": src, "regime_1w": fc["1w"]["regime"], "regime_1m": fc["1m"]["regime"]})
            spot_gate[prod] = g
        # research regime input (only where OOS 1w P(up) skill is positive enough)
        min_skill = float(getattr(settings, "forward_research_min_skill", 0.005) or 0.005)
        cap = float(getattr(settings, "forward_research_max_logit", 0.25) or 0.25)
        research_input: dict[str, Any] = {}
        for e in out_markets:
            f1 = (e.get("forecasts") or {}).get("1w")
            if not f1:
                continue
            sk = ((cal_rows.get(e["market"]) or {}).get("1w") or {}).get("skill_up")
            tilt = max(-cap, min(cap, M.logit(f1["p_up"]) - M.logit(f1["base_up"])))
            research_input[e["market"]] = {"regime_1w": f1["regime"], "p_up_1w": f1["p_up"], "base_up_1w": f1["base_up"],
                                           "skill_up_1w": sk, "long_logit": round(tilt, 4),
                                           "enabled": sk is not None and float(sk) >= min_skill}
        # what changed since the previous forecast day
        prev = store.previous_run_date(run_date)
        changes: list[dict[str, Any]] = []
        if prev:
            old = {(r["market"], r["horizon"]): r for r in store.forecasts_for(prev)}
            for r in store.forecasts_for(run_date):
                o = old.get((r["market"], r["horizon"]))
                if o is None:
                    changes.append({"market": r["market"], "horizon": r["horizon"], "change": "new market"})
                    continue
                if o["regime"] != r["regime"]:
                    changes.append({"market": r["market"], "horizon": r["horizon"],
                                    "change": f"regime {o['regime']} -> {r['regime']}"})
                elif abs(float(r["p_continue"]) - float(o["p_continue"])) >= 0.05:
                    changes.append({"market": r["market"], "horizon": r["horizon"],
                                    "change": f"P(continue) {float(o['p_continue']):.0%} -> {float(r['p_continue']):.0%}"})
        graded = grade_due(store, hist, now_ms)
        fed = fed_context(Path(getattr(settings, "fed_sqlite_path", data_dir / "snowball_fed.db")))
        summary = {
            "run_date": run_date, "generated_at": datetime.now(timezone.utc).isoformat(), "previous_run_date": prev,
            "markets": out_markets, "playbook": playbook, "spot_gate": spot_gate, "research_input": research_input,
            "macro": {"fed": fed, "rates": _rate_context(irx, vix), "events": macro_events(fed, now)},
            "changes": changes, "calibration": {"per_market": cal_rows, "pooled": pooled,
                                                "test_from": f"{M.TEST_FROM_YEAR}-01-01",
                                                "validation": f"{M.VALIDATION[0]}-{M.VALIDATION[1] - 1}"},
            "scorecard": store.scorecard(), "errors": errors, "inserted": inserted, "graded_now": graded,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "method": {"a_prior": M.A_PRIOR, "use_macro": M.USE_MACRO, "horizon_bars": M.H_BARS,
                       "regime_params": M.REGIME_PARAMS},
        }
        store.save_run(run_date, summary)
        log.info("forward guidance run", extra={"data": {"run_date": run_date, "markets": len(out_markets),
                                                           "inserted": inserted, "graded": graded,
                                                           "errors": len(errors), "elapsed_s": summary["elapsed_s"]}})
        return summary
    finally:
        if own_store:
            store.close()


def load_latest(settings: Any) -> dict[str, Any] | None:
    p = default_path(settings)
    if not p.exists():
        return None
    st = ForwardStore(p)
    try:
        return st.run()
    finally:
        st.close()


def fresh_guidance(state: Any, max_age_hours: float | None = None) -> dict[str, Any] | None:
    fg = getattr(state, "forward_guidance", None) or {}
    if not fg or not fg.get("generated_at"):
        return None
    if max_age_hours is None:
        max_age_hours = float(getattr(getattr(state, "settings", None), "forward_max_age_hours", 36.0) or 36.0)
    try:
        at = datetime.fromisoformat(str(fg["generated_at"]))
    except ValueError:
        return None
    if datetime.now(timezone.utc) - at > timedelta(hours=max_age_hours):
        return None
    return fg


class ForwardGuidanceRunner:
    """Background thread: daily run at/after FORWARD_RUN_AFTER_CT (and at startup if today is missing)."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.settings = state.settings
        self.last_error: str | None = None
        self.history = DailyHistory(Path(getattr(self.settings, "sqlite_path", "./data/snowball.db")).parent / "forward_cache")

    def _due(self, now: datetime) -> bool:
        ct = ct_now(now)
        hh, mm = (int(x) for x in str(getattr(self.settings, "forward_run_after_ct", "07:20")).split(":"))
        if (ct.hour, ct.minute) < (hh, mm):
            return False
        fg = getattr(self.state, "forward_guidance", None) or {}
        return fg.get("run_date") != ct.date().isoformat()

    def tick(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self._due(now):
            return False
        try:
            self.state.forward_guidance = run_forward_guidance(self.settings, state=self.state, now=now, history=self.history)
            self.last_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:200]
            log.exception("forward guidance run failed")
            return False

    def run_forever(self) -> None:
        try:
            latest = load_latest(self.settings)
            if latest:
                self.state.forward_guidance = latest
        except Exception:  # noqa: BLE001
            log.exception("forward guidance load failed")
        for _ in range(60):  # let the trading loop start first
            if not getattr(self.state, "running", True):
                return
            time.sleep(1.0)
        every = max(60.0, float(getattr(self.settings, "forward_check_seconds", 600.0) or 600.0))
        while getattr(self.state, "running", True):
            self.tick()
            slept = 0.0
            while slept < every and getattr(self.state, "running", True):
                time.sleep(1.0)
                slept += 1.0
