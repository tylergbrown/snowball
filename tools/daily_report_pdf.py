#!/usr/bin/env python3
"""Snowball daily trade PDF — professional layout + top-10 leaderboard + Future Trader."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
def _resolve_root() -> Path:
    import os
    env = (os.environ.get("SNOWBALL_ROOT") or "").strip()
    if env:
        return Path(env)
    app = Path("/app")
    if (app / "data").is_dir():
        return app
    return Path("/home/tyler/snowball")

ROOT = _resolve_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "/app" not in sys.path and Path("/app/snowball").is_dir():
    sys.path.insert(0, "/app")

from snowball.reports.closed_pnl import aggregate_closed_pnl, default_lane_dbs
from snowball.reports.treasury_metrics import treasury_snapshot_for_pdf
from snowball.reports import research_report as rr  # noqa: E402
from snowball.treasury.dca import LABEL as DIVIDEND_LABEL, RULE_TEXT as DIVIDEND_RULE, dividend_status, kind_label  # noqa: E402

DB = ROOT / "data" / "snowball.db"
FUT_DB = ROOT / "data" / "snowball_futures.db"
REPORTS = ROOT / "reports"

# Preferred index order for Future Trader section
FT_INDEX_ORDER = ("SPY", "QQQ")


def fetch_snap() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/snapshot", timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def fetch_futures(snap: dict | None = None) -> dict:
    """Prefer dedicated /api/futures; fall back to snapshot key."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/futures", timeout=120) as r:
            data = json.loads(r.read().decode())
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    if isinstance(snap, dict):
        fut = snap.get("futures")
        if isinstance(fut, dict):
            return fut
    return {}


def _money(v: object, *, signed: bool = False) -> str:
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        x = 0.0
    if signed:
        return ("+" if x >= 0 else "") + f"${x:,.2f}"
    return f"${x:,.2f}"


def _index_label(product: str | None) -> str:
    raw = (product or "").strip().upper()
    if not raw:
        return "—"
    # SPY-PERP-INTX / QQQ-PERP-INTX → SPY / QQQ
    return raw.split("-", 1)[0]


def top_trades(con: sqlite3.Connection, n: int = 10) -> list[sqlite3.Row]:
    return list(
        con.execute(
            """
            select id, product, strategy, round(realized_pnl, 4) pnl,
                   round(entry_price, 6) entry_price, round(exit_price, 6) exit_price,
                   opened_at, closed_at
            from positions
            where status='closed' and realized_pnl is not null
              and coalesce(strategy, '') != 'research_idea'
            order by realized_pnl desc
            limit ?
            """,
            (n,),
        )
    )


def praise_line(rows: list) -> str:
    if not rows:
        return "Leaderboard empty — waiting for the first green winner. Keep hunting."
    # credit strategies by sum of top-10 pnl
    credit: dict[str, float] = {}
    for r in rows:
        sid = r["strategy"] or "unknown"
        credit[sid] = credit.get(sid, 0.0) + float(r["pnl"] or 0)
    champ = max(credit.items(), key=lambda kv: kv[1])[0]
    names = {
        "sma_5m": "SMA 5m bot",
        "sma_15m": "SMA 15m swing bot",
    }
    champ_name = names.get(champ, champ)
    return (
        f"Top credit goes to <b>{champ_name}</b> — absolute heater. "
        f"Great work. Keep kicking ass."
    )


def make_table(headers, rows, widths):
    th = ParagraphStyle("th", fontSize=8, textColor=colors.white, leading=11, fontName="Helvetica-Bold")
    td = ParagraphStyle("td", fontSize=8, textColor=colors.HexColor("#0f172a"), leading=11)
    data = [[Paragraph(h, th) for h in headers]]
    for r in rows:
        data.append([Paragraph(str(c), td) for c in r])
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ]
        )
    )
    return t


def _parse_iso(ts: object) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _in_ct_day(ts: object, day: date) -> bool:
    dt = _parse_iso(ts)
    if dt is None:
        return False
    return dt.astimezone(CT).date() == day


