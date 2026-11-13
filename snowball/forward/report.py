"""Forward Guidance presentation helpers: snapshot payload, regime table rows, Obsidian notes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowball.forward.model import HORIZONS

TRADING_MOC = "[[Trading MOC]]"
_SECRET_RX = re.compile(r"(api[_-]?key|secret|passphrase|token|password|BEGIN [A-Z ]*PRIVATE KEY|@icloud\.com|@gmail\.com|"
                        r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b|\bbc1[a-z0-9]{20,}\b)", re.I)


def pct(v: Any, nd: int = 0) -> str:
    try:
        return f"{float(v) * 100:.{nd}f}%"
    except (TypeError, ValueError):
        return "—"


def spct(v: Any, nd: int = 2) -> str:
    try:
        return f"{float(v) * 100:+.{nd}f}%"
    except (TypeError, ValueError):
        return "—"


def regime_rows(summary: dict[str, Any], *, alts: bool | None = None) -> list[dict[str, Any]]:
    rows = []
    for e in summary.get("markets") or []:
        if alts is not None and bool(e.get("alt")) != alts:
            continue
        fc = e.get("forecasts") or {}
        row = {"market": e.get("market"), "label": e.get("label"), "error": e.get("error"),
               "stance": e.get("research_stance_7d")}
        for h in HORIZONS:
            f = fc.get(h)
            row[h] = None if not f else {"regime": f.get("regime"), "p_continue": f.get("p_continue"),
                                         "p_reversal": f.get("p_reversal"), "p_up": f.get("p_up"),
                                         "base_continue": f.get("base_continue")}
        rows.append(row)
    return rows


def cell(f: dict[str, Any] | None) -> str:
    if not f:
        return "—"
    return f"{f['regime']} · cont {pct(f['p_continue'])} / rev {pct(f['p_reversal'])}"


def compact(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Snapshot payload (no secrets; JSON-safe)."""
    if not summary:
        return {"state": "no_forecast_yet"}
    pb = []
    for p in summary.get("playbook") or []:
        ev = p.get("ev") or {}
        br = p.get("bracket") or {}
        pb.append({"market": p.get("market"), "instrument": p.get("instrument"), "action": p.get("action"),
                   "side": p.get("side"), "regime_1w": p.get("regime_1w"), "regime_1m": p.get("regime_1m"),
                   "p_target": ev.get("p_target"), "p_stop": ev.get("p_stop"), "ev_pct": ev.get("ev_pct"),
                   "break_even_p": ev.get("break_even_p"), "ev_usd_per_contract": p.get("ev_usd_per_contract"),
                   "target_pct": br.get("target_pct"), "stop_pct": br.get("stop_pct"), "stop_rule": br.get("stop_rule"),
                   "meets_auto_rule": p.get("meets_auto_rule"), "gate": (p.get("gate") or {}).get("action"),
                   "p_green_floor_30d": p.get("p_green_floor_30d"), "note": p.get("note")})
    cal = summary.get("calibration") or {}
    pooled = {pool: {h: {k: v.get(k) for k in ("n", "n_effective", "brier_continue", "brier_base_continue",
                                                 "skill_continue", "skill_up", "hit_rate", "base_hit_rate",
                                                 "mean_p_continue", "observed_continue", "reliability")}
                     for h, v in d.items()} for pool, d in (cal.get("pooled") or {}).items()}
    return {
        "state": "ok", "run_date": summary.get("run_date"), "generated_at": summary.get("generated_at"),
        "regimes": regime_rows(summary), "playbook": pb,
        "spot_gate": {k: {"action": v.get("action"), "why": v.get("why"), "source": v.get("source")}
                      for k, v in (summary.get("spot_gate") or {}).items() if v.get("action") != "normal"},
        "gate_stats": summary.get("_gate_stats") or {},
        "research_input": summary.get("research_input"), "macro": summary.get("macro"),
        "changes": summary.get("changes"), "previous_run_date": summary.get("previous_run_date"),
        "calibration": {"pooled": pooled, "test_from": cal.get("test_from"), "validation": cal.get("validation"),
                        "per_market": cal.get("per_market")},
        "scorecard": summary.get("scorecard"), "errors": summary.get("errors"), "method": summary.get("method"),
    }


# ------------------------------------------------------------------ Obsidian --


