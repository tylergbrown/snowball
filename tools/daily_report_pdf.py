#!/usr/bin/env python3
"""Snowball daily trade PDF — professional layout + top-10 leaderboard."""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

CT = ZoneInfo("America/Chicago")
ROOT = Path("/home/tb/snowball")
DB = ROOT / "data" / "snowball.db"
REPORTS = ROOT / "reports"


def fetch_snap() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/snapshot", timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


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


def build(path: Path, *, sample: bool = False) -> Path:
    now_ct = datetime.now(CT)
    today = now_ct.date().isoformat()
    snap = fetch_snap()
    status = snap.get("status") or {}
    risk = snap.get("risk") or {}
    score = snap.get("scorecard") or {}
    positions = snap.get("positions") or []

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    leaders = top_trades(con, 10)
    day_start = datetime(now_ct.year, now_ct.month, now_ct.day, tzinfo=CT).astimezone(timezone.utc).isoformat()
    today_fills = list(
        con.execute(
            "select product, side, strategy, round(notional_usd,2) notional, reason from fills where ts >= ? order by ts desc limit 20",
            (day_start,),
        )
    )

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

    pnl = float(risk.get("daily_pnl_usd") or 0)
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
    story.append(
        Paragraph(
            f"Equity ${float(risk.get('equity_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Cash ${float(risk.get('cash_usd') or 0):,.2f}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Mode {status.get('mode', '?')}{' · LIVE' if status.get('live_enabled') else ''}",
            body,
        )
    )
    story.append(Paragraph("Strategies: " + (", ".join(status.get("strategies") or []) or "—"), body))

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

    story.append(Paragraph("Today's fills (CT day so far)", h2))
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
        story.append(Paragraph("No fills yet today (CT).", body))

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceBefore=4, spaceAfter=8))
    story.append(
        Paragraph(
            f"Generated {now_ct.strftime('%Y-%m-%d %H:%M %Z')} · leaderboard auto-updates from closed trades · no secrets",
            muted,
        )
    )
    doc.build(story)
    return path


if __name__ == "__main__":
    REPORTS.mkdir(parents=True, exist_ok=True)
    today = datetime.now(CT).date().isoformat()
    path = REPORTS / f"daily-{today}-SAMPLE.pdf"
    build(path, sample=True)
    print(path)