def _ft_session_status(pos: dict | None, pair: dict | None, now_ct: datetime) -> str:
    """Prefer API session fields; else infer flat / open_today / holding_overnight."""
    for src in (pos, pair):
        if not isinstance(src, dict):
            continue
        for key in ("session_status", "lot_status", "status_text", "session_state"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if not pos:
        return "flat"
    opened = _parse_iso(pos.get("opened_at"))
    if opened is None:
        return "open"
    today_et = datetime.now(ET).date()
    opened_et = opened.astimezone(ET).date()
    if opened_et < today_et:
        return "holding_overnight"
    return "open_today"


def _ft_liquidity_note(fut: dict) -> str | None:
    """Build liquidity-cap note when API exposes budget fields; omit quietly otherwise."""
    risk = fut.get("risk") if isinstance(fut.get("risk"), dict) else {}
    candidates = [
        fut.get("liquidity_note"),
        risk.get("liquidity_note"),
        fut.get("budget_note"),
        risk.get("budget_note"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()

    pct = None
    for src in (fut, risk):
        if not isinstance(src, dict):
            continue
        for key in (
            "account_budget_pct",
            "futures_account_budget_pct",
            "liquidity_cap_pct",
            "budget_pct",
        ):
            if src.get(key) is not None:
                try:
                    pct = float(src[key])
                    break
                except (TypeError, ValueError):
                    pass
        if pct is not None:
            break

    if pct is None:
        return None
    # Accept either 0.10 or 10 meaning 10%
    pct_display = pct * 100 if pct <= 1.0 else pct
    split = fut.get("budget_split") or risk.get("budget_split") or "50/50 SPY/QQQ"
    return f"{pct_display:.0f}% account budget, {split}"


def _ft_products(fut: dict) -> list[str]:
    products = fut.get("products")
    if isinstance(products, list) and products:
        return [str(p) for p in products]
    pairs = fut.get("pairs") if isinstance(fut.get("pairs"), list) else []
    out = []
    for p in pairs:
        if isinstance(p, dict) and p.get("product"):
            out.append(str(p["product"]))
    return out or ["SPY-PERP-INTX", "QQQ-PERP-INTX"]


def _ft_fills_from_db(day: date) -> list[dict]:
    if not FUT_DB.exists():
        return []
    try:
        con = sqlite3.connect(FUT_DB)
        con.row_factory = sqlite3.Row
        day_start_dt = datetime(day.year, day.month, day.day, tzinfo=CT)
        day_end_dt = day_start_dt + timedelta(days=1)
        day_start = day_start_dt.astimezone(timezone.utc).isoformat()
        day_end = day_end_dt.astimezone(timezone.utc).isoformat()
        rows = list(
            con.execute(
                """
                select product, side, strategy, round(notional_usd,2) notional,
                       round(price,6) price, reason, ts
                from fills
                where ts >= ? and ts < ?
                order by ts desc
                limit 20
                """,
                (day_start, day_end),
            )
        )
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _ft_closed_from_db(day: date) -> list[dict]:
    if not FUT_DB.exists():
        return []
    try:
        con = sqlite3.connect(FUT_DB)
        con.row_factory = sqlite3.Row
        day_start_dt = datetime(day.year, day.month, day.day, tzinfo=CT)
        day_end_dt = day_start_dt + timedelta(days=1)
        day_start = day_start_dt.astimezone(timezone.utc).isoformat()
        day_end = day_end_dt.astimezone(timezone.utc).isoformat()
        rows = list(
            con.execute(
                """
                select product, strategy, round(entry_price,6) entry_price,
                       round(exit_price,6) exit_price, round(realized_pnl,4) realized_pnl,
                       closed_at, status
                from positions
                where status='closed' and closed_at >= ? and closed_at < ?
                order by closed_at desc
                limit 20
                """,
                (day_start, day_end),
            )
        )
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []




def _treasury_kinds_line(data_dir: Path) -> str:
    """'By kind: Dividend 3 ($30.00) · Seed 1 ($100.00)' — daily_dca and dividend both show as Dividend."""
    try:
        from snowball.treasury.ledger import default_treasury_db

        db = default_treasury_db(Path(data_dir))
        if not db.exists():
            return ""
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(usd_amount), 0) FROM contributions GROUP BY kind"
            ).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return ""
    agg: dict[str, list[float]] = {}
    for kind, n, usd in rows:
        lab = kind_label(kind)
        cur = agg.setdefault(lab, [0, 0.0])
        cur[0] += int(n)
        cur[1] += float(usd or 0)
    if not agg:
        return ""
    parts = [f"{lab} {int(n)} ({_money(usd)})" for lab, (n, usd) in sorted(agg.items(), key=lambda kv: -kv[1][1])]
    return "By kind: " + " · ".join(parts)


_DASH = "\u2014"


def _ct_short(iso: object) -> str:
    try:
        t = datetime.fromisoformat(str(iso))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        t = t.astimezone(ZoneInfo("America/Chicago"))
        return f"{t.month}/{t.day} {t.strftime('%I:%M %p').lstrip('0')}"
    except (TypeError, ValueError):
        return "\u2014"


def _target_bar(frac: float, width: float = 216.0, height: float = 6.0) -> Table:
    """Thin progress bar for closed profit vs the daily profit minimum ($100/day)."""
    f = max(0.0, min(1.0, float(frac or 0)))
    done = max(width * f, 0.01)
    rest = max(width - done, 0.01)
    t = Table([["", ""]], colWidths=[done, rest], rowHeights=[height], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#16a34a" if f >= 1 else "#f59e0b")),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#e2e8f0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _floor_text(v: object) -> str:
    """Red-day dividend floor as "$10" (from dividend_status; NOT the $100/day profit minimum)."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        from snowball.treasury.dca import RED_DAY_FLOOR_USD as f
    return f"${f:,.0f}" if float(f) == int(f) else f"${f:,.2f}"


def ytd_chart(series: list[dict], width: float = 7.0 * inch, height: float = 1.15 * inch, minimum: float | None = None):
    """Small chart: daily net closed profit bars vs the rolling YTD average line (and the minimum)."""
    from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String

    d = Drawing(width, height)
    pts = [r for r in series if isinstance(r, dict) and r.get("net_usd") is not None]
    if not pts:
        d.add(String(4, height / 2, "No completed days yet", fontSize=7, fillColor=colors.HexColor("#94a3b8")))
        return d
    vals = [float(r["net_usd"]) for r in pts] + [float(r.get("avg_after_usd") or 0) for r in pts] + [0.0]
    if minimum:
        vals.append(float(minimum))
    hi, lo = max(vals), min(vals)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad_l, pad_b, pad_t = 34.0, 12.0, 6.0
    plot_h = height - pad_b - pad_t
    plot_w = width - pad_l - 4

    def y(v: float) -> float:
        return pad_b + (float(v) - lo) / (hi - lo) * plot_h

    step = plot_w / len(pts)
    bw = max(1.5, step * 0.62)
    d.add(Line(pad_l, y(0), width - 4, y(0), strokeColor=colors.HexColor("#cbd5e1"), strokeWidth=0.6))
    if minimum:
        d.add(Line(pad_l, y(minimum), width - 4, y(minimum), strokeColor=colors.HexColor("#16a34a"),
                   strokeWidth=0.5, strokeDashArray=[2, 2]))
        d.add(String(pad_l + 2, y(minimum) + 2, f"${float(minimum):,.0f} min", fontSize=5.5, fillColor=colors.HexColor("#16a34a")))
    line = []
    for i, r in enumerate(pts):
        x = pad_l + i * step + (step - bw) / 2
        v = float(r["net_usd"])
        top, bot = (y(v), y(0)) if v >= 0 else (y(0), y(v))
        col = "#16a34a" if r.get("beat") else ("#b91c1c" if v < 0 else "#94a3b8")
        d.add(Rect(x, bot, bw, max(top - bot, 0.6), fillColor=colors.HexColor(col), strokeColor=None))
        if r.get("avg_after_usd") is not None:
            line += [x + bw / 2, y(r["avg_after_usd"])]
    if len(line) >= 4:
        d.add(PolyLine(line, strokeColor=colors.HexColor("#f59e0b"), strokeWidth=1.2))
    for v in (hi, lo):
        d.add(String(0, y(v) - 2, _money(v), fontSize=5.5, fillColor=colors.HexColor("#64748b")))
    d.add(String(pad_l, 1, str(pts[0].get("date") or "")[5:], fontSize=5.5, fillColor=colors.HexColor("#64748b")))
    d.add(String(width - 22, 1, str(pts[-1].get("date") or "")[5:], fontSize=5.5, fillColor=colors.HexColor("#64748b")))
    return d


def ytd_lines(y: dict | None) -> list[str]:
    """Paragraph markup for the YTD average daily profit + $100 streak block (read-only)."""
    from snowball.reports.ytd_profit import vs_text

    if not isinstance(y, dict) or y.get("error"):
        return [f"<b>YTD average daily profit</b> \u00b7 unavailable ({(y or {}).get('error', 'no data')})"]
    avg = y.get("avg_usd")
    lines = [
        f"<b>YTD average daily profit: {_money(avg, signed=True) if avg is not None else _DASH}</b>"
        f" \u00b7 {int(y.get('days') or 0)} days counted since {y.get('first_day') or _DASH}"
        f" \u00b7 YTD total {_money(y.get('total_usd'), signed=True)}"
        f" \u00b7 {int(y.get('green_days') or 0)} green / {int(y.get('red_days') or 0)} red / {int(y.get('flat_days') or 0)} flat",
    ]
    yd, td = y.get("yesterday") or {}, y.get("today") or {}
    parts = []
    if yd:
        col = "#15803d" if yd.get("beat") else "#b91c1c"
        parts.append(f"Yesterday ({yd.get('date')}) {_money(yd.get('net_usd'), signed=True)} vs "
                     f"{_money(yd.get('vs_usd'), signed=True) if yd.get('vs_usd') is not None else _DASH} avg: "
                     f"<font color='{col}'>{vs_text(yd.get('diff_usd'), yd.get('beat'))}</font>")
    if td:
        parts.append(f"today so far ({td.get('date')} window) {_money(td.get('net_usd'), signed=True)}: "
                     f"{vs_text(td.get('diff_usd'), td.get('beating'), running=True)}")
    parts.append(f"beat streak {int(y.get('beat_streak') or 0)} \u00b7 days beaten {int(y.get('days_beaten') or 0)}"
                 f"/{int(y.get('days_compared') or 0)} YTD")
    lines.append(" \u00b7 ".join(parts))
    mn = y.get("minimum_usd")
    st_txt = y.get("min_streak_text") or ""
    if y.get("topup_unlocked"):
        lines.append(f"<font color='#15803d'><b>TOP-UP UNLOCKED: {st_txt}</b> \u2014 "
                     f"{int(y.get('min_streak') or 0)} straight days at or above {_money(mn)}; "
                     f"Tb's {_money(y.get('topup_usd'))} top-up is earned (flag only; nothing is moved automatically).</font>")
    else:
        n_over = int(y.get('days_over_minimum') or 0)
        lines.append(f"<b>{_floor_text(mn)} streak</b>: {st_txt} \u00b7 {n_over} {'day' if n_over == 1 else 'days'} at "
                     f"{_floor_text(mn)}+ YTD")
    lines.append(_esc(y.get("goal") or ""))
    lines.append(f"Day = {_esc(y.get('day_window') or 'dividend window')}, live closed P/L all lanes; red/flat/no-trade days count; "
                 f"resets each year on {y.get('year_reset')} CT (YEAR_RESET; next {y.get('next_reset')}). "
                 "Bars: daily profit (green = beat the average) \u00b7 amber line: rolling YTD average \u00b7 dashed: daily minimum.")
    return lines


def dividend_lines(data_dir: Path, now: datetime | None = None) -> list[str]:
    """Paragraph markup for the daily profit dividend block (read-only)."""
    from snowball.treasury.ledger import default_treasury_db

    try:
        st = dividend_status(default_treasury_db(Path(data_dir)), Path(data_dir), now=now)
    except Exception as e:  # noqa: BLE001
        return [f"<b>{DIVIDEND_LABEL}</b> \u00b7 status unavailable ({e})"]
    floor_txt = _floor_text(st.get("red_day_floor_usd"))
    lines = [f"<b>{DIVIDEND_LABEL}</b> \u00b7 {st.get('rule_text') or DIVIDEND_RULE}"]
    today = st.get("today") or {}
    state = str(today.get("state") or "pending")
    if state == "done" and today.get("outcome") == "rolled_below_min":
        state = "rolled (below Coinbase minimum)"
    elif state == "done":
        state = "paid"
    elif state == "skipped":
        state = f"skipped \u2014 {today.get('reason') or 'low cash'}"
    w = st.get("window") or {}
    pend = st.get("pending") or {}
    net = w.get("net_usd")
    rule = "profit day, 100% of net" if pend.get("rule") == "profit" else f"red/flat day, {floor_txt} floor"
    if pend.get("below_min"):
        rule = "profit below Coinbase minimum \u2014 rolls forward"
    lines.append(
        f"Today {today.get('ct_date', '')}: {state} \u00b7 window {_ct_short(w.get('window_start'))} \u2192 "
        f"{_ct_short(w.get('window_end'))} CT \u00b7 net closed live P/L so far "
        f"{_money(net, signed=True) if net is not None else _DASH} ({w.get('closes', 0)} closes) \u00b7 "
        f"next dividend {_ct_short(st.get('next_ct'))} CT: "
        f"{_money(pend.get('amount_usd')) if pend else _DASH} ({rule})"
        + (f" \u00b7 carry {_money(st.get('carry_usd'))}" if (st.get("carry_usd") or 0) > 0 else "")
    )
    unpaid = st.get("unpaid") or []
    if unpaid:
        lines.append(
            f"<font color='#b91c1c'>Unpaid dividend dates ({_esc(st.get('cutoff_label') or 'cutoff')} CT passed): " + ", ".join(unpaid[:10])
            + (f" (+{len(unpaid) - 10} more)" if len(unpaid) > 10 else "")
            + "</font> — paid oldest first by <i>treasury_dca.py --execute --catch-up</i>")
    if st.get("in_doubt_dates"):
        lines.append("<font color='#b91c1c'>In doubt (never auto-retried; check Coinbase): "
                     + ", ".join(st["in_doubt_dates"][:10]) + "</font>")
    cu = st.get("caught_up") or []
    if cu:
        lines.append("Caught up: " + " · ".join(
            f"{c.get('ct_date')} paid {_ct_short(c.get('at_ct'))} CT {_money(c.get('usd'))} "
            f"({float(c.get('btc') or 0):.8f} BTC @ {_money(c.get('price'))})" for c in cu[-5:]))
    cl = st.get("catch_up_last") or {}
    if cl.get("remaining_unpaid") and cl.get("stopped"):
        lines.append(f"Last catch-up stopped at {cl['stopped'].get('at_ct_date')}: "
                     f"{cl['stopped'].get('reason')}; remaining {', '.join(cl['remaining_unpaid'][:10])}")
    tg = st.get("target") or {}
    if tg.get("net_usd") is not None:
        min_txt = _floor_text(tg.get("target_usd") or st.get("daily_profit_min_usd") or st.get("daily_target_usd"))
        if tg.get("beat"):
            verdict = (f"<font color='#15803d'>cleared the {min_txt} minimum"
                       f" (+{_money(tg.get('overage_usd') or 0)} overage)</font>")
        else:
            verdict = f"below the {min_txt} minimum ({_money(tg.get('short_usd'))} to go)"
        src = ("paid from closed profit" if pend.get("rule") == "profit"
               else f"red/flat day \u2192 the {floor_txt} floor comes out of free cash")
        lines.append(f"Minimum: {tg.get('text')} closed profit ({tg.get('pct', 0):.0f}%) \u2014 {verdict}; {src}.")
    tot, wk, mo = st.get("totals") or {}, st.get("week") or {}, st.get("month") or {}
    share = st.get("dividend_btc_share_pct")
    lines.append(
        f"Dividends paid: {tot.get('count', 0)} \u00b7 {_money(tot.get('usd'))} \u00b7 {float(tot.get('btc') or 0):.8f} BTC"
        f" \u00b7 week {_money(wk.get('usd'))} ({wk.get('count', 0)}) \u00b7 month {_money(mo.get('usd'))} ({mo.get('count', 0)})"
        f" \u00b7 share of treasury BTC {('%.2f%%' % share) if share is not None else _DASH}"
        " \u00b7 50% P/L sweep retired (replaced by this dividend)"
    )
    rec = st.get("recent") or []
    if not rec:
        lines.append(f"No dividends yet \u2014 first at {_ct_short(st.get('next_ct'))} CT.")
    else:
        parts = []
        for r in rec:
            tag = {"green": "#15803d", "red": "#b91c1c"}.get(r.get("tag"), "#475569")
            nc = r.get("net_closed_usd")
            parts.append(
                f"{_ct_short(r.get('at_ct'))} <font color='{tag}'>{r.get('kind_label')}</font> "
                f"{_money(r.get('usd'))} \u00b7 {float(r.get('btc') or 0):.8f} BTC @ {_money(r.get('price'))}"
                + (f" (net {_money(nc, signed=True)}{', cleared ' + _floor_text(st.get('daily_profit_min_usd') or st.get('daily_target_usd')) if r.get('beat_target') else ''})" if nc is not None else "")
                + (" from free cash" if r.get("kind") == "red_day_floor" else "")
                + (f" · catch-up for {r.get('ct_date')}" if r.get("catch_up") else "")
            )
        lines.append("Recent: " + " \u00b7 ".join(parts))
    return lines


def append_bitcoin_treasury_section(
    story: list,
    *,
    data_dir: Path,
    h2: ParagraphStyle,
    body: ParagraphStyle,
    muted: ParagraphStyle,
    now: datetime | None = None,
) -> None:
    """Bitcoin treasury backstop — separate from CRYPTO trader lane."""
    story.append(Paragraph("Treasury (BTC + SPCX)", h2))
    try:
        snap = treasury_snapshot_for_pdf(data_dir, now=now, write_mark=True)
    except Exception as e:
        story.append(Paragraph(f"Treasury ledger unavailable ({e}).", body))
        story.append(
            Paragraph(
                f"Separate from CRYPTO trader · {DIVIDEND_LABEL}",
                muted,
            )
        )
        return

    mid = ParagraphStyle(
        "treas_mid",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        spaceAfter=2,
        textColor=colors.HexColor("#0f172a"),
    )
    cap = ParagraphStyle(
        "treas_cap",
        parent=muted,
        fontSize=8,
        textColor=colors.HexColor("#64748b"),
        leading=11,
        spaceAfter=2,
    )

    def _pct(v: float | None) -> str:
        if v is None:
            return "—"
        return ("+" if v >= 0 else "") + f"{v:.2f}%"

    def _pnl_color(v: float | None) -> colors.Color:
        if v is None:
            return colors.HexColor("#0f172a")
        return colors.HexColor("#15803d" if v >= 0 else "#b91c1c")

    avg_s = _money(snap.avg_price_usd) if snap.avg_price_usd is not None else "—"
    total_pnl = snap.total_pnl_usd
    total_s = _money(total_pnl, signed=True) if total_pnl is not None else "—"
    trend_s = _pct(snap.trend_30d_btc_spot_pct)
    weekly = snap.weekly_pnl_usd
    weekly_s = _money(weekly, signed=True) if weekly is not None else "—"

    mid_avg = ParagraphStyle("treas_avg", parent=mid)
    mid_pnl = ParagraphStyle(
        "treas_pnl", parent=mid, textColor=_pnl_color(total_pnl)
    )
    mid_trend = ParagraphStyle(
        "treas_trend",
        parent=mid,
        textColor=_pnl_color(snap.trend_30d_btc_spot_pct),
    )
    mid_week = ParagraphStyle(
        "treas_week", parent=mid, textColor=_pnl_color(weekly)
    )

    story.append(
        Table(
            [
                [
                    Paragraph("Average price (cost basis)", cap),
                    Paragraph("Total P/L (mark vs cost)", cap),
                ],
                [
                    Paragraph(avg_s, mid_avg),
                    Paragraph(total_s, mid_pnl),
                ],
                [
                    Paragraph(snap.trend_30d_label, cap),
                    Paragraph(snap.weekly_pnl_label, cap),
                ],
                [
                    Paragraph(trend_s, mid_trend),
                    Paragraph(weekly_s, mid_week),
                ],
            ],
            colWidths=[3.5 * inch, 3.5 * inch],
            hAlign="LEFT",
        )
    )

    if snap.empty:
        story.append(
            Paragraph(
                "No treasury BTC yet — ledger ready. "
                "Daily profit dividends (Robo Trader → Treasury) will appear here.",
                body,
            )
        )
    else:
        story.append(
            Paragraph(
                f"Holdings {snap.holdings_btc:.8f} BTC · "
                f"Cost {_money(snap.cost_basis_usd)} · "
                f"Mark {_money(snap.mark_value_usd)}"
                + (
                    f" @ {_money(snap.mark_btc_usd)}/BTC"
                    if snap.mark_btc_usd is not None
                    else ""
                )
                + f" · {snap.contribution_count} contribution"
                + ("s" if snap.contribution_count != 1 else ""),
                body,
            )
        )
        kinds_line = _treasury_kinds_line(data_dir)
        if kinds_line:
            story.append(Paragraph(kinds_line, muted))
    try:
        from snowball.treasury.equities import equity_holdings
        from snowball.treasury.ledger import default_treasury_db as _tdb

        _eq = equity_holdings(_tdb(Path(data_dir)), write_mark=True)
    except Exception:  # noqa: BLE001
        _eq = []
    btc_val = float(snap.mark_value_usd or 0.0) if not snap.empty else 0.0
    eq_val = 0.0
    for h in _eq:
        if h.shares <= 0:
            continue
        eq_val += h.value_usd
        story.append(
            Paragraph(
                f"{h.symbol} {h.shares:g} shares @ "
                + (_money(h.price) if h.price else "—")
                + f" = {_money(h.value_usd)} · cost {_money(h.cost_usd)}"
                + ("" if h.price_source == "live" else f" ({h.price_source.replace('_', ' ')})")
                + " · external deposit, receive-only (never traded)",
                body,
            )
        )
    if _eq:
        story.append(
            Paragraph(
                f"<b>Total treasury {_money(btc_val + eq_val)}</b> "
                f"(BTC {_money(btc_val)} + stock {_money(eq_val)}) · "
                "not counted in trading equity, bankroll, or P/L",
                body,
            )
        )
    for i, line in enumerate(dividend_lines(data_dir, now)):
        story.append(Paragraph(line, body if i == 0 else muted))
        if line.startswith("Minimum: "):
            try:
                from snowball.treasury.dca import target_progress
                from snowball.treasury.ledger import default_treasury_db

                _st = dividend_status(default_treasury_db(Path(data_dir)), Path(data_dir), now=now)
                story.append(_target_bar((_st.get("target") or target_progress(None)).get("bar") or 0.0))
            except Exception:  # noqa: BLE001
                pass

    story.append(
        Paragraph(
            "Separate from CRYPTO trader (backstop) · "
            f"{DIVIDEND_LABEL} · "
            f"{snap.trend_30d_label} = BTC-USD spot % change over 30 calendar days · "
            f"{snap.weekly_pnl_label} = change in treasury unrealized P/L over 7 days "
            "(ex-contribution; — until marks exist)",
            muted,
        )
    )


def fetch_real_balances_for_report(fut: dict | None = None) -> dict:
    """REAL Coinbase balances (read-only). Falls back to the snapshot's CFM liq view."""
    try:
        from snowball.config import Settings
        from snowball.reports.real_balances import fetch_real_balances

        rb = fetch_real_balances(Settings())
        if rb.get("available"):
            return rb
    except Exception as e:  # noqa: BLE001
        rb = {"available": False, "why": f"{type(e).__name__}"}
    try:
        for v in (((fut or {}).get("stops") or {}).get("lanes") or {}).values():
            liq = (v or {}).get("liq") or {}
            if liq.get("available"):
                return {"available": True, "source": "snapshot_liq_fallback", "at": v.get("at"),
                        "cfm": {"available_margin": liq.get("available_margin"),
                                "liquidation_buffer_amount": liq.get("current_liquidation_buffer")},
                        "spot_usd_free": None, "fallback_why": rb.get("why")}
    except Exception:  # noqa: BLE001
        pass
    return rb


def _money_or_dash(v: object, *, signed: bool = False) -> str:
    return "\u2014" if v is None else _money(v, signed=signed)


def append_real_balances_section(story: list, rb: dict, *, h2: ParagraphStyle, body: ParagraphStyle,
                                 muted: ParagraphStyle, report_day: date) -> None:
    story.append(Paragraph("Real Coinbase balances", h2))
    if not rb or not rb.get("available"):
        story.append(Paragraph(f"Unavailable ({_esc((rb or {}).get('why') or 'no data')}).", body))
        return
    cfm = rb.get("cfm") or {}
    at = _ct_short(rb.get("at"))
    rows = [
        ["CFM futures total USD balance", _money_or_dash(cfm.get("cfm_usd_balance"))],
        ["CFM unrealized P/L", _money_or_dash(cfm.get("unrealized_pnl"), signed=True)],
        ["CFM liquidation buffer",
         _money_or_dash(cfm.get("liquidation_buffer_amount"))
         + (f" ({float(cfm['liquidation_buffer_percentage']):.0f}%)"
            if cfm.get("liquidation_buffer_percentage") is not None else "")],
        ["CFM available margin", _money_or_dash(cfm.get("available_margin"))],
        ["Spot USD free", _money_or_dash(rb.get("spot_usd_free"))],
    ]
    if cfm.get("total_usd_balance") is not None:
        rows.append(["Coinbase total USD (CFM + spot cash)", _money_or_dash(cfm.get("total_usd_balance"))])
    story.append(_kv_table(rows, [3.2 * inch, 3.8 * inch]))
    story.append(Paragraph(
        f"Read live from Coinbase as of {at} CT (report day {report_day.isoformat()}; balances are current, "
        "not end-of-day). These are real money. Lane ledger figures elsewhere in this report "
        "(e.g. Futures 'lane budget') are Snowball's internal budgets, not balances."
        + (" Source: snapshot fallback." if rb.get("source") == "snapshot_liq_fallback" else ""),
        muted))


def _budget_pct_label(fut: dict) -> str:
    risk = fut.get("risk") if isinstance(fut.get("risk"), dict) else {}
    try:
        return f"{float(risk.get('budget_pct')) * 100:.0f}%"
    except (TypeError, ValueError):
        return "60%"


def append_future_trader_section(
    story: list,
    *,
    fut: dict,
    day: date,
    is_today: bool,
    h2: ParagraphStyle,
    body: ParagraphStyle,
    muted: ParagraphStyle,
) -> None:
    """Append Future Trader block — paper/live safe; defensive field reads."""
    if not fut or fut.get("enabled") is False:
        story.append(Paragraph("Futures (Future Trader + former Stock lane)", h2))
        story.append(Paragraph("Lane disabled or unavailable.", body))
        return

    risk = fut.get("risk") if isinstance(fut.get("risk"), dict) else {}
    status = fut.get("status") if isinstance(fut.get("status"), dict) else {}
    mode = (
        fut.get("futures_mode")
        or fut.get("mode")
        or status.get("mode")
        or "paper"
    )
    live_flag = fut.get("futures_live_enabled")
    if live_flag is None:
        live_flag = fut.get("live_enabled")

    cash = risk.get("cash_usd")
    equity = risk.get("equity_usd")
    daily_pnl = risk.get("daily_pnl_usd")
    if daily_pnl is None:
        daily_pnl = risk.get("daily_pnl")

    story.append(Paragraph("Futures (Future Trader + former Stock lane)", h2))

    mode_bits = [f"Mode {mode}"]
    if live_flag is not None:
        mode_bits.append("live_enabled=yes" if live_flag else "live_enabled=no")
    if status.get("trading_enabled") is False:
        mode_bits.append("trading paused")
    if status.get("daily_killed"):
        mode_bits.append("daily kill")
    story.append(Paragraph(" · ".join(mode_bits), body))

    # 2026-10-10: the FT ledger cash/equity are an internal lane BUDGET, not Coinbase money.
    # Real Coinbase balances are the headline section at the top of the report.
    book_bits = []
    if cash is not None:
        book_bits.append(f"cash {_money(cash)}")
    if equity is not None:
        book_bits.append(f"equity {_money(equity)}")
    if book_bits:
        story.append(Paragraph("Lane budget (internal): " + "&nbsp;&nbsp;·&nbsp;&nbsp;".join(book_bits)
                               + " &mdash; Snowball's internal ledger, NOT real money (see Real Coinbase balances)",
                               muted))
    if daily_pnl is not None:
        story.append(Paragraph(f"Daily P/L (ledger) {_money(daily_pnl, signed=True)}", body))

    comb = fut.get("combined_closed_pnl") if isinstance(fut.get("combined_closed_pnl"), dict) else None
    if comb:
        bl = comb.get("by_original_lane") or {}
        ft_b = bl.get("futures") or {}
        st_b = bl.get("stocks") or {}
        story.append(
            Paragraph(
                f"Closed P/L (live) all-time {_money(comb.get('all_time_usd'), signed=True)} · "
                f"YTD {_money(comb.get('ytd_usd'), signed=True)} "
                f"(FT {_money(ft_b.get('all_time_usd'), signed=True)} + former Stock lane "
                f"{_money(st_b.get('all_time_usd'), signed=True)} all-time)",
                body,
            )
        )
    merged = fut.get("merged_stock") if isinstance(fut.get("merged_stock"), dict) else None
    if merged and merged.get("enabled"):
        story.append(
            Paragraph(
                "Stock strategies in lane: " + ", ".join(merged.get("strategies") or []),
                muted,
            )
        )

    liq = _ft_liquidity_note(fut)
    if liq:
        story.append(Paragraph(f"Liquidity cap: {liq}", body))

    # Per-index open lots
    products = _ft_products(fut)
    # Prefer SPY/QQQ order
    def _sort_key(p: str) -> tuple:
        lab = _index_label(p)
        try:
            return (FT_INDEX_ORDER.index(lab), lab)
        except ValueError:
            return (99, lab)

    products = sorted(products, key=_sort_key)

    pairs_by = {}
    for p in fut.get("pairs") or []:
        if isinstance(p, dict) and p.get("product"):
            pairs_by[str(p["product"])] = p

    positions = [p for p in (fut.get("positions") or []) if isinstance(p, dict)]
    pos_by_product: dict[str, list[dict]] = {}
    for p in positions:
        pos_by_product.setdefault(str(p.get("product") or ""), []).append(p)

    # Also merge any indices / sessions map if present (future schema)
    sessions = fut.get("sessions") or fut.get("indices") or {}
    if not isinstance(sessions, dict):
        sessions = {}

    idx_rows = []
    now_ct = datetime.now(CT)
    for product in products:
        lab = _index_label(product)
        pair = pairs_by.get(product) or sessions.get(lab) or sessions.get(product) or {}
        if not isinstance(pair, dict):
            pair = {}
        open_lots = pos_by_product.get(product) or []
        # Some APIs may nest open lot under pair
        if not open_lots and isinstance(pair.get("open_lot"), dict):
            open_lots = [pair["open_lot"]]
        pos = open_lots[0] if open_lots else None
        has_lot = "yes" if open_lots else "no"
        if pos:
            entry = pos.get("entry_price")
            mark = pos.get("mark")
            if mark is None:
                mark = pair.get("last") or pair.get("mark")
            u_pnl = pos.get("unrealized_pnl")
            if u_pnl is None and entry is not None and mark is not None:
                try:
                    u_pnl = (float(mark) - float(entry)) * float(pos.get("qty") or 0)
                except (TypeError, ValueError):
                    u_pnl = None
            entry_s = f"${float(entry):,.2f}" if entry is not None else "—"
            mark_s = f"${float(mark):,.2f}" if mark is not None else "—"
            upnl_s = _money(u_pnl, signed=True) if u_pnl is not None else "—"
        else:
            mark = pair.get("last") or pair.get("mark")
            entry_s = "—"
            mark_s = f"${float(mark):,.2f}" if mark is not None else "—"
            upnl_s = "—"
        status_txt = _ft_session_status(pos, pair, now_ct)
        # closed_green may only appear after session close in API
        if not open_lots:
            closed = pair.get("closed_status") or sessions.get(f"{lab}_closed")
            if isinstance(closed, str) and closed.strip():
                status_txt = closed.strip()
        idx_rows.append([lab, has_lot, entry_s, mark_s, upnl_s, status_txt])

    if not idx_rows:
        idx_rows = [["—", "no", "—", "—", "—", "flat"]]

    story.append(
        make_table(
            ["Index", "Open lot", "Entry", "Mark", "Unrealized", "Status"],
            idx_rows,
            [0.7 * inch, 0.85 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch, 1.35 * inch],
        )
    )
    story.append(Spacer(1, 6))

    # Today's fills / closed session trades
    api_fills = [f for f in (fut.get("fills") or []) if isinstance(f, dict)]
    day_fills = [f for f in api_fills if _in_ct_day(f.get("ts"), day)]
    if not day_fills:
        day_fills = _ft_fills_from_db(day)

    closed_day = [
        c
        for c in (fut.get("closed_trades") or fut.get("session_closes") or [])
        if isinstance(c, dict) and (_in_ct_day(c.get("closed_at") or c.get("ts"), day) or is_today)
    ]
    if not closed_day:
        closed_day = _ft_closed_from_db(day)

    fills_title = "FT fills / closed (CT day so far)" if is_today else f"FT fills / closed for {day.isoformat()} (CT)"
    story.append(Paragraph(fills_title, body))

    fill_rows = []
    for f in day_fills[:12]:
        fill_rows.append(
            [
                _index_label(f.get("product")),
                f.get("side") or "—",
                f.get("strategy") or "—",
                _money(f.get("notional_usd") if f.get("notional_usd") is not None else f.get("notional")),
                (str(f.get("reason") or ""))[:22] or "—",
            ]
        )
    for c in closed_day[:8]:
        # Avoid duplicating if already shown as fill; still useful for session closes
        pnl = c.get("realized_pnl")
        fill_rows.append(
            [
                _index_label(c.get("product")),
                "close",
                c.get("strategy") or "—",
                _money(pnl, signed=True) if pnl is not None else "—",
                "closed_green" if (pnl is not None and float(pnl) >= 0) else "session close",
            ]
        )

    if fill_rows:
        story.append(
            make_table(
                ["Index", "Side", "Strategy", "Notional/PnL", "Reason"],
                fill_rows[:15],
                [0.7 * inch, 0.7 * inch, 1.2 * inch, 1.3 * inch, 2.0 * inch],
            )
        )
    else:
        story.append(Paragraph(f"No FT fills or closed session trades on {day.isoformat()} (CT).", muted))

    # Strategy split: session_day vs momentum_15m
    by_strat = fut.get("fills_by_strategy") if isinstance(fut.get("fills_by_strategy"), dict) else {}
    if not by_strat and day_fills:
        by_strat = {}
        for f in day_fills:
            sid = str(f.get("strategy") or "unknown")
            by_strat.setdefault(sid, {"fills": 0})
            by_strat[sid]["fills"] = by_strat[sid].get("fills", 0) + 1
    if by_strat:
        story.append(Paragraph("Futures by strategy (session / momentum / stock strategies)", body))
        strat_rows = []
        for sid in ("session_day", "momentum_15m"):
            b = by_strat.get(sid) or {}
            if not b and sid not in by_strat:
                continue
            pnl = b.get("realized_pnl")
            strat_rows.append(
                [
                    sid,
                    str(b.get("fills") or b.get("buy") or "—"),
                    _money(pnl, signed=True) if pnl is not None else "—",
                ]
            )
        for sid, b in by_strat.items():
            if sid in ("session_day", "momentum_15m"):
                continue
            pnl = b.get("realized_pnl")
            strat_rows.append(
                [
                    sid,
                    str(b.get("fills") or "—"),
                    _money(pnl, signed=True) if pnl is not None else "—",
                ]
            )
        if strat_rows:
            story.append(
                make_table(
                    ["Strategy", "Fills", "Realized PnL"],
                    strat_rows,
                    [2.0 * inch, 1.0 * inch, 1.5 * inch],
                )
            )
        story.append(Spacer(1, 4))

    mom = fut.get("momentum") if isinstance(fut.get("momentum"), dict) else {}
    target = fut.get("daily_pnl_target_usd")
    if mom.get("enabled") or target is not None:
        bits = []
        if mom.get("enabled"):
            bits.append(
                f"momentum {mom.get('timeframe', '15m')} lookback={mom.get('lookback_bars')} "
                f"min={mom.get('min_momentum_pct')} TP={mom.get('take_profit_pct')}"
            )
        if target is not None:
            bits.append(f"aspirational KPI ~${float(target):.0f}/day (report only)")
        story.append(Paragraph(" · ".join(bits), muted))

    _append_stops_block(story, fut, body=body, muted=muted)

    story.append(Spacer(1, 4))
    story.append(
        Paragraph(
            "Rules: session exits at close only if green else hold overnight; "
            "momentum exits on TP/stall/EOD if green; stock strategies exit on signal/fade/stall "
            "if green; never_sell_red / never_cover_red; ONE net position per index across all "
            "Futures strategies (max CFM_MAX_CONTRACTS, no scale-in); one Futures budget "
            f"~{_budget_pct_label(fut)}",
            muted,
        )
    )


def _append_stops_block(story: list, fut: dict, *, body: ParagraphStyle, muted: ParagraphStyle) -> None:
    """Stops / liquidation guard / netted pair / one-index gate / spot USD floor (2026-10-01)."""
    stops = fut.get("stops") if isinstance(fut.get("stops"), dict) else None
    if not stops:
        return
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"<b>Futures stops</b> ({'ON' if stops.get('enabled') else 'OFF'}): {_esc(stops.get('rule', ''))}", body))
    rows: list[list[object]] = []
    lanes = stops.get("lanes") if isinstance(stops.get("lanes"), dict) else {}
    for lane in sorted(lanes):
        lv = lanes.get(lane)
        # stops_snapshot gives {lane: {"lots": [...], "liq": ..., ...}}; older payloads a bare list.
        lots = lv.get("lots") if isinstance(lv, dict) else lv
        for v in (lots or []):
            if not isinstance(v, dict):
                continue
            netted = bool(v.get("netted"))
            liq = v.get("liq_headroom_usd_per_contract")
            rows.append([
                f"{lane} #{v.get('id')}",
                f"{_index_label(v.get('product'))} {v.get('side', '')} x{v.get('contracts', '')}",
                "-" if v.get("basis") is None else f"{float(v['basis']):,.1f}",
                "netted" if netted else ("-" if v.get("stop_px") is None else f"{float(v['stop_px']):,.1f} ({v.get('stop_leg', '')})"),
                "netted" if netted else _money(v.get("distance_to_stop_usd")),
                "-" if netted or liq is None else f"{_money(liq)}/ct (liq~{v.get('liq_px_est')})",
                _aged_cell(v),
            ])
    if rows:
        story.append(make_table(["Lot", "Position", "Basis", "Stop px", "To stop", "Liq headroom", "Aged exit"], rows,
                                [0.7 * inch, 1.2 * inch, 0.65 * inch, 1.05 * inch, 0.7 * inch, 1.2 * inch, 1.5 * inch]))
    ag = stops.get("aged_exit") if isinstance(stops.get("aged_exit"), dict) else {}
    if ag:
        story.append(Paragraph(
            f"Aged-lot exit: {'ON' if ag.get('enabled') else 'OFF'} — {_esc(ag.get('rule', ''))}", muted))
    net = stops.get("netted") if isinstance(stops.get("netted"), dict) else {}
    for p in (net.get("pairs") or []):
        if isinstance(p, dict) and p.get("valid"):
            story.append(Paragraph(
                f"Netted pair {_esc(p.get('pair'))} ({_esc(_index_label(p.get('product')))}): "
                f"locked P&amp;L gross {_money(p.get('netted_pnl_gross_usd'), signed=True)}, "
                f"net of entry fees {_money(p.get('netted_pnl_net_usd'), signed=True)} — bookkeeping only, "
                "excluded from stops/exits/margin, no order", muted))
    gate = stops.get("one_index_gate") if isinstance(stops.get("one_index_gate"), dict) else {}
    if gate:
        opens = gate.get("open_index_contracts") or []
        desc = ", ".join(f"{o.get('lane')}#{o.get('id')} {o.get('side')}" for o in opens if isinstance(o, dict))
        story.append(Paragraph(
            f"One-index-contract gate: {'ON' if gate.get('enabled') else 'OFF'} — "
            f"{'BLOCKING new US5/TEK entries' if gate.get('blocking_new_index_entries') else 'open for entries'}"
            f"{' (open: ' + _esc(desc) + ')' if desc else ''}; rolls allowed", muted))
    blocks = stops.get("reentry_blocks") or []
    if blocks:
        story.append(Paragraph("Re-entry blocked until next ET day: " + _esc(", ".join(
            str(b.get("family")) for b in blocks if isinstance(b, dict))), muted))
    sf = stops.get("spot_usd_floor") if isinstance(stops.get("spot_usd_floor"), dict) else {}
    if sf:
        floor = float(sf.get("floor_usd") or 0.0)
        if floor > 0:
            story.append(Paragraph(
                f"Spot USD floor {_money(floor)}: spot USD {_money(sf.get('last_free_usd'))}, "
                f"headroom {_money(sf.get('headroom_usd'))}; blocked buys {int(sf.get('blocked_buys') or 0)} "
                f"({_money(sf.get('blocked_usd') or 0)}) — sells and treasury dividend exempt", muted))
        else:
            story.append(Paragraph("Spot USD floor: OFF (SPOT_USD_FLOOR=0)", muted))