def _front(date: str, status: str = "unreviewed") -> str:
    return ("---\nagent: Snowball\narea: trading\ntype: report\n"
            f"date: {date}\nstatus: {status}\n"
            "tags: [agent-written, unreviewed, area/trading, type/report, forward-guidance]\n---\n")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def esc(x: Any) -> str:
        return str(x if x is not None else "—").replace("|", "/").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def daily_note(summary: dict[str, Any]) -> str:
    d = summary.get("run_date") or datetime.now(timezone.utc).date().isoformat()
    y, m = d[:4], d[5:7]
    L = [_front(d), f"# {d} Forward Guidance", "", f"Up: {TRADING_MOC} · [[Forward Guidance Scorecard]]", ""]
    L.append(f"Generated {summary.get('generated_at', '')[:16].replace('T', ' ')} UTC by Snowball from multi-year daily "
             "history (Coinbase / Yahoo). Probabilities are counted base rates, not predictions with an edge; see "
             "calibration below.")
    L += ["", "## Regimes and continuation probabilities", ""]
    rows = []
    for r in regime_rows(summary):
        if r.get("error"):
            rows.append([r["market"], r["error"], "", ""])
        else:
            rows.append([r["market"]] + [cell(r.get(h)) for h in HORIZONS])
    L.append(_md_table(["Market", "1 day", "1 week", "1 month"], rows))
    L += ["", "cont = P(trend continues: bull closes higher / bear closes lower / range stays inside its typical "
              "move); rev = 1 − cont.", "", "## Playbook", ""]
    prow = []
    for p in summary.get("playbook") or []:
        ev = p.get("ev") or {}
        if ev.get("available"):
            br = p.get("bracket") or {}
            stats = (f"{p.get('side')}: P(+{pct(br.get('target_pct'), 1)} before -{pct(br.get('stop_pct'), 1)}) "
                     f"{pct(ev.get('p_target'))}, break-even {pct(ev.get('break_even_p'))}, EV {spct(ev.get('ev_pct'), 3)}"
                     + (f" (${p['ev_usd_per_contract']:+.2f}/contract)" if p.get("ev_usd_per_contract") is not None else ""))
        elif p.get("p_green_floor_30d") is not None:
            stats = f"P(+{pct(p.get('green_floor_pct'))} green floor within 30d) {pct(p.get('p_green_floor_30d'))}"
        else:
            stats = "—"
        prow.append([p.get("market"), p.get("instrument"), f"{p.get('regime_1w')}/{p.get('regime_1m')}",
                     p.get("action"), stats, "yes" if p.get("meets_auto_rule") else "no"])
    L.append(_md_table(["Market", "Instrument", "Regime 1w/1m", "Playbook", "Odds and EV after fees", "Meets P≥70% & EV>0"], prow))
    L += ["", "## Macro", ""]
    mac = summary.get("macro") or {}
    rt = mac.get("rates") or {}
    fed = mac.get("fed") or {}
    L.append(f"- 13-week T-bill {rt.get('tbill_13w', '—')}% ({rt.get('tbill_63d_change', 0):+.2f} pts in 63 trading days: "
             f"{rt.get('rate_state', '—')}) · VIX {rt.get('vix', '—')}")
    if fed.get("state") == "ok":
        L.append(f"- Fed funds target {fed.get('target')} (EFFR {fed.get('effr')}); FedWatch: " + "; ".join(
            f"{m.get('date')} hold {pct(m.get('p_hold'))} / hike {pct(m.get('p_hike'))} / cut {pct(m.get('p_cut'))}"
            for m in (fed.get("meetings") or [])[:3]))
    for e in mac.get("events") or []:
        L.append(f"- {e['date']} {e['time']} — {e['event']}" + (f" ({e['detail']})" if e.get("detail") else "")
                 + f" · in {e.get('days_away')}d")
    L += ["", "## What changed since the previous forecast", ""]
    ch = summary.get("changes") or []
    if not summary.get("previous_run_date"):
        L.append("- First forecast; nothing to compare yet.")
    elif not ch:
        L.append(f"- No regime flips or ≥5-pt probability moves since {summary['previous_run_date']}.")
    for c in ch[:40]:
        L.append(f"- {c['market']} {c['horizon']}: {c['change']}")
    L += ["", "## Out-of-sample calibration", ""]
    cal = summary.get("calibration") or {}
    crow = []
    for pool, dd in (cal.get("pooled") or {}).items():
        for h in HORIZONS:
            v = dd.get(h) or {}
            if v.get("n"):
                crow.append([pool, h, v.get("n"), v.get("brier_continue"), v.get("brier_base_continue"),
                             spct(v.get("skill_continue"), 2), pct(v.get("hit_rate"), 1), pct(v.get("base_hit_rate"), 1)])
    L.append(_md_table(["Pool", "Horizon", "Forecasts", "Brier", "Brier base rate", "Skill vs base", "Hit rate", "Base hit rate"], crow))
    L += ["", f"Walk-forward from {cal.get('test_from')} (test; settings chosen on {cal.get('validation')}). "
              "Skill ≈ 0 means the regime adds no measurable edge over the long-run base rate."]
    sc = summary.get("scorecard") or {}
    L += ["", "## Live scorecard", "", f"Graded {int((sc.get('all') or {}).get('n') or 0)} · pending {sc.get('pending', 0)}. "
          "Full history: [[Forward Guidance Scorecard]].", ""]
    return "\n".join(L).rstrip() + "\n"


