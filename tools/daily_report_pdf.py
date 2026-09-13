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
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
ROOT = Path("/home/tb/snowball")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowball.reports.closed_pnl import aggregate_closed_pnl, default_lane_dbs

DB = ROOT / "data" / "snowball.db"
FUT_DB = ROOT / "data" / "snowball_futures.db"
REPORTS = ROOT / "reports"

# Preferred index order for Future Trader section
FT_INDEX_ORDER = ("SPY", "QQQ")


def fetch_snap() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/snapshot", timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def fetch_futures(snap: dict | None = None) -> dict:
    """Prefer dedicated /api/futures; fall back to snapshot key."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/futures", timeout=25) as r:
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
        story.append(Paragraph("Future Trader", h2))
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

    story.append(Paragraph("Future Trader", h2))

    mode_bits = [f"Mode {mode}"]
    if live_flag is not None:
        mode_bits.append("live_enabled=yes" if live_flag else "live_enabled=no")
    if status.get("trading_enabled") is False:
        mode_bits.append("trading paused")
    if status.get("daily_killed"):
        mode_bits.append("daily kill")
    story.append(Paragraph(" · ".join(mode_bits), body))

    book_bits = []
    if cash is not None:
        book_bits.append(f"Cash {_money(cash)}")
    if equity is not None:
        book_bits.append(f"Equity {_money(equity)}")
    if daily_pnl is not None:
        book_bits.append(f"Daily P/L {_money(daily_pnl, signed=True)}")
    if book_bits:
        story.append(Paragraph("&nbsp;&nbsp;·&nbsp;&nbsp;".join(book_bits), body))

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
        story.append(Paragraph("FT by strategy (session vs momentum)", body))
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

    story.append(Spacer(1, 4))
    story.append(
        Paragraph(
            "Rules: session exits at close only if green else hold overnight; "
            "momentum exits on TP/stall/EOD if green; never_sell_red; max 1 lot per index "
            "(session OR momentum — no double-long); FT budget ~30%",
            muted,
        )
    )


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
            f"Equity ${float(risk.get('equity_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Cash ${float(risk.get('cash_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Mode {status.get('mode', '?')}{' · LIVE' if status.get('live_enabled') else ''}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{eq_note}"
            + (f"&nbsp;&nbsp;·&nbsp;&nbsp;Day closed PnL from {int(closed_day['c'] or 0)} exits" if not is_today else ""),
            body,
        )
    )

    ytd_pnl = float(book_closed.ytd)
    all_time_pnl = float(book_closed.all_time)
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
                    Paragraph("YTD closed P/L", cap),
                    Paragraph("All-time closed P/L", cap),
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
            f"Closed realized only · book-wide (crypto + stocks/CFM + futures/FT + crash + fed) "
            f"· {book_closed.ytd_count} YTD / {book_closed.all_time_count} all-time exits "
            f"· CT year {book_closed.year} · unrealized not included",
            muted,
        )
    )
    story.append(Paragraph("Strategies: " + (", ".join(status.get("strategies") or []) or "—"), body))

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

    story.append(Paragraph("By strategy (lifetime closed)", h2))
    rows = []
    for r in score.get("by_strategy") or []:
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
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    now_ct = datetime.now(CT)
    if args.date:
        report_day = date.fromisoformat(args.date)
    else:
        report_day = (now_ct.date() - timedelta(days=1))
    suffix = "-SAMPLE" if args.sample else ""
    path = REPORTS / f"daily-{report_day.isoformat()}{suffix}.pdf"
    build(path, sample=args.sample, report_day=report_day)
    print(path)