def _aged_cell(v: dict) -> str:
    """'aged-exit armed' (age, target price, move still needed) / young / netted (2026-10-02)."""
    age = v.get("age_days")
    age_s = "" if age is None else f"{float(age):.1f}d"
    if v.get("netted"):
        return f"netted (excluded){', ' + age_s if age_s else ''}"
    if v.get("aged_exit_armed"):
        tgt = v.get("aged_target_px")
        mv = v.get("aged_move_needed")
        if v.get("aged_qualifies"):
            return f"aged-exit armed {age_s}: net green now"
        return (f"aged-exit armed {age_s}: closes at {float(tgt):,.1f} "
                f"{'or lower' if v.get('side') == 'short' else 'or higher'}"
                + ("" if mv is None else f" ({float(mv):,.1f} away)"))
    if v.get("aged_excluded") == "disabled":
        return "off"
    return f"young {age_s}" if age_s else "-"


def _esc(v: object) -> str:
    return str(v if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _kv_table(rows: list[list[str]], widths: list[float]) -> Table:
    k = ParagraphStyle("rk", fontSize=7, textColor=colors.HexColor("#475569"), leading=9, fontName="Helvetica-Bold")
    v = ParagraphStyle("rv", fontSize=7, textColor=colors.HexColor("#0f172a"), leading=9)
    data = [[Paragraph(_esc(a), k), Paragraph(_esc(b), v)] for a, b in rows]
    t = Table(data, colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _small_table(headers: list[str], rows: list[list[object]], widths: list[float]) -> Table:
    th = ParagraphStyle("rth", fontSize=7, textColor=colors.white, leading=9, fontName="Helvetica-Bold")
    td = ParagraphStyle("rtd", fontSize=7, textColor=colors.HexColor("#0f172a"), leading=9)
    data = [[Paragraph(_esc(h), th) for h in headers]] + [[Paragraph(_esc(c), td) for c in r] for r in rows]
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _fmt_ind(ind: dict) -> str:
    parts = []
    for k, v in (ind or {}).items():
        if k in ("bid", "ask"):
            continue
        if isinstance(v, float):
            v = f"{v:,.6g}"
        parts.append(f"{k}={v}")
    return ", ".join(parts) or "—"


def _fmt_dict(d: object) -> str:
    if not isinstance(d, dict) or not d:
        return "—"
    out = []
    for k, v in d.items():
        if isinstance(v, float):
            v = f"{v:,.6g}"
        elif isinstance(v, (list, dict)):
            v = json.dumps(v, default=str)[:90]
        out.append(f"{k}={v}")
    return ", ".join(out)


def trade_rows(t: dict, findings: dict) -> list[list[str]]:
    """Key/value rows for one fill in the Why-we-traded table (stored values only)."""
    ctx = t.get("context")
    head = (f"{t['lane']} · {t['product']} · {t['action']} {t['side']} · {t.get('strategy') or '—'} · "
            f"fill #{t['fill_id']} · {rr.ct_label(t['ts'])}")
    rows = [["Trade", head], ["Price", f"{float(t.get('price') or 0):,.6g}"
                              + (f" · entry {float(t['entry_price']):,.6g}" if t.get("entry_price") else "")
                              + (f" · exit {float(t['exit_price']):,.6g}" if t.get("exit_price") else "")
                              + (f" · P/L {_money(t['realized_pnl'], signed=True)}" if t.get("realized_pnl") is not None
                                 and t.get("action") == "exit" else "")]]
    rows.append(["Rule fired", str(t.get("reason") or "—")])
    if not ctx:
        rows.append(["Signal context", "context not recorded (fill predates signal-context snapshots)"])
    elif ctx.get("source") == "fill_only":
        rows.append(["Signal context", "context not recorded by the engine for this path (reconcile/adopt/emergency); "
                                       "gate snapshot only"])
    else:
        rows.append(["Rule", str(ctx.get("rule") or "—")])
        rows.append(["Indicators", _fmt_ind(ctx.get("indicators") or {})])
        tf = ctx.get("timeframe")
        rows.append(["Timeframe / candle", f"{tf or '—'} · {rr.ct_label(ctx.get('candle_ts')) if ctx.get('candle_ts') else '—'}"])
        if ctx.get("floor") or ctx.get("target"):
            rows.append(["Floor / target", "; ".join(x for x in (_fmt_dict(ctx.get("floor")) if ctx.get("floor") else "",
                                                               _fmt_dict(ctx.get("target")) if ctx.get("target") else "") if x)])
        rows.append(["Gates passed", _fmt_dict(ctx.get("gates"))])
    infl = rr.news_influence(ctx)
    if ctx and not rr.has_news_input(ctx):
        infl = infl + ["technical signal only; no news input"]
    rows.append(["News that influenced it", " | ".join(infl) if infl else
                 ("no news input" if ctx else "context not recorded")])
    rel = rr.related_news(findings, t.get("product"), t.get("ts"))
    rows.append(["Related news, prior 24h (context, not a trigger)", " | ".join(rel) if rel else "none on this ticker"])
    return rows


def _pct(v: object, nd: int = 0) -> str:
    try:
        return f"{float(v):.{nd}%}"
    except (TypeError, ValueError):
        return "—"


def append_research_learning_section(story, *, data_dir: Path, start: datetime, end: datetime,
                                     h2, body, muted, max_backfill: int = 60) -> None:
    """Research: videos learned today + prospective plays with probabilities (Tb 3:45 AM CT 10/3)."""
    import os

    small = ParagraphStyle("rls", parent=body, fontSize=7.5, leading=9.5, spaceAfter=3)
    h3 = ParagraphStyle("rlh3", parent=body, fontName="Helvetica-Bold", fontSize=8.5, leading=11, spaceBefore=6, spaceAfter=2)
    try:
        min_prob = float(os.environ.get("RESEARCH_EXEC_MIN_PROB", "0.70") or 0.70)
    except ValueError:
        min_prob = 0.70
    need_ev = str(os.environ.get("RESEARCH_EXEC_REQUIRE_POSITIVE_EV", "true")).strip().lower() not in ("0", "false", "no", "off")
    story.append(PageBreak())
    story.append(Paragraph("Research: videos learned today", h2))
    v = rr.videos_learned(data_dir / "snowball.db", start, end)
    vids = v.get("videos") or []
    story.append(Paragraph(
        f"Window {rr.ct_label(start)} → {rr.ct_label(end)} · {len(vids)} video transcripts stored "
        f"({v.get('fresh', 0)} new uploads ≤48h, {v.get('backfill', 0)} backfill of older uploads) · by source: "
        + (", ".join(f"{k} {n}" for k, n in sorted((v.get('by_source') or {}).items())) or "none")
        + " · by channel: " + (", ".join(f"@{k} {n}" for k, n in sorted((v.get('by_channel') or {}).items())) or "none")
        + f" · videos still without a transcript (all time): {v.get('not_ok_total', '—')}", small))
    story.append(Paragraph(
        "Notes are verbatim key sentences picked from each transcript by an extractive scorer (economic / market "
        "vocabulary, numbers, tickers; sponsor/outro filler skipped). Nothing is paraphrased or invented. "
        "Stance = ticker bull/bear keyword hits from the transcript. Only NEW uploads (published in the last 24h) "
        "can create trade proposals; backfilled older videos feed the source track record and these notes only.", muted))
    if v.get("state") != "ok":
        story.append(Paragraph(f"Videos: {v.get('state')} · {_esc(v.get('why'))}", small))
    shown = [x for x in vids if x["fresh"]] + [x for x in vids if not x["fresh"]][:max_backfill]
    rows = []
    for x in shown:
        notes = x.get("notes") or []
        if not x["fresh"]:
            notes = notes[:2]
        ntext = " ".join(f"• {n}" for n in notes) if notes else "no economic/market sentence found in transcript"
        if x.get("stance"):
            ntext = f"[{x['stance']}] " + ntext
        rows.append([f"@{x['channel']}", str(x["title"])[:90] + ("" if x["fresh"] else " (backfill)"),
                     rr.ct_label(x["published_at"], "%m/%d/%y"), x["source"].replace(" (audio transcription)", ""),
                     ntext])
    if rows:
        story.append(_small_table(["Channel", "Title", "Published", "Transcript", "Notes (economic insight, verbatim)"],
                                  rows, [0.95 * inch, 1.6 * inch, 0.6 * inch, 0.75 * inch, 3.3 * inch]))
    else:
        story.append(Paragraph("No video transcripts were stored in this window.", small))
    rest = len(vids) - len(shown)
    if rest > 0:
        story.append(Paragraph(f"+{rest} more backfill transcripts in this window not listed "
                               "(all are stored and used for the source track record).", muted))

    # Prospective plays
    story.append(Paragraph("Prospective plays", h3))
    pl = rr.prospective_plays(data_dir, start, end, min_prob=min_prob)
    plays = pl.get("plays") or []
    if pl.get("state") != "ok":
        story.append(Paragraph(f"Proposals: {pl.get('state')} · {_esc(pl.get('why'))}", small))
    if plays:
        prow = []
        for x in plays[:25]:
            tgt = _pct(x.get("target_pct"), 2) if x.get("target_pct") is not None else "—"
            stp = (_pct(x.get("stop_pct"), 2) if x.get("stop_pct") is not None else
                   ("none (never-sell-red)" if x.get("kind") == "spot" else "—"))
            pcell = (_pct(x.get("probability"), 0) if x.get("probability") is not None
                     else f"n/a ({str(x.get('prob_why') or 'not scored')[:40]})")
            if x.get("probability") is not None:
                pcell += f" (base {_pct(x.get('p_base'))}, tech {_pct(x.get('p_tech'))}, research {float(x.get('research_delta') or 0):+.2f})"
                if x.get("break_even_p") is not None:
                    pcell += f" · break-even {_pct(x.get('break_even_p'))} · EV {float(x.get('ev_pct') or 0):+.2%}"
            prow.append([f"#{x['id']} {x['ticker']}" + (f" → {x['product']}" if x.get("product") else ""),
                         x["side"], str(x.get("thesis") or "")[:110], str(x.get("sources") or "")[:120],
                         pcell, ("—" if x.get("target_pct") is None else
                                 (f"+{tgt} / -{stp}" if x.get("stop_pct") is not None else f"+{tgt} / {stp}")),
                         x["status"]])
        story.append(_small_table(["Play", "Dir", "Thesis", "Source videos / filings", "P(success)", "Target / stop", "Status"],
                                  prow, [0.85 * inch, 0.35 * inch, 1.35 * inch, 1.35 * inch, 1.35 * inch, 0.75 * inch, 1.2 * inch]))
        if len(plays) > 25:
            story.append(Paragraph(f"+{len(plays) - 25} more proposals not shown", muted))
    elif pl.get("state") == "ok":
        story.append(Paragraph("No plays proposed in this window.", small))
    ex = [x for x in plays if x.get("probability") is not None and x["probability"] >= min_prob]
    story.append(Paragraph(
        f"<b>Execution rule</b>: RESEARCH_EXEC_MIN_PROB = {min_prob:.0%}. Plays at or above it are executed by the "
        f"research_idea lane{' when their expected value after fees is positive' if need_ev else ''}, through every "
        "existing guardrail (1 contract / 1x caps, FUTURES_ONE_INDEX_CONTRACT, futures stops 8% or $250 per contract "
        "+ $50 liquidation guard, spot never-sell-red, treasury receive-only, TEK-first, aged-lot exit, normal sizing). "
        "Below the threshold: logged only. The same idea (same source videos/filings) is never entered twice. "
        f"In this window: {len(ex)} play(s) at/above {min_prob:.0%}.", small))
    tr = (pl.get("track_record") or {}).get("families") or {}
    if tr:
        story.append(Paragraph("<b>Source track record</b> (past calls, 3%/3% reference bracket, 72h): " + "; ".join(
            f"{k}: {v.get('n', 0)} calls, hit {_pct(v.get('hit_rate'))} vs base {_pct(v.get('base'))} → "
            f"{float(v.get('delta') or 0):+.2f} logit" if v.get("hit_rate") is not None else f"{k}: no measured record → no credit"
            for k, v in sorted(tr.items())), small))
    story.append(Paragraph(
        "<b>How P(success) is computed</b> (transparent, from history; never inflated): success = the lane's real "
        "target is touched before its real stop within 72h of entry (futures: +0.8% take-profit vs the futures stop; "
        "spot: the research_idea green floor, no stop; non-tradable tickers: 3%/3% reference). "
        "(1) Base = how often that happened from every hourly start over the last ~180 days of 1h price history "
        "(Yahoo; index futures use SPY/QQQ, BIP uses BTC-USD, NOL uses CL=F). (2) Technical confirmation = the same "
        "frequency only for past starts in today's trend state (close vs 50-bar SMA), shrunk toward the base by "
        "its effective sample size. (3) Research = each agreeing source family's measured hit rate on its own past "
        "calls vs the base rate of those same windows, shrunk with 20 pseudo-observations (no history → 0 credit; "
        "capped ±0.75 per family, ±1.0 total). P = logistic(logit(tech) + research). Both target and stop inside "
        "one hour counts as a stop. A high P can still lose money when the stop is far wider than the target, "
        "so break-even P and EV are shown and negative-EV plays are not executed.", muted))


def append_research_report_section(story, *, data_dir: Path, start: datetime, end: datetime,
                                   h2, body, muted, max_items: int = 6, max_trades: int = 6) -> None:
    """Research Report: findings, why-we-traded, research proposals. Real stored data only."""
    small = ParagraphStyle("rs", parent=body, fontSize=7.5, leading=9.5, spaceAfter=3)
    h3 = ParagraphStyle("rh3", parent=body, fontName="Helvetica-Bold", fontSize=8.5, leading=11, spaceBefore=6, spaceAfter=2)
    W = 7.2 * inch
    story.append(PageBreak())
    story.append(Paragraph("Research Report", h2))
    story.append(Paragraph(
        f"Window {rr.ct_label(start)} → {rr.ct_label(end)} (last 24h) · stored feed data only; failed feeds shown as unavailable",
        muted))
    f = rr.collect_findings(data_dir, start, end)

    # 1. New findings by source
    story.append(Paragraph("1 · New findings (last 24h) by source", h3))
    w = f["watcher"]
    fs = w.get("feed_status") or {}
    bad = [f"{v.get('source')}: {v.get('state')} ({str(v.get('why') or '')[:70]})"
           for v in fs.values() if isinstance(v, dict) and v.get("state") not in ("ok",)]
    story.append(Paragraph(
        f"<b>Watcher</b> ({w.get('state')}): {len(w.get('items') or [])} new official items · "
        + (", ".join(f"{k} {n}" for k, n in sorted((w.get("by_source") or {}).items())) or "none")
        + f" · FOMC/rate calendar events stored: {w.get('calendar_events_upcoming', 0)}"
        + (" (FOMC halt gate enabled but has no calendar data)" if not w.get("calendar_events_upcoming") else "")
        + f" · HALT {'ACTIVE' if w.get('halt_active') else 'clear'} · WATCHER_HALT {'SET' if w.get('watcher_halt_flag') else 'clear'}"
        + " · sentiment: n/a (keyword tagger, no sentiment model)", small))
    if bad:
        story.append(Paragraph("Unavailable feeds: " + _esc("; ".join(bad[:6])) + (f" (+{len(bad) - 6} more)" if len(bad) > 6 else ""), small))
    elif not fs:
        story.append(Paragraph("Per-feed status: not recorded yet (recorded from the next Watcher poll).", small))
    items = w.get("items") or []
    if items:
        story.append(_small_table(["Time", "Source", "Headline", "Tags"],
                                  [[rr.ct_label(i["at"]), i["source"], str(i["headline"])[:110],
                                    ", ".join(i.get("tags") or []) or "—"] for i in items[:max_items]],
                                  [0.95 * inch, 0.8 * inch, 4.35 * inch, 1.1 * inch]))
        if len(items) > max_items:
            story.append(Paragraph(f"+{len(items) - max_items} more Watcher items not shown", muted))
    y = f["yolo"]
    tr = y.get("transcripts") or {}
    story.append(Paragraph(
        f"<b>Yolo</b> ({y.get('state')}): YouTube {y.get('youtube_state')} · X {y.get('x_state')}"
        + (" (unavailable: no X API key)" if y.get("x_state") == "missing_key" else "")
        + f" · {len(y.get('videos') or [])} new videos · transcripts "
        + (", ".join(f"{k} {n}" for k, n in sorted(tr.items())) or "none"), small))
    vids = y.get("videos") or []
    if vids:
        story.append(_small_table(
            ["Time", "Channel", "Headline", "Tickers / stance"],
            [[rr.ct_label(v["at"]), v["channel"], str(v["headline"])[:80],
              (", ".join(f"{s['ticker']} {s['stance']} {s['bull']}/{s['bear']}" for s in v.get("stance") or [])
               or ", ".join(v.get("tickers") or []) or "—")
              + ("" if v.get("transcript") == "ok" else f" · transcript {v.get('transcript')}"
                 + (f" ({v['transcript_error']})" if v.get("transcript_error") else ""))]
             for v in vids[:max_items]],
            [0.95 * inch, 1.05 * inch, 3.2 * inch, 2.0 * inch]))
        if len(vids) > max_items:
            story.append(Paragraph(f"+{len(vids) - max_items} more videos not shown", muted))
    e = f["earnings"]
    story.append(Paragraph(
        f"<b>Earnings</b> ({e.get('state')}): {len(e.get('rows') or [])} rows updated"
        + (f" · {_esc(str(e.get('why'))[:110])}" if e.get("why") else ""), small))
    k = f["clerk"]
    story.append(Paragraph(
        f"<b>Congressional logger (House Clerk)</b> ({k.get('state')}): {len(k.get('rows') or [])} new transactions · "
        f"last new transaction {rr.ct_label(k.get('last_new_tx_at'))}"
        + (f" · {_esc(str(k.get('why'))[:110])}" if k.get("why") else ""), small))
    for r in (k.get("rows") or [])[:3]:
        story.append(Paragraph(f"· {_esc(r['member'])} {r['tx']} {_esc(r['ticker'] or r['asset'])} {r['amount']} "
                               f"(tx {r['tx_date']}, logged {rr.ct_label(r['at'])})", small))
    fd = f["fed"]
    def _p(v):
        try:
            return f"{float(v):.1%}"
        except (TypeError, ValueError):
            return "—"
    story.append(Paragraph(
        f"<b>Fed desk</b> ({fd.get('state')}): {fd.get('snapshots', 0)} research snapshots · last {rr.ct_label(fd.get('last_research_at'))}"
        f" · EFFR {fd.get('effr', '—')} · target {fd.get('current_target', '—')} · next meeting {fd.get('next_meeting', '—')}: "
        f"hold {_p(fd.get('p_hold'))} / hike {_p(fd.get('p_hike'))} / cut {_p(fd.get('p_cut'))} (dominant {fd.get('dominant', '—')})"
        + (f" · partial: {_esc(fd.get('partial_error'))}" if fd.get("partial_error") else ""), small))
    tt = f["top_tickers"]
    story.append(Paragraph("<b>Top tickers by mentions</b>: " + (
        ", ".join(f"{t} {n} ({src})" for t, n, src in tt) if tt else "none in window"), small))

    # 2. Why we traded
    story.append(Paragraph("2 · Why we traded (fills in the last 24h, all lanes)", h3))
    tr_all = rr.collect_trades(data_dir, start, end)
    fills = tr_all["fills"]
    if not fills:
        story.append(Paragraph("No trades opened or closed in any lane in this window.", small))
    for t in fills[:max_trades]:
        story.append(_kv_table(trade_rows(t, f), [1.55 * inch, W - 1.55 * inch]))
        story.append(Spacer(1, 3))
    if len(fills) > max_trades:
        story.append(Paragraph(f"+{len(fills) - max_trades} more fills not detailed", muted))
    n_ctx = sum(1 for t in fills if t.get("context") and t["context"].get("source") == "engine")
    n_news = sum(1 for t in fills if rr.has_news_input(t.get("context")))
    if fills:
        story.append(Paragraph(f"{len(fills)} fills · {n_ctx} with engine signal context · {n_news} with a news/research input", small))
    for ev in tr_all["ledger_events"][:4]:
        story.append(Paragraph(
            f"Ledger event (not a trade): {ev['lane']} position #{ev['position_id']} {ev['product']} {ev['side']} "
            f"marked <b>{_esc(ev['status'])}</b> at {rr.ct_label(ev['at'])}", small))

    # 3. Research proposals (Task 16)
    story.append(Paragraph("3 · Research proposals", h3))
    pr = rr.collect_proposals(data_dir, start, end)
    if pr["state"] != "ok":
        story.append(Paragraph(f"Proposals: {pr['state']} · {_esc(pr.get('why'))}", small))
    else:
        cnt = pr["counts"]
        story.append(Paragraph(
            f"Proposed {pr['created_in_window']} new · taken {cnt.get('taken', 0)} · live {cnt.get('live', 0)} · "
            f"expired {cnt.get('expired', 0)} · logged only {cnt.get('logged_only', 0)} · conflicted {cnt.get('conflicted', 0)} · "
            f"taken all-time: {len(pr['taken_all'])} ({pr['taken_closed']} closed, {pr['taken_open']} open), "
            f"realized {_money(pr['taken_realized'], signed=True)}", small))
        rows = []
        for p in pr["rows"][:8]:
            pnl = pr["pnl_by_pid"].get(int(p["id"]))
            rows.append([f"#{p['id']}", p["ticker"], p["side"], p.get("product") or "—",
                         f"{float(p['confidence']):.2f}" + (" auto" if p.get("autonomous") else ""),
                         ", ".join(p.get("families") or []), p["status"]
                         + (f" ({p.get('taken_strategy')})" if p.get("taken_strategy") else ""),
                         (_money(pnl["realized_pnl"], signed=True) if pnl and pnl.get("realized_pnl") is not None
                          else ("open" if pnl else "—")),
                         str(p.get("thesis") or "")[:70]])
        if rows:
            story.append(_small_table(["Id", "Ticker", "Side", "Product", "Conf", "Sources", "Status", "P/L", "Thesis"],
                                      rows, [0.35 * inch, 0.5 * inch, 0.4 * inch, 0.9 * inch, 0.5 * inch, 0.9 * inch,
                                             0.85 * inch, 0.55 * inch, 2.25 * inch]))
            if len(pr["rows"]) > 8:
                story.append(Paragraph(f"+{len(pr['rows']) - 8} more proposals not shown", muted))
        lr = pr.get("last_run") or {}
        if lr:
            srcs = lr.get("sources") or {}
            story.append(Paragraph(
                f"Last generation {rr.ct_label(lr.get('at'))}: {lr.get('raw_ideas', 0)} raw ideas → {lr.get('proposals', 0)} proposals · sources "
                + ", ".join(f"{k} {v.get('state')}" for k, v in srcs.items() if isinstance(v, dict)), muted))
    rb = rr.research_idea_book(data_dir)
    story.append(Paragraph(
        f"<b>research_idea</b> (autonomous high-confidence strategy): {rb['closed']} closed ({rb['wins']} green), "
        f"{rb['open']} open, realized {_money(rb['realized'], signed=True)}", small))



def append_forward_guidance_section(story, *, data_dir: Path, report_day: date, h2, body, muted) -> None:
    """Forward Guidance (Tb 8:58 AM CT 10/3): regimes, continuation odds, playbook, macro, changes, calibration, scorecard."""
    from snowball.forward.model import HORIZONS
    from snowball.forward.report import cell, pct, regime_rows, spct
    from snowball.forward.store import ForwardStore

    small = ParagraphStyle("fgs", parent=body, fontSize=7.5, leading=9.5, spaceAfter=3)
    h3 = ParagraphStyle("fgh3", parent=body, fontName="Helvetica-Bold", fontSize=8.5, leading=11, spaceBefore=6, spaceAfter=2)
    story.append(PageBreak())
    story.append(Paragraph("Forward Guidance", h2))
    path = data_dir / "snowball_forward.db"
    if not path.exists():
        story.append(Paragraph("No forecast stored yet (the first run happens at startup / 7:20 AM CT).", small))
        return
    st = ForwardStore(path)
    try:
        s = st.run((report_day + timedelta(days=1)).isoformat()) or st.run()
        sc = st.scorecard()
    finally:
        st.close()
    if not s:
        story.append(Paragraph("No forecast stored yet.", small))
        return
    story.append(Paragraph(
        f"Forecast {s.get('run_date')} · generated {rr.ct_label(s.get('generated_at'))} · multi-year daily history "
        "(Coinbase for crypto, Yahoo for SPY/QQQ/crude) · cont = P(trend continues: bull closes higher, bear closes "
        "lower, range stays inside its typical move) · rev = 1 − cont · BIP uses BTC's regime.", muted))
    story.append(Paragraph("Regimes and continuation probabilities", h3))
    rows = []
    for r in regime_rows(s, alts=False):
        rows.append([r["market"] + (" / BIP" if r["market"] == "BTC" else ""),
                     *( [r["error"], "", ""] if r.get("error") else [cell(r.get(h)) for h in HORIZONS])])
    story.append(_small_table(["Market", "1 day", "1 week", "1 month"], rows, [1.0 * inch, 2.05 * inch, 2.05 * inch, 2.05 * inch]))
    alts = regime_rows(s, alts=True)
    if alts:
        story.append(Spacer(1, 3))
        story.append(_small_table(["Held spot alt", "1 day", "1 week", "1 month"],
                                  [[r["market"], *([r["error"], "", ""] if r.get("error") else [cell(r.get(h)) for h in HORIZONS])]
                                   for r in alts], [1.0 * inch, 2.05 * inch, 2.05 * inch, 2.05 * inch]))
    stance = {e["market"]: e.get("research_stance_7d") for e in s.get("markets") or [] if e.get("research_stance_7d")}
    if stance:
        story.append(Paragraph("<b>Researchers (video transcripts, last 7 days, bull/bear keyword hits; context only, not in the "
                               "probabilities)</b>: " + _esc(", ".join(f"{k} {v['bull']}/{v['bear']} in {v['videos']} videos"
                                                                         for k, v in sorted(stance.items())[:14])), small))

    story.append(Paragraph("Up-and-down playbook", h3))
    prow = []
    for p in s.get("playbook") or []:
        ev = p.get("ev") or {}
        br = p.get("bracket") or {}
        if ev.get("available"):
            odds = (f"{p.get('side')}: P(+{pct(br.get('target_pct'), 1)} before −{pct(br.get('stop_pct'), 1)}) "
                    f"{pct(ev.get('p_target'))} · break-even {pct(ev.get('break_even_p'))} · EV {spct(ev.get('ev_pct'), 3)}"
                    + (f" (${p['ev_usd_per_contract']:+.2f}/contract)" if p.get("ev_usd_per_contract") is not None else "")
                    + f" · {br.get('stop_rule', '')}")
        elif p.get("p_green_floor_30d") is not None:
            odds = f"P(+{pct(p.get('green_floor_pct'))} green floor within 30d) {pct(p.get('p_green_floor_30d'))}"
        else:
            odds = p.get("detail") or "—"
        if p.get("note"):
            odds += f" · {p['note']}"
        prow.append([p.get("market"), p.get("instrument"), f"{p.get('regime_1w')}/{p.get('regime_1m')}", p.get("action"),
                     odds, "yes" if p.get("meets_auto_rule") else "no", (p.get("gate") or {}).get("action", "")])
    if prow:
        majors = [r for r in prow if "futures" in str(r[1]) or r[0] in ("BTC", "ETH")]
        others = [r for r in prow if r not in majors]
        story.append(_small_table(["Market", "Instrument", "1w/1m", "Playbook", "Odds and EV after fees (1-week horizon)", "P≥70% & EV>0"],
                                  [r[:6] for r in majors], [0.5 * inch, 1.15 * inch, 0.6 * inch, 1.55 * inch, 2.75 * inch, 0.6 * inch]))
        if others:
            story.append(Spacer(1, 3))
            story.append(_small_table(["Alt", "Spot", "1w/1m", "Playbook", "Odds", "Gate"],
                                      [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in others],
                                      [0.5 * inch, 1.15 * inch, 0.6 * inch, 1.55 * inch, 2.75 * inch, 0.6 * inch]))
    gst = s.get("_gate_stats") or {}
    gated = {k: v for k, v in (s.get("spot_gate") or {}).items() if v.get("action") != "normal"}
    story.append(Paragraph(
        "<b>How the playbook trades</b>: bull = longs (spot and futures), bear = futures shorts and spot buys paused "
        "(1w and 1m bear) or halved (one of them bear); spot is never shorted and stays never-sell-red. Spot gate today: "
        + (_esc(", ".join(f"{k} {v['action']}" for k, v in sorted(gated.items()))) or "no gated products")
        + ". Research plays still auto-execute only at P≥70% with positive EV after fees, through every guardrail "
          "(1 contract, 1x, one-index-contract gate, TEK-first, futures stops 8% or $250/contract + $50 liquidation "
          "guard, aged-lot exit, treasury receive-only). The regime is added to research P(success) only for markets "
          "whose out-of-sample 1-week skill is positive"
        + ("; today: " + (", ".join(k for k, v in (s.get("research_input") or {}).items() if v.get("enabled")) or "none qualify (no measured edge)"))
        + "." + (f" Gate counters since this run: {gst}" if gst else ""), small))

    mac = s.get("macro") or {}
    story.append(Paragraph("Macro and upcoming events", h3))
    rt = mac.get("rates") or {}
    fed = mac.get("fed") or {}
    line = (f"13-week T-bill {rt.get('tbill_13w', '—')}% ({float(rt.get('tbill_63d_change') or 0):+.2f} pts over 63 trading days: "
            f"<b>{rt.get('rate_state', '—')}</b>) · VIX {rt.get('vix', '—')}")
    if fed.get("state") == "ok":
        line += (f" · Fed funds {fed.get('target')} (EFFR {fed.get('effr')}) · FedWatch "
                 + "; ".join(f"{m.get('date')}: hold {pct(m.get('p_hold'))} / hike {pct(m.get('p_hike'))} / cut {pct(m.get('p_cut'))}"
                             for m in (fed.get("meetings") or [])[:3]))
    story.append(Paragraph(_esc(line).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"), small))
    ev_rows = [[e["date"], e["time"], e["event"], e.get("detail") or "", f"{e.get('days_away')}d", e.get("source")]
               for e in (mac.get("events") or [])[:12]]
    if ev_rows:
        story.append(_small_table(["Date", "Time", "Event", "Detail", "In", "Source"], ev_rows,
                                  [0.75 * inch, 0.75 * inch, 1.6 * inch, 2.4 * inch, 0.4 * inch, 1.3 * inch]))

    story.append(Paragraph("What changed since the previous forecast", h3))
    ch = s.get("changes") or []
    if not s.get("previous_run_date"):
        story.append(Paragraph("First forecast; nothing to compare yet.", small))
    elif not ch:
        story.append(Paragraph(f"No regime flips or ≥5-point probability moves since {s['previous_run_date']}.", small))
    else:
        story.append(Paragraph(_esc("; ".join(f"{c['market']} {c['horizon']}: {c['change']}" for c in ch[:30])), small))

    story.append(Paragraph("Calibration (out of sample, walk-forward, no look-ahead)", h3))
    cal = s.get("calibration") or {}
    crow = []
    for pool, dd in (cal.get("pooled") or {}).items():
        for hz in HORIZONS:
            v = dd.get(hz) or {}
            if v.get("n"):
                crow.append([pool, hz, f"{v['n']:,} (≈{float(v.get('n_effective') or 0):,.0f} indep.)",
                             f"{v.get('brier_continue')}", f"{v.get('brier_base_continue')}", spct(v.get("skill_continue"), 2),
                             pct(v.get("hit_rate"), 1), f"{pct(v.get('mean_p_continue'), 1)} vs {pct(v.get('observed_continue'), 1)}"])
    if crow:
        story.append(_small_table(["Pool", "Horizon", "Forecasts", "Brier", "Brier base rate", "Skill", "Hit rate", "Mean P vs observed"],
                                  crow, [0.55 * inch, 0.5 * inch, 1.3 * inch, 0.7 * inch, 0.9 * inch, 0.6 * inch, 0.65 * inch, 1.35 * inch]))
    story.append(Paragraph(
        f"Test period from {cal.get('test_from')}; shrinkage and the macro switch were chosen on {cal.get('validation')} only. "
        "Skill = 1 − Brier / Brier of the long-run base rate; ≈ 0 means the regime adds no measurable edge, so the "
        "probabilities stay close to base rates on purpose (nothing inflated).", muted))

    story.append(Paragraph("Live scorecard (graded automatically when each horizon ends)", h3))
    srow = []
    for hz in HORIZONS:
        v = (sc.get("by_horizon") or {}).get(hz)
        srow.append([hz, v["n"], pct(v.get("hit"), 1), f"{float(v['b']):.4f}", f"{float(v['bb']):.4f}", spct(v.get("skill"), 1)]
                    if v else [hz, 0, "—", "—", "—", "not graded yet"])
    story.append(_small_table(["Horizon", "Graded", "Hit rate", "Brier", "Brier base", "Skill"], srow,
                              [0.8 * inch, 0.8 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 1.2 * inch]))
    story.append(Paragraph(f"{sc.get('total', 0)} forecasts stored · {sc.get('pending', 0)} waiting for their horizon to end.", muted))


def build(path: Path, *, sample: bool = False, report_day: date | None = None) -> Path:
    now_ct = datetime.now(CT)
    day = report_day or now_ct.date()
    today = day.isoformat()
    is_today = day == now_ct.date()
    snap = fetch_snap()
    status = snap.get("status") or {}
    risk = snap.get("risk") or {}
    score = snap.get("scorecard") or {}
    positions = snap.get("positions") or []
    fut = fetch_futures(snap)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    leaders = top_trades(con, 10)
    day_start_dt = datetime(day.year, day.month, day.day, tzinfo=CT)
    day_end_dt = day_start_dt + timedelta(days=1)
    day_start = day_start_dt.astimezone(timezone.utc).isoformat()
    day_end = day_end_dt.astimezone(timezone.utc).isoformat()
    today_fills = list(
        con.execute(
            "select product, side, strategy, round(notional_usd,2) notional, reason from fills where ts >= ? and ts < ? order by ts desc limit 20",
            (day_start, day_end),
        )
    )
    closed_day = con.execute(
        "select coalesce(sum(realized_pnl),0) p, count(*) c from positions where status='closed' and closed_at >= ? and closed_at < ?",
        (day_start, day_end),
    ).fetchone()
    day_realized = float(closed_day["p"] or 0)
    book_closed = aggregate_closed_pnl(default_lane_dbs(ROOT / "data"), year=day.year)

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=20, textColor=colors.HexColor("#0f172a"), spaceAfter=4, leading=24)
    sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=10, leading=12)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11, textColor=colors.HexColor("#0f172a"), spaceBefore=16, spaceAfter=8, leading=14)
    body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#334155"), leading=13, spaceAfter=6)
    muted = ParagraphStyle("m", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#94a3b8"), leading=11)
    praise = ParagraphStyle("p", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#0f172a"), leading=14, spaceBefore=4, spaceAfter=8, backColor=colors.HexColor("#ecfdf5"), borderPadding=8)

    if is_today:
        pnl = float(risk.get("daily_pnl_usd") or 0)
    else:
        pnl = day_realized
    big = ParagraphStyle(
        "big",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=26,
        textColor=colors.HexColor("#15803d" if pnl >= 0 else "#b91c1c"),
        leading=30,
        spaceAfter=6,
    )

    story = []
    label = "SAMPLE · " if sample else ""
    story.append(Paragraph("SNOWBALL", title))
    story.append(Paragraph(f"Daily trade summary · {label}{today} · America/Chicago", sub))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceAfter=14))

    story.append(Paragraph("Daily P/L", h2))
    story.append(Paragraph(("+" if pnl >= 0 else "") + f"${pnl:,.2f}", big))
    eq_note = "book as of report" if is_today else "current book (live)"
    story.append(
        Paragraph(
            f"Crypto lane book: equity ${float(risk.get('equity_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"cash ${float(risk.get('cash_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Mode {status.get('mode', '?')}{' · LIVE' if status.get('live_enabled') else ''}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{eq_note}"
            + (f"&nbsp;&nbsp;·&nbsp;&nbsp;Day closed PnL from {int(closed_day['c'] or 0)} exits" if not is_today else ""),
            body,
        )
    )

    # Real money headline (2026-10-10): actual Coinbase CFM + spot balances.
    append_real_balances_section(story, fetch_real_balances_for_report(fut), h2=h2, body=body,
                                 muted=muted, report_day=day)

    ytd_pnl = float(book_closed.ytd)
    all_time_pnl = float(book_closed.all_time)
    spot_all = float(book_closed.spot_all_time)
    cfm_all = float(book_closed.cfm_all_time)
    mid = ParagraphStyle(
        "mid",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        spaceAfter=2,
    )
    mid_ytd = ParagraphStyle(
        "mid_ytd",
        parent=mid,
        textColor=colors.HexColor("#15803d" if ytd_pnl >= 0 else "#b91c1c"),
    )
    mid_all = ParagraphStyle(
        "mid_all",
        parent=mid,
        textColor=colors.HexColor("#15803d" if all_time_pnl >= 0 else "#b91c1c"),
    )
    cap = ParagraphStyle(
        "cap",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#64748b"),
        leading=11,
        spaceAfter=2,
    )
    story.append(Spacer(1, 6))
    story.append(
        Table(
            [
                [
                    Paragraph("YTD live closed P/L", cap),
                    Paragraph("All-time live closed P/L", cap),
                ],
                [
                    Paragraph(_money(ytd_pnl, signed=True), mid_ytd),
                    Paragraph(_money(all_time_pnl, signed=True), mid_all),
                ],
            ],
            colWidths=[3.5 * inch, 3.5 * inch],
            hAlign="LEFT",
        )
    )
    story.append(
        Paragraph(
            f"Live closed realized only (not account balance change) · "
            f"Spot crypto {_money(spot_all, signed=True)} · "
            f"CFM (Futures incl. former Stock lane + crash + fed) {_money(cfm_all, signed=True)} · "
            f"{book_closed.ytd_count} YTD / {book_closed.all_time_count} all-time live exits · "
            f"CT year {book_closed.year} · unrealized not included",
            muted,
        )
    )
    paper_all = float(getattr(book_closed, "paper_all_time", 0.0) or 0.0)
    paper_n = int(getattr(book_closed, "paper_all_time_count", 0) or 0)
    if paper_n or abs(paper_all) > 1e-9:
        story.append(
            Paragraph(
                f"Paper / sim closed excluded from headline: "
                f"{_money(paper_all, signed=True)} across {paper_n} exits",
                muted,
            )
        )
    # YTD average daily profit (beat it every day) + $100 streak toward Tb's top-up (display only)
    try:
        from snowball.reports.ytd_profit import ytd_profit_status

        as_of = min(datetime.now(timezone.utc),
                    datetime.combine(day + timedelta(days=1), datetime.min.time().replace(hour=8), tzinfo=CT))
        ytd = ytd_profit_status(ROOT / "data", as_of)
    except Exception as e:  # noqa: BLE001
        ytd = {"error": f"{type(e).__name__}: {e}"[:160]}
    story.append(Spacer(1, 4))
    for i, line in enumerate(ytd_lines(ytd)):
        story.append(Paragraph(line, body if i in (0, 1, 2) else muted))
    if not ytd.get("error"):
        story.append(ytd_chart(ytd.get("series") or [], minimum=ytd.get("minimum_usd")))
    story.append(Paragraph("Strategies: " + (", ".join(status.get("strategies") or []) or "—"), body))

    # Bitcoin treasury — backstop, separate from CRYPTO trader
    append_bitcoin_treasury_section(
        story,
        data_dir=ROOT / "data",
        h2=h2,
        body=body,
        muted=muted,
        now=now_ct,
    )

    # Future Trader — after crypto summary, before leaderboard (cleanest layout)
    append_future_trader_section(
        story,
        fut=fut,
        day=day,
        is_today=is_today,
        h2=h2,
        body=body,
        muted=muted,
    )

    # Leaderboard
    story.append(Paragraph("ALL-TIME Top 10 profit trades", h2))
    story.append(Paragraph(praise_line(leaders), praise))
    lb_rows = []
    for i, r in enumerate(leaders, 1):
        bot = r["strategy"] or "—"
        lb_rows.append(
            [
                f"#{i}",
                r["product"],
                bot,
                f"+${float(r['pnl']):,.2f}",
                (r["closed_at"] or "")[:10],
            ]
        )
    if not lb_rows:
        lb_rows = [["—", "—", "—", "$0.00", "—"]]
    story.append(
        make_table(
            ["Rank", "Product", "Bot / strategy", "Profit", "Closed"],
            lb_rows,
            [0.6 * inch, 1.4 * inch, 1.6 * inch, 1.2 * inch, 1.1 * inch],
        )
    )
    story.append(Paragraph(
        "Technical strategies only · research_idea trades ranked separately (Research ideas leaderboard).", muted))
    ri_lb = rr.research_idea_book(ROOT / "data")
    story.append(Paragraph("Research ideas leaderboard (research_idea, all lanes)", h2))
    if ri_lb["top"]:
        story.append(make_table(
            ["Rank", "Lane", "Product", "Side", "Profit", "Closed"],
            [[f"#{i}", r["lane"], r["product"], r["side"], _money(r["realized_pnl"], signed=True),
              (r["closed_at"] or "")[:10]] for i, r in enumerate(ri_lb["top"], 1)],
            [0.6 * inch, 0.9 * inch, 1.5 * inch, 0.7 * inch, 1.1 * inch, 1.1 * inch],
        ))
    story.append(Paragraph(
        f"research_idea: {ri_lb['closed']} closed ({ri_lb['wins']} green) · {ri_lb['open']} open · "
        f"realized {_money(ri_lb['realized'], signed=True)}"
        + ("" if ri_lb["rows"] else " · no research_idea trades yet"), body))

    story.append(Paragraph("By strategy (lifetime closed)", h2))
    rows = []
    ri_book = rr.research_idea_book(ROOT / "data")
    for r in score.get("by_strategy") or []:
        if str(r.get("strategy")) == rr.RESEARCH_IDEA:
            continue  # reported separately below (all lanes)
        rows.append(
            [
                r.get("strategy"),
                r.get("closed_trades"),
                f"{float(r.get('win_rate') or 0) * 100:.0f}%",
                f"${float(r.get('realized_pnl') or 0):,.2f}",
                r.get("open_count"),
            ]
        )
    story.append(
        make_table(
            ["Strategy", "Closed", "Win %", "Realized", "Open"],
            rows or [["—", "0", "—", "$0.00", "0"]],
            [1.6 * inch, 1.0 * inch, 1.0 * inch, 1.4 * inch, 0.9 * inch],
        )
    )
    ri_wr = (f"{ri_book['wins'] / ri_book['closed'] * 100:.0f}%" if ri_book["closed"] else "—")
    story.append(Spacer(1, 4))
    story.append(
        make_table(
            ["Research ideas (separate)", "Closed", "Win %", "Realized", "Open"],
            [[rr.RESEARCH_IDEA + " · all lanes", ri_book["closed"], ri_wr,
              _money(ri_book["realized"], signed=True), ri_book["open"]]],
            [1.6 * inch, 1.0 * inch, 1.0 * inch, 1.4 * inch, 0.9 * inch],
        )
    )

    story.append(Paragraph("By product (lifetime closed)", h2))
    rows = []
    for r in score.get("by_product") or []:
        rows.append(
            [
                r.get("product"),
                r.get("closed_trades"),
                f"{float(r.get('win_rate') or 0) * 100:.0f}%",
                f"${float(r.get('realized_pnl') or 0):,.2f}",
                r.get("open_count"),
            ]
        )
    story.append(
        make_table(
            ["Product", "Closed", "Win %", "Realized", "Open"],
            rows or [["—", "0", "—", "$0.00", "0"]],
            [1.6 * inch, 1.0 * inch, 1.0 * inch, 1.4 * inch, 0.9 * inch],
        )
    )

    story.append(Paragraph("Open positions", h2))
    if isinstance(positions, list) and positions:
        rows = []
        for p in positions[:20]:
            if isinstance(p, dict):
                rows.append(
                    [
                        p.get("product") or p.get("symbol") or "—",
                        p.get("strategy") or "—",
                        f"{float(p.get('qty') or 0):.6g}",
                        f"${float(p.get('entry_price') or 0):,.4g}",
                    ]
                )
        story.append(
            make_table(
                ["Product", "Strategy", "Qty", "Entry"],
                rows,
                [1.7 * inch, 1.3 * inch, 1.4 * inch, 1.5 * inch],
            )
        )
    else:
        story.append(Paragraph("None open.", body))

    fills_title = "Today's fills (CT day so far)" if is_today else f"Fills for {today} (CT)"
    story.append(Paragraph(fills_title, h2))
    if today_fills:
        rows = [
            [
                f["product"],
                f["side"],
                f["strategy"] or "—",
                f"${f['notional']:,.0f}",
                (f["reason"] or "")[:22],
            ]
            for f in today_fills[:15]
        ]
        story.append(
            make_table(
                ["Product", "Side", "Strategy", "Notional", "Reason"],
                rows,
                [1.4 * inch, 0.7 * inch, 1.1 * inch, 1.0 * inch, 1.7 * inch],
            )
        )
    else:
        story.append(Paragraph(f"No fills on {today} (CT).", body))

    # Research Report (Task 15/16): last 24h ending at render time (or end of a past CT day)
    if is_today or day == now_ct.date() - timedelta(days=1):
        rr_end = now_ct.astimezone(timezone.utc)
    else:
        rr_end = day_end_dt.astimezone(timezone.utc)
    try:
        append_forward_guidance_section(story, data_dir=ROOT / "data", report_day=day, h2=h2, body=body, muted=muted)
    except Exception as exc:  # noqa: BLE001  (never lose the whole PDF over this section)
        story.append(Paragraph(f"Forward Guidance — section failed: {_esc(str(exc)[:160])}", muted))
    try:
        append_research_learning_section(
            story, data_dir=ROOT / "data", start=rr_end - timedelta(hours=24), end=rr_end,
            h2=h2, body=body, muted=muted,
        )
    except Exception as exc:  # noqa: BLE001  (never lose the whole PDF over this section)
        story.append(Paragraph(f"Research: videos learned today — section failed: {_esc(str(exc)[:160])}", muted))
    append_research_report_section(
        story, data_dir=ROOT / "data", start=rr_end - timedelta(hours=24), end=rr_end,
        h2=h2, body=body, muted=muted,
    )

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceBefore=4, spaceAfter=8))
    story.append(
        Paragraph(
            f"Generated {now_ct.strftime('%Y-%m-%d %H:%M %Z')} · leaderboard auto-updates from closed trades · no secrets",
            muted,
        )
    )
    doc.build(story)
    con.close()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snowball daily trade PDF")
    parser.add_argument(
        "--date",
        help="Report day America/Chicago YYYY-MM-DD (default: prior CT day)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Mark PDF as SAMPLE and use SAMPLE filename suffix",
    )
    parser.add_argument("--out", help="Output PDF path (default reports/daily-<date>[-SAMPLE].pdf)")
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    now_ct = datetime.now(CT)
    if args.date:
        report_day = date.fromisoformat(args.date)
    else:
        report_day = (now_ct.date() - timedelta(days=1))
    suffix = "-SAMPLE" if args.sample else ""
    path = Path(args.out) if args.out else REPORTS / f"daily-{report_day.isoformat()}{suffix}.pdf"
    build(path, sample=args.sample, report_day=report_day)
    print(path)