def scorecard_note(summary: dict[str, Any], store_scorecard: dict[str, Any] | None = None) -> str:
    d = summary.get("run_date") or datetime.now(timezone.utc).date().isoformat()
    sc = store_scorecard or summary.get("scorecard") or {}
    L = [_front(d, "active").replace("status: active", f"status: active\nupdated: {d}"), "# Forward Guidance Scorecard", "",
         f"Up: {TRADING_MOC}", "",
         "Every daily forecast is stored in snowball_forward.db and graded automatically when its horizon ends. "
         "Brier: lower is better (0.25 = coin flip). Skill = 1 − Brier / Brier of the base rate.", "",
         f"As of {d}: {sc.get('total', 0)} forecasts stored, {int((sc.get('all') or {}).get('n') or 0)} graded, "
         f"{sc.get('pending', 0)} pending.", "", "## By horizon", ""]
    rows = []
    for h in HORIZONS:
        v = (sc.get("by_horizon") or {}).get(h)
        if v:
            rows.append([h, v.get("n"), pct(v.get("hit"), 1), f"{float(v['b']):.4f}" if v.get("b") is not None else "—",
                         f"{float(v['bb']):.4f}" if v.get("bb") is not None else "—", spct(v.get("skill"), 1),
                         f"{v.get('d0')} → {v.get('d1')}"])
        else:
            rows.append([h, 0, "—", "—", "—", "—", "not graded yet"])
    L.append(_md_table(["Horizon", "Graded", "Hit rate", "Brier", "Brier base", "Skill", "Forecast dates"], rows))
    rec = sc.get("recent") or []
    if rec:
        L += ["", "## Latest graded", ""]
        L.append(_md_table(["Forecast date", "Market", "Horizon", "Regime", "P(continue)", "Outcome", "Continued", "Hit"],
                           [[r["run_date"], r["market"], r["horizon"], r["regime"], pct(r["p_continue"]),
                             spct(r["outcome_ret"]), "yes" if r["outcome_continue"] else "no", "✓" if r["hit"] else "✗"]
                            for r in rec]))
    L += ["", "## Backtest calibration (out of sample)", ""]
    cal = summary.get("calibration") or {}
    crow = []
    for pool, dd in (cal.get("pooled") or {}).items():
        for h in HORIZONS:
            v = dd.get(h) or {}
            if v.get("n"):
                crow.append([pool, h, v.get("n"), spct(v.get("skill_continue"), 2), pct(v.get("hit_rate"), 1),
                             pct(v.get("mean_p_continue"), 1), pct(v.get("observed_continue"), 1)])
    L.append(_md_table(["Pool", "Horizon", "Forecasts", "Skill vs base", "Hit rate", "Mean P", "Observed"], crow))
    return "\n".join(L).rstrip() + "\n"


def note_files(summary: dict[str, Any], store_scorecard: dict[str, Any] | None = None) -> dict[str, str]:
    d = summary.get("run_date")
    files = {f"Forward Guidance/{d[:4]}/{d[5:7]}/{d} Forward Guidance.md": daily_note(summary),
             "Forward Guidance/Forward Guidance Scorecard.md": scorecard_note(summary, store_scorecard)}
    for k, v in files.items():
        if _SECRET_RX.search(v):
            raise ValueError(f"refusing to emit {k}: secret-like or email pattern found")
    return files


def emit_notes(summary: dict[str, Any], out_dir: Path, store_scorecard: dict[str, Any] | None = None) -> list[Path]:
    out = []
    for rel, txt in note_files(summary, store_scorecard).items():
        p = Path(out_dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(txt, encoding="utf-8")
        tmp.replace(p)
        out.append(p)
    return out
