"""Research Report data for the daily PDF (Task 15 + Task 16).

Pure read-only collectors over the lane SQLite files. Nothing here invents a
value: every field comes from a stored row; missing/failed feeds are reported
as ``unavailable``/``error`` with the stored reason; fills without a stored
signal-context snapshot are reported as ``context not recorded``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from snowball.reports.closed_pnl import LANE_DB_FILES

CT = ZoneInfo("America/Chicago")
RESEARCH_IDEA = "research_idea"
MACRO_PRODUCTS = ("US5", "TEK")
PRODUCT_TICKERS: dict[str, tuple[str, ...]] = {
    "US5": ("SPY", "SPX", "ES", "VOO", "IVV"),
    "TEK": ("QQQ", "NDX", "NQ", "TQQQ"),
    "BIP": ("BTC", "XBT", "IBIT"),
    "NOL": ("USO", "OIL", "WTI", "CL"),
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(ts: Any) -> datetime | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ct_label(ts: Any, fmt: str = "%m/%d %I:%M %p") -> str:
    dt = parse_ts(ts)
    if dt is None:
        return "—"
    return dt.astimezone(CT).strftime(fmt).lstrip("0") + " CT"


def _in(ts: Any, start: datetime, end: datetime) -> bool:
    dt = parse_ts(ts)
    return dt is not None and start <= dt < end


def _ro(path: Path) -> sqlite3.Connection | None:
    try:
        if not Path(path).exists():
            return None
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _meta(c: sqlite3.Connection, table: str) -> dict[str, str]:
    try:
        return {str(r[0]): str(r[1]) for r in c.execute(f"SELECT key, value FROM {table}")}
    except sqlite3.Error:
        return {}


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def product_family(product: str | None) -> str:
    p = (product or "").upper()
    for fam in ("US5", "TEK", "BIP", "NOL"):
        if p == fam or p.startswith(fam + "-"):
            return fam
    return p


def product_tickers(product: str | None) -> tuple[str, ...]:
    """Research tickers that refer to a traded product (spot base, or mapped futures family)."""
    fam = product_family(product)
    if fam in PRODUCT_TICKERS:
        return PRODUCT_TICKERS[fam]
    base = (product or "").upper().split("-", 1)[0]
    return (base,) if base else ()


# ---------------------------------------------------------------- findings --


def watcher_findings(main_db: Path, start: datetime, end: datetime, *, halt_dir: Path | None = None) -> dict[str, Any]:
    c = _ro(main_db)
    if c is None:
        return {"state": "unavailable", "why": "snowball.db missing", "items": []}
    out: dict[str, Any] = {"state": "ok", "items": []}
    try:
        rows = c.execute(
            "SELECT source, kind, title, url, published_at, fetched_at, tags, importance FROM research_events "
            "WHERE fetched_at >= ? AND fetched_at < ? ORDER BY coalesce(published_at, fetched_at) DESC",
            (_iso(start), _iso(end))).fetchall()
        for r in rows:
            out["items"].append({
                "source": r["source"], "kind": r["kind"], "headline": r["title"], "url": r["url"],
                "at": r["published_at"] or r["fetched_at"], "tags": _loads(r["tags"], []),
                "importance": r["importance"],
            })
        cal = c.execute(
            "SELECT count(*) FROM research_events WHERE kind='calendar' AND published_at >= ?",
            (_iso(start),)).fetchone()[0]
        out["calendar_events_upcoming"] = int(cal)
        meta = _meta(c, "watcher_meta")
        out["last_poll_at"] = meta.get("last_poll_at")
        out["last_error"] = meta.get("last_error")
        fs = _loads(meta.get("feed_status"), {})
        out["feed_status"] = fs.get("feeds") if isinstance(fs, dict) else None
        out["feed_status_at"] = fs.get("at") if isinstance(fs, dict) else None
        try:
            n_rates = c.execute("SELECT count(*) FROM research_rates").fetchone()[0]
        except sqlite3.Error:
            n_rates = 0
        out["rates_rows"] = int(n_rates)
    except sqlite3.Error as exc:
        out = {"state": "error", "why": str(exc)[:160], "items": []}
    finally:
        c.close()
    out["by_source"] = dict(Counter(i["source"] for i in out["items"]))
    if halt_dir is not None:
        out["halt_active"] = (halt_dir / "HALT").exists()
        flag = halt_dir / "WATCHER_HALT"
        out["watcher_halt_flag"] = flag.exists()
    return out


def yolo_findings(main_db: Path, start: datetime, end: datetime) -> dict[str, Any]:
    c = _ro(main_db)
    if c is None:
        return {"state": "unavailable", "why": "snowball.db missing", "videos": []}
    out: dict[str, Any] = {"state": "ok", "videos": []}
    try:
        meta = _meta(c, "yolo_meta")
        out["source_status"] = meta.get("source_status")
        out["disabled_reason"] = meta.get("disabled_reason") or None
        st = dict(kv.split("=", 1) for kv in (meta.get("source_status") or "").split(",") if "=" in kv)
        out["youtube_state"] = st.get("youtube", "unknown")
        out["x_state"] = st.get("x", "unknown")
        vids = c.execute(
            """SELECT v.video_id, v.channel_handle, v.title, v.url, v.published_at, v.tickers_json,
                      t.status AS t_status, t.error AS t_error, t.ideas_json
               FROM yolo_videos v LEFT JOIN yolo_transcripts t ON t.video_id = v.video_id
               WHERE v.published_at >= ? AND v.published_at < ? ORDER BY v.published_at DESC""",
            (_iso(start)[:19], _iso(end)[:19] + "Z")).fetchall()
        tstat: Counter[str] = Counter()
        for v in vids:
            if not _in(v["published_at"], start, end):
                continue
            ideas = [i for i in _loads(v["ideas_json"], []) if isinstance(i, dict)]
            tick = list(dict.fromkeys([*(_loads(v["tickers_json"], []) or []),
                                       *[str(i.get("ticker") or "") for i in ideas]]))
            tstat[str(v["t_status"] or "pending")] += 1
            err = str(v["t_error"] or "")
            out["videos"].append({
                "channel": v["channel_handle"], "headline": v["title"], "url": v["url"],
                "at": v["published_at"], "tickers": [t for t in tick if t],
                "transcript": v["t_status"] or "pending",
                "transcript_error": err.split(":", 1)[0] if err and err != "None" else None,
                "stance": [{"ticker": i.get("ticker"), "stance": i.get("stance"),
                            "bull": int(i.get("bull") or 0), "bear": int(i.get("bear") or 0)}
                           for i in ideas if (i.get("bull") or i.get("bear"))],
            })
        out["transcripts"] = dict(tstat)
        since_epoch = start.timestamp()
        ment = c.execute(
            "SELECT ticker, source, count(*) n FROM yolo_mentions WHERE created_utc >= ? AND created_utc < ? "
            "GROUP BY ticker, source", (since_epoch, end.timestamp())).fetchall()
        out["mentions"] = [{"ticker": r["ticker"], "source": r["source"], "n": int(r["n"])} for r in ment]
    except sqlite3.Error as exc:
        out = {"state": "error", "why": str(exc)[:160], "videos": []}
    finally:
        c.close()
    return out


def earnings_findings(db: Path, start: datetime, end: datetime) -> dict[str, Any]:
    c = _ro(db)
    if c is None:
        return {"state": "unavailable", "why": "snowball_earnings.db missing", "rows": []}
    try:
        meta = _meta(c, "earnings_meta")
        rows = c.execute(
            "SELECT ticker, report_date, name, session, eps_forecast, post_ret_1d, updated_at "
            "FROM earnings_events WHERE updated_at >= ? AND updated_at < ? ORDER BY report_date",
            (_iso(start), _iso(end))).fetchall()
    except sqlite3.Error as exc:
        return {"state": "error", "why": str(exc)[:160], "rows": []}
    finally:
        c.close()
    err = meta.get("last_error") or ""
    counts = meta.get("last_counts") or ""
    state = "ok"
    if err and not rows:
        state = "unavailable"
    return {"state": state, "why": err[:160] or None, "last_poll_at": meta.get("last_poll_at"),
            "last_counts": counts[:160] or None,
            "rows": [{"ticker": r["ticker"], "report_date": r["report_date"], "name": r["name"],
                      "session": r["session"], "post_ret_1d": r["post_ret_1d"], "at": r["updated_at"]}
                     for r in rows]}


def clerk_findings(db: Path, start: datetime, end: datetime) -> dict[str, Any]:
    c = _ro(db)
    if c is None:
        return {"state": "unavailable", "why": "snowball_clerk.db missing", "rows": []}
    try:
        meta = _meta(c, "clerk_meta")
        rows = c.execute(
            "SELECT member, ticker, asset_name, tx_type, tx_date, amount_range, created_at FROM clerk_transactions "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (_iso(start), _iso(end))).fetchall()
        last_tx = c.execute("SELECT max(created_at) FROM clerk_transactions").fetchone()[0]
    except sqlite3.Error as exc:
        return {"state": "error", "why": str(exc)[:160], "rows": []}
    finally:
        c.close()
    err = meta.get("last_error") or ""
    state = "ok"
    if "pdftotext" in err:
        state = "degraded"
    return {"state": state, "why": (("pdftotext missing in container; PTR PDFs not parsed · " if "pdftotext" in err else "")
                                    + err[:120]) or None,
            "last_poll_at": meta.get("last_poll_at"), "last_new_tx_at": last_tx,
            "rows": [{"member": r["member"], "ticker": r["ticker"], "asset": r["asset_name"], "tx": r["tx_type"],
                      "tx_date": r["tx_date"], "amount": r["amount_range"], "at": r["created_at"]} for r in rows]}


def fed_findings(db: Path, start: datetime, end: datetime) -> dict[str, Any]:
    c = _ro(db)
    if c is None:
        return {"state": "unavailable", "why": "snowball_fed.db missing"}
    try:
        meta = _meta(c, "meta")
        n = c.execute("SELECT count(*) FROM research_snapshots WHERE ts >= ? AND ts < ?",
                      (_iso(start), _iso(end))).fetchone()[0]
    except sqlite3.Error as exc:
        return {"state": "error", "why": str(exc)[:160]}
    finally:
        c.close()
    res = _loads(meta.get("last_research_json"), {})
    meetings = res.get("meetings") or [] if isinstance(res, dict) else []
    nm = meetings[0] if meetings else {}
    at = meta.get("last_research_at")
    err = meta.get("last_research_error") or ""
    state = "ok" if _in(at, start - timedelta(hours=1), end + timedelta(hours=1)) else "stale"
    return {"state": state, "last_research_at": at, "snapshots": int(n),
            "effr": res.get("effr") if isinstance(res, dict) else None,
            "current_target": res.get("current_target") if isinstance(res, dict) else None,
            "next_meeting": nm.get("date"), "p_hold": nm.get("p_hold"), "p_hike": nm.get("p_hike"),
            "p_cut": nm.get("p_cut"), "dominant": nm.get("dominant"), "max_prob": nm.get("max_prob"),
            "partial_error": ("FRED unreachable (DNS)" if "fred" in err.lower() and "resolve" in err.lower()
                              else err[:120]) or None}


def top_tickers(yolo: dict[str, Any], clerk: dict[str, Any], earnings: dict[str, Any], n: int = 8) -> list[tuple[str, int, str]]:
    cnt: Counter[str] = Counter()
    src: dict[str, set[str]] = {}
    for m in yolo.get("mentions") or []:
        t = str(m.get("ticker") or "").upper()
        if t:
            cnt[t] += int(m.get("n") or 0)
            src.setdefault(t, set()).add(f"yolo:{m.get('source')}")
    for v in yolo.get("videos") or []:
        for s in v.get("stance") or []:
            t = str(s.get("ticker") or "").upper()
            if t:
                cnt[t] += int(s.get("bull") or 0) + int(s.get("bear") or 0)
                src.setdefault(t, set()).add("yolo:transcript")
    for r in clerk.get("rows") or []:
        t = str(r.get("ticker") or "").upper()
        if t:
            cnt[t] += 1
            src.setdefault(t, set()).add("clerk")
    for r in earnings.get("rows") or []:
        t = str(r.get("ticker") or "").upper()
        if t:
            cnt[t] += 1
            src.setdefault(t, set()).add("earnings")
    return [(t, c, ", ".join(sorted(src.get(t, ())))) for t, c in cnt.most_common(n)]


def collect_findings(data_dir: Path, start: datetime, end: datetime) -> dict[str, Any]:
    main = data_dir / "snowball.db"
    y = yolo_findings(main, start, end)
    e = earnings_findings(data_dir / "snowball_earnings.db", start, end)
    k = clerk_findings(data_dir / "snowball_clerk.db", start, end)
    return {
        "watcher": watcher_findings(main, start, end, halt_dir=data_dir),
        "yolo": y,
        "earnings": e,
        "clerk": k,
        "fed": fed_findings(data_dir / "snowball_fed.db", start, end),
        "top_tickers": top_tickers(y, k, e),
    }


# ------------------------------------------------------------------ trades --


def _has_col(c: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        return any(str(r[1]) == col for r in c.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def collect_trades(data_dir: Path, start: datetime, end: datetime) -> dict[str, Any]:
    """Every fill in the window across lanes, with its stored context (or none)."""
    fills: list[dict[str, Any]] = []
    ledger_events: list[dict[str, Any]] = []
    for lane, name in LANE_DB_FILES:
        c = _ro(data_dir / name)
        if c is None:
            continue
        try:
            has_ctx = _has_col(c, "fills", "context_json")
            has_pid = _has_col(c, "fills", "proposal_id")
            rows = c.execute(
                "SELECT f.id, f.position_id, f.product, f.side, f.qty, f.price, f.notional_usd, f.ts, f.reason, "
                "f.strategy" + (", f.context_json" if has_ctx else ", NULL AS context_json")
                + (", f.proposal_id" if has_pid else ", NULL AS proposal_id")
                + ", p.side AS pos_side, p.entry_price, p.exit_price, p.realized_pnl, p.status AS pos_status, "
                "p.opened_at, p.closed_at FROM fills f LEFT JOIN positions p ON p.id = f.position_id "
                "WHERE f.ts >= ? AND f.ts < ? ORDER BY f.ts",
                (_iso(start), _iso(end))).fetchall()
            for r in rows:
                ctx = _loads(r["context_json"], None)
                action = (ctx or {}).get("action")
                if not action:
                    o = parse_ts(r["opened_at"])
                    t = parse_ts(r["ts"])
                    action = "entry" if (o and t and abs((t - o).total_seconds()) < 5) else "exit"
                fills.append({
                    "lane": lane, "fill_id": r["id"], "position_id": r["position_id"], "product": r["product"],
                    "side": r["side"], "qty": r["qty"], "price": r["price"], "notional": r["notional_usd"],
                    "ts": r["ts"], "reason": r["reason"], "strategy": r["strategy"], "action": action,
                    "context": ctx if isinstance(ctx, dict) else None, "proposal_id": r["proposal_id"],
                    "pos_side": r["pos_side"], "entry_price": r["entry_price"], "exit_price": r["exit_price"],
                    "realized_pnl": r["realized_pnl"], "pos_status": r["pos_status"],
                })
            ev = c.execute(
                "SELECT id, product, side, status, strategy, opened_at, closed_at FROM positions "
                "WHERE status NOT IN ('open','closed') AND closed_at >= ? AND closed_at < ?",
                (_iso(start), _iso(end))).fetchall()
            for r in ev:
                ledger_events.append({"lane": lane, "position_id": r["id"], "product": r["product"],
                                      "side": r["side"], "status": r["status"], "strategy": r["strategy"],
                                      "at": r["closed_at"]})
        except sqlite3.Error:
            continue
        finally:
            c.close()
    return {"fills": fills, "ledger_events": ledger_events}


def news_influence(ctx: dict[str, Any] | None) -> list[str]:
    """How recorded news/gates influenced the fill (only from the stored snapshot)."""
    if not ctx:
        return []
    out: list[str] = []
    rg = ctx.get("research_gates") or {}
    if rg:
        if rg.get("watcher_halt_flag"):
            out.append(f"Watcher HALT active ({rg.get('watcher_halt_reason') or 'no reason'}) · gated")
        elif "watcher_halt_flag" in rg:
            out.append("Watcher HALT clear · allowed")
        if rg.get("watcher_fomc_gate_enabled"):
            n = rg.get("fomc_calendar_events_72h")
            nxt = rg.get("fomc_next") or {}
            if nxt:
                out.append(f"FOMC calendar: {nxt.get('title')} ({ct_label(nxt.get('at'))}) · outside halt window · allowed")
            elif n == 0:
                out.append("FOMC gate on; no FOMC/rate calendar event in next 72h · allowed")
    r = ctx.get("research")
    if isinstance(r, dict) and r:
        src = r.get("source") or r.get("input") or "research"
        head = r.get("headline") or r.get("summary") or ""
        if not head and r.get("dominant"):
            try:
                mp = f"{float(r.get('max_prob')):.1%}"
            except (TypeError, ValueError):
                mp = str(r.get("max_prob"))
            head = f"next meeting {r.get('meeting')}: {r.get('dominant')} {mp}"
        when = f" ({ct_label(r.get('at'))})" if r.get("at") else ""
        out.append(f"{src}: {head}{when} · {r.get('effect', 'input')}")
    p = ctx.get("proposal")
    if isinstance(p, dict) and p:
        srcs = "; ".join(f"{s.get('source')}: {str(s.get('headline') or '')[:70]} ({ct_label(s.get('at'))})"
                         for s in (p.get("sources") or [])[:3])
        out.append(f"Proposal #{p.get('id')} {p.get('ticker')} {p.get('side')} conf {float(p.get('confidence') or 0):.2f} "
                   f"({'autonomous' if p.get('autonomous') else 'confirmed by technical entry'}) · {p.get('thesis', '')[:120]}"
                   + (f" · sources: {srcs}" if srcs else ""))
    return out


def has_news_input(ctx: dict[str, Any] | None) -> bool:
    if not ctx:
        return False
    rg = ctx.get("research_gates") or {}
    return bool(ctx.get("research") or ctx.get("proposal") or rg.get("watcher_halt_flag") or rg.get("fomc_next"))


def related_news(findings: dict[str, Any], product: str | None, at: Any, hours: float = 24.0, limit: int = 3) -> list[str]:
    """News on the ticker in the prior 24h (context, not a trigger)."""
    t_at = parse_ts(at) or datetime.now(timezone.utc)
    lo = t_at - timedelta(hours=hours)
    ticks = set(product_tickers(product))
    out: list[str] = []
    for v in (findings.get("yolo") or {}).get("videos") or []:
        if not _in(v.get("at"), lo, t_at + timedelta(seconds=1)):
            continue
        if ticks & {str(x).upper() for x in v.get("tickers") or []}:
            st = [f"{s['ticker']} {s['stance']}" for s in v.get("stance") or [] if str(s.get("ticker")).upper() in ticks]
            out.append(f"Yolo @{v.get('channel')}: {str(v.get('headline'))[:80]} ({ct_label(v.get('at'))})"
                       + (f" · {', '.join(st)}" if st else ""))
    if product_family(product) in MACRO_PRODUCTS:
        for i in (findings.get("watcher") or {}).get("items") or []:
            if i.get("source") in ("fed", "cme_fedwatch") and _in(i.get("at"), lo, t_at + timedelta(seconds=1)):
                out.append(f"Watcher {i.get('source')}: {str(i.get('headline'))[:80]} ({ct_label(i.get('at'))})")
    return out[:limit]


# --------------------------------------------------------------- proposals --


def collect_proposals(data_dir: Path, start: datetime, end: datetime) -> dict[str, Any]:
    path = data_dir / "snowball_proposals.db"
    c = _ro(path)
    if c is None:
        return {"state": "off", "why": "RESEARCH_PROPOSALS_ENABLED off or no proposals DB yet", "rows": []}
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM research_proposals WHERE (created_at >= ? AND created_at < ?) "
            "OR (updated_at >= ? AND updated_at < ?) OR (taken_at >= ? AND taken_at < ?) "
            "ORDER BY confidence DESC, created_at DESC", (_iso(start), _iso(end)) * 3)]
        taken_all = [dict(r) for r in c.execute(
            "SELECT * FROM research_proposals WHERE taken_fill_id IS NOT NULL ORDER BY taken_at")]
        meta = _meta(c, "research_proposal_meta")
    except sqlite3.Error as exc:
        return {"state": "error", "why": str(exc)[:160], "rows": []}
    finally:
        c.close()
    for r in rows + taken_all:
        r["sources"] = _loads(r.pop("sources_json", "[]"), [])
        r["families"] = _loads(r.pop("families_json", "[]"), [])
    lane_db = dict(LANE_DB_FILES)
    pnl_by_pid: dict[int, dict[str, Any]] = {}
    for r in taken_all:
        db = lane_db.get(str(r.get("taken_lane") or ""))
        if not db or r.get("taken_position_id") is None:
            continue
        pc = _ro(data_dir / db)
        if pc is None:
            continue
        try:
            p = pc.execute("SELECT status, realized_pnl, entry_price, exit_price, closed_at FROM positions WHERE id=?",
                           (int(r["taken_position_id"]),)).fetchone()
            if p is not None:
                pnl_by_pid[int(r["id"])] = dict(p)
        except sqlite3.Error:
            pass
        finally:
            pc.close()
    created_in = [r for r in rows if _in(r.get("created_at"), start, end)]
    counts = Counter(str(r.get("status")) for r in rows)
    taken_closed = [pnl_by_pid[i] for i in pnl_by_pid if pnl_by_pid[i].get("status") == "closed"]
    return {
        "state": "ok", "rows": rows, "created_in_window": len(created_in), "counts": dict(counts),
        "taken_all": taken_all, "pnl_by_pid": pnl_by_pid,
        "taken_realized": round(sum(float(p.get("realized_pnl") or 0) for p in taken_closed), 4),
        "taken_closed": len(taken_closed),
        "taken_open": sum(1 for p in pnl_by_pid.values() if p.get("status") == "open"),
        "last_run": _loads(meta.get("last_run"), None),
    }


def research_idea_book(data_dir: Path) -> dict[str, Any]:
    """research_idea lots across all lanes (separate leaderboard)."""
    rows: list[dict[str, Any]] = []
    for lane, name in LANE_DB_FILES:
        c = _ro(data_dir / name)
        if c is None:
            continue
        try:
            for r in c.execute(
                "SELECT id, product, side, status, entry_price, exit_price, realized_pnl, opened_at, closed_at "
                "FROM positions WHERE strategy = ?", (RESEARCH_IDEA,)):
                d = dict(r)
                d["lane"] = lane
                rows.append(d)
        except sqlite3.Error:
            pass
        finally:
            c.close()
    closed = [r for r in rows if r["status"] == "closed" and r["realized_pnl"] is not None]
    wins = sum(1 for r in closed if float(r["realized_pnl"]) > 0)
    return {
        "rows": rows,
        "closed": len(closed),
        "open": sum(1 for r in rows if r["status"] == "open"),
        "wins": wins,
        "realized": round(sum(float(r["realized_pnl"]) for r in closed), 4),
        "top": sorted(closed, key=lambda r: float(r["realized_pnl"]), reverse=True)[:5],
    }


# ---------------------------------------------------- videos learned today --

METHOD_LABEL = {None: "captions (YouTube API)", "": "captions (YouTube API)", "api": "captions (YouTube API)",
                "ytdlp": "captions (yt-dlp)", "whisper": "Whisper (audio transcription)"}


def videos_learned(main_db: Path, start: datetime, end: datetime, *, fresh_hours: float = 48.0,
                   with_notes: bool = True) -> dict[str, Any]:
    """Every video whose transcript was stored (status ok) in the window, with extractive notes."""
    from snowball.research.notes import extract_notes, stance_line

    c = _ro(main_db)
    if c is None:
        return {"state": "unavailable", "why": "snowball.db missing", "videos": []}
    try:
        cols = {str(r[1]) for r in c.execute("PRAGMA table_info(yolo_transcripts)")}
        mcol = "t.method" if "method" in cols else "NULL"
        dcol = "t.duration_sec" if "duration_sec" in cols else "NULL"
        rows = c.execute(
            f"""SELECT t.video_id, t.channel_handle, t.fetched_at, t.chars,
                       {"t.text" if with_notes else "NULL"} AS text, t.ideas_json,
                       {mcol} AS method, {dcol} AS duration_sec, v.title, v.url, v.published_at
                FROM yolo_transcripts t LEFT JOIN yolo_videos v ON v.video_id = t.video_id
                WHERE t.status='ok' AND t.fetched_at >= ? AND t.fetched_at < ?""",
            (_iso(start), _iso(end))).fetchall()
        pending = c.execute("SELECT count(*) FROM yolo_transcripts WHERE status != 'ok'").fetchone()[0]
    except sqlite3.Error as exc:
        return {"state": "error", "why": str(exc)[:160], "videos": []}
    finally:
        c.close()
    vids: list[dict[str, Any]] = []
    for r in rows:
        ideas = [i for i in _loads(r["ideas_json"], []) if isinstance(i, dict)]
        pub = parse_ts(r["published_at"])
        fresh = pub is not None and pub >= end - timedelta(hours=fresh_hours)
        vids.append({
            "video_id": r["video_id"], "channel": r["channel_handle"], "title": r["title"] or "",
            "url": r["url"], "published_at": r["published_at"], "learned_at": r["fetched_at"],
            "source": METHOD_LABEL.get(r["method"], str(r["method"])), "method": r["method"] or "api",
            "chars": int(r["chars"] or 0), "duration_min": (round(float(r["duration_sec"]) / 60.0, 1)
                                                           if r["duration_sec"] else None),
            "fresh": fresh, "stance": stance_line(ideas),
            "notes": extract_notes(r["text"] or "", ideas) if with_notes else [],
        })
    vids.sort(key=lambda v: (not v["fresh"], -(parse_ts(v["published_at"]) or start).timestamp()))
    by_ch = Counter(v["channel"] for v in vids)
    by_src = Counter(v["source"] for v in vids)
    return {"state": "ok", "videos": vids, "by_channel": dict(by_ch), "by_source": dict(by_src),
            "fresh": sum(1 for v in vids if v["fresh"]), "backfill": sum(1 for v in vids if not v["fresh"]),
            "not_ok_total": int(pending)}


# ------------------------------------------------------- prospective plays --


def play_status(p: dict[str, Any], min_prob: float) -> str:
    """Executed / waiting below threshold / blocked (which guardrail) — from stored fields only."""
    st = str(p.get("status") or "")
    if st == "taken" or p.get("taken_fill_id") is not None:
        return (f"EXECUTED {ct_label(p.get('taken_at'))} ({p.get('taken_lane')} {p.get('taken_strategy')}, "
                f"fill #{p.get('taken_fill_id')})")
    es = p.get("exec_state")
    why = str(p.get("exec_reason") or p.get("status_reason") or "")
    prefix = "expired · " if st == "expired" else ""
    if es is None:
        return prefix + "not scored (created before probability scoring)"
    if es == "eligible":
        if p.get("engine_block"):
            return prefix + f"eligible (≥{min_prob:.0%}) · blocked by guardrail: {p['engine_block']}"
        return prefix + f"eligible (≥{min_prob:.0%}) · enters on the next lane tick if every guardrail passes"
    if es == "below_threshold":
        return prefix + f"waiting: below {min_prob:.0%} (logged only)"
    return prefix + f"blocked: {why}"


def prospective_plays(data_dir: Path, start: datetime, end: datetime, *, min_prob: float = 0.70) -> dict[str, Any]:
    pr = collect_proposals(data_dir, start, end)
    if pr.get("state") != "ok":
        return {**pr, "plays": []}
    c = _ro(data_dir / "snowball_proposals.db")
    track = None
    if c is not None:
        try:
            track = _loads(_meta(c, "research_proposal_meta").get("track_record"), None)
        finally:
            c.close()
    plays = []
    for p in pr["rows"]:
        prob = _loads(p.get("prob_json"), {}) or {}
        br = prob.get("bracket") or {}
        vids = [s for s in p.get("sources") or [] if isinstance(s, dict)]
        src = "; ".join(f"{s.get('source')}: {str(s.get('headline') or '')[:80]}" for s in vids[:3])
        plays.append({
            "id": p["id"], "ticker": p["ticker"], "product": p.get("product"), "lane": p.get("lane"),
            "side": p["side"], "thesis": p.get("thesis"), "sources": src, "families": p.get("families"),
            "probability": p.get("probability"), "p_base": prob.get("p_base"), "p_tech": prob.get("p_tech"),
            "research_delta": prob.get("research_delta"), "break_even_p": prob.get("break_even_p"),
            "ev_pct": prob.get("ev_pct"), "prob_why": prob.get("why"), "symbol": prob.get("symbol"),
            "target_pct": br.get("target_pct"), "stop_pct": br.get("stop_pct"), "stop_rule": br.get("stop_rule"),
            "kind": br.get("kind"), "mark": br.get("mark"), "base_n": prob.get("base_n"),
            "n_effective": prob.get("n_effective"), "tech": prob.get("tech"),
            "status": play_status(p, min_prob), "exec_state": p.get("exec_state"), "raw_status": p.get("status"),
            "created_at": p.get("created_at"), "confidence": p.get("confidence"),
        })
    plays.sort(key=lambda x: (x["raw_status"] != "taken", -(x["probability"] or -1)))
    return {"state": "ok", "plays": plays, "track_record": track, "counts": pr.get("counts"),
            "last_run": pr.get("last_run"), "min_prob": min_prob}


def research_digest(data_dir: Path, now: datetime | None = None, *, min_prob: float = 0.70,
                    max_videos: int = 25) -> dict[str, Any]:
    """Compact snapshot/status mirror of the PDF research section (last 24h)."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    v = videos_learned(data_dir / "snowball.db", start, end, with_notes=False)
    pl = prospective_plays(data_dir, start, end, min_prob=min_prob)
    return {
        "window_start": _iso(start), "window_end": _iso(end),
        "videos_learned_24h": len(v.get("videos") or []), "videos_fresh": v.get("fresh"),
        "videos_backfill": v.get("backfill"), "by_source": v.get("by_source"), "by_channel": v.get("by_channel"),
        "videos": [{k: x[k] for k in ("channel", "title", "published_at", "learned_at", "source", "stance")}
                   for x in (v.get("videos") or [])[:max_videos]],
        "plays": [{k: x[k] for k in ("id", "ticker", "product", "side", "probability", "break_even_p", "ev_pct",
                                     "target_pct", "stop_pct", "status", "exec_state")}
                  for x in (pl.get("plays") or [])[:30]],
        "exec_min_prob": min_prob,
    }
