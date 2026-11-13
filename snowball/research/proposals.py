"""Research trade proposals (Task 16).

Research sidecars (Yolo Demon, The Watcher, Earnings scout, Clerk congressional
logger, Fed desk research) emit *proposals* here. A proposal never sizes or
places an order by itself:

* Low-confidence proposals only get linked (``fills.proposal_id``) when an
  existing technical strategy in that lane opens a same-direction entry on the
  mapped product while the proposal is live.
* High-confidence proposals (``confidence >= RESEARCH_MIN_CONFIDENCE`` AND
  (>= 2 agreeing source families OR one official source: watcher/fed)) are
  flagged ``autonomous``; the ``research_idea`` strategy may enter on them,
  through every existing gate/cap (see engines).

Only data the bot really has is used; a source with nothing directional emits
nothing (the Watcher's keyword tagger has no sentiment, so it emits no
direction; it still gates via HALT).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("snowball.research")

CT = ZoneInfo("America/Chicago")
OFFICIAL_FAMILIES = frozenset({"watcher", "fed"})
FAMILY_LABEL = {
    "yolo_youtube": "Yolo Demon · YouTube transcripts",
    "yolo_x": "Yolo Demon · X",
    "watcher": "The Watcher (official RSS/FedWatch)",
    "earnings": "Earnings scout",
    "clerk": "Congressional logger (House Clerk)",
    "fed": "Fed desk (CME FedWatch)",
}
# Index / macro / oil proxies -> futures families
INDEX_US5 = frozenset({"SPY", "SPX", "ES", "VOO", "IVV", "US500", "SPXL", "UPRO"})
INDEX_TEK = frozenset({"QQQ", "NDX", "NQ", "TQQQ", "QLD", "US100"})
OIL = frozenset({"USO", "OIL", "WTI", "CL", "BNO", "UCO", "CRUDE"})
BTC_TICKERS = frozenset({"BTC", "XBT", "IBIT"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def family_of(product: str) -> str:
    return str(product or "").upper().split("-", 1)[0]


@dataclass
class RawIdea:
    family: str
    ticker: str
    side: str  # long | short
    confidence: float
    thesis: str
    headline: str = ""
    source: str = ""
    url: str | None = None
    at: str | None = None
    ref: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_source(self) -> dict[str, Any]:
        d = {"family": self.family, "source": self.source or FAMILY_LABEL.get(self.family, self.family),
             "headline": self.headline[:160], "url": self.url, "at": self.at,
             "confidence": round(float(self.confidence), 3), "ref": self.ref}
        d.update(self.extra)
        return d


# ----------------------------------------------------------------- mapping --


def map_ticker(ticker: str, *, crypto_products: list[str], futures_products: list[str],
               futures_short_products: list[str], side: str) -> dict[str, Any]:
    """Map a research ticker to (lane, product) or explain why it is log-only."""
    t = str(ticker or "").upper().lstrip("$")
    fut_by_family = {family_of(p): p for p in futures_products}
    short_fams = {family_of(p) for p in futures_short_products}

    def fut(fam: str, why: str) -> dict[str, Any]:
        prod = fut_by_family.get(fam)
        if prod is None:
            return {"lane": None, "product": None, "tradable": False,
                    "why": f"{why}; no {fam} product configured in futures lane"}
        if side == "short" and fam not in short_fams:
            return {"lane": "futures", "product": prod, "tradable": False,
                    "why": f"{why}; shorts not permitted on {fam} (FUTURES_SHORT_PRODUCTS)"}
        return {"lane": "futures", "product": prod, "tradable": True, "why": why}

    if t in BTC_TICKERS:
        return fut("BIP", "BTC → BIP (nano BTC future)")
    if t in INDEX_US5:
        return fut("US5", f"{t} → US5 (S&P proxy)")
    if t in INDEX_TEK:
        return fut("TEK", f"{t} → TEK (Nasdaq proxy)")
    if t in OIL:
        return fut("NOL", f"{t} → NOL (crude oil)")
    spot = f"{t}-USD"
    if spot in set(crypto_products):
        if side != "long":
            return {"lane": "crypto", "product": spot, "tradable": False,
                    "why": "crypto spot lane is long-only"}
        return {"lane": "crypto", "product": spot, "tradable": True, "why": f"{t} → {spot} spot"}
    return {"lane": None, "product": None, "tradable": False,
            "why": "stock/other ticker with no tradable product (logged only)"}


def macro_targets(side: str, **kw: Any) -> list[dict[str, Any]]:
    return [map_ticker("SPY", side=side, **kw), map_ticker("QQQ", side=side, **kw)]


# ------------------------------------------------------------------- store --


class ProposalStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ct_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                lane TEXT,
                product TEXT,
                side TEXT NOT NULL,
                thesis TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                families_json TEXT NOT NULL DEFAULT '[]',
                n_families INTEGER NOT NULL DEFAULT 1,
                official INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL,
                autonomous INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                status_reason TEXT,
                taken_at TEXT,
                taken_lane TEXT,
                taken_strategy TEXT,
                taken_fill_id INTEGER,
                taken_position_id INTEGER,
                taken_price REAL,
                fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_prop_live ON research_proposals(status, product, side);
            CREATE TABLE IF NOT EXISTS research_proposal_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        # Probability of success + execution state (Tb 3:45 AM CT 10/3). Additive columns only.
        cols = {str(r[1]) for r in self._conn.execute("PRAGMA table_info(research_proposals)")}
        for col, decl in (("probability", "REAL"), ("prob_json", "TEXT"), ("exec_state", "TEXT"),
                          ("exec_reason", "TEXT"), ("idea_refs", "TEXT"), ("engine_block", "TEXT"),
                          ("engine_block_at", "TEXT")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE research_proposals ADD COLUMN {col} {decl}")
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO research_proposal_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT value FROM research_proposal_meta WHERE key=?", (key,)).fetchone()
            return None if r is None else str(r[0])

    def upsert(self, p: dict[str, Any], now: datetime) -> tuple[int, bool]:
        """Insert or refresh one proposal (fingerprint = ct_date|ticker|product|side)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, status FROM research_proposals WHERE fingerprint=?", (p["fingerprint"],)
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    """INSERT INTO research_proposals
                       (created_at, updated_at, expires_at, ct_date, ticker, lane, product, side, thesis,
                        sources_json, families_json, n_families, official, confidence, autonomous,
                        status, status_reason, fingerprint, probability, prob_json, exec_state,
                        exec_reason, idea_refs)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_iso(now), _iso(now), p["expires_at"], p["ct_date"], p["ticker"], p.get("lane"),
                     p.get("product"), p["side"], p["thesis"], json.dumps(p["sources"], default=str),
                     json.dumps(p["families"]), int(p["n_families"]), int(bool(p["official"])),
                     float(p["confidence"]), int(bool(p["autonomous"])), p["status"],
                     p.get("status_reason"), p["fingerprint"], p.get("probability"),
                     None if p.get("prob") is None else json.dumps(p["prob"], default=str),
                     p.get("exec_state"), p.get("exec_reason"), json.dumps(sorted(p.get("idea_refs") or []))),
                )
                self._conn.commit()
                return int(cur.lastrowid), True
            pid, status = int(row[0]), str(row[1])
            if status in ("live", "logged_only", "conflicted"):
                self._conn.execute(
                    """UPDATE research_proposals SET updated_at=?, thesis=?, sources_json=?, families_json=?,
                       n_families=?, official=?, confidence=?, autonomous=?, status=?, status_reason=?,
                       lane=?, product=?, probability=?, prob_json=?, exec_state=?, exec_reason=?,
                       idea_refs=? WHERE id=?""",
                    (_iso(now), p["thesis"], json.dumps(p["sources"], default=str), json.dumps(p["families"]),
                     int(p["n_families"]), int(bool(p["official"])), float(p["confidence"]),
                     int(bool(p["autonomous"])), p["status"], p.get("status_reason"),
                     p.get("lane"), p.get("product"), p.get("probability"),
                     None if p.get("prob") is None else json.dumps(p["prob"], default=str),
                     p.get("exec_state"), p.get("exec_reason"),
                     json.dumps(sorted(p.get("idea_refs") or [])), pid),
                )
                self._conn.commit()
            return pid, False

    def note_engine_block(self, pid: int, reason: str, now: datetime | None = None) -> None:
        """Last lane-guardrail block for an eligible proposal (shown in the PDF)."""
        now = now or utcnow()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE research_proposals SET engine_block=?, engine_block_at=? WHERE id=? AND status='live' "
                "AND (engine_block IS NULL OR engine_block != ? OR engine_block_at < ?)",
                (str(reason)[:160], _iso(now), int(pid), str(reason)[:160], _iso(now - timedelta(minutes=10))))
            if cur.rowcount:
                self._conn.commit()

    def taken_refs(self, *, ticker: str, side: str, since: datetime) -> set[str]:
        """Idea refs (videos / filings / meetings) already executed for this ticker+side."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT idea_refs FROM research_proposals WHERE taken_fill_id IS NOT NULL AND ticker=? "
                "AND side=? AND coalesce(taken_at, created_at) >= ?", (ticker, side, _iso(since))).fetchall()
        out: set[str] = set()
        for (raw,) in rows:
            try:
                out.update(str(x) for x in json.loads(raw or "[]"))
            except ValueError:
                continue
        return out

    def taken_any(self, *, ticker: str, side: str, since: datetime) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM research_proposals WHERE taken_fill_id IS NOT NULL AND ticker=? AND side=? "
                "AND coalesce(taken_at, created_at) >= ? LIMIT 1", (ticker, side, _iso(since))).fetchone()
        return r is not None

    def expire(self, now: datetime) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE research_proposals SET status='expired', status_reason=coalesce(status_reason,'') || "
                "CASE WHEN status_reason IS NULL OR status_reason='' THEN 'ttl' ELSE ' · ttl' END, updated_at=? "
                "WHERE status IN ('live','conflicted') AND expires_at <= ?",
                (_iso(now), _iso(now)),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def _row(self, r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("sources_json", "families_json"):
            try:
                d[k[:-5]] = json.loads(d.pop(k) or "[]")
            except ValueError:
                d[k[:-5]] = []
        return d

    def get(self, pid: int) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM research_proposals WHERE id=?", (int(pid),)).fetchone())

    def match_live(self, *, product: str, side: str, lane: str | None = None,
                   now: datetime | None = None, autonomous_only: bool = False) -> dict[str, Any] | None:
        """Best live same-direction proposal for this product family (for linking/entry)."""
        now = now or utcnow()
        fam = family_of(product)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM research_proposals WHERE status='live' AND side=? AND expires_at > ? "
                + ("AND autonomous=1 " if autonomous_only else "")
                + "ORDER BY coalesce(probability, 0) DESC, confidence DESC, id ASC",
                (side, _iso(now)),
            ).fetchall()
        for r in rows:
            d = self._row(r)
            if not d or not d.get("product"):
                continue
            if lane and d.get("lane") and d["lane"] != lane:
                continue
            if d["product"] == product or family_of(d["product"]) == fam:
                return {"id": d["id"], "ticker": d["ticker"], "side": d["side"], "thesis": d["thesis"],
                        "confidence": d["confidence"], "autonomous": bool(d["autonomous"]),
                        "probability": d.get("probability"), "exec_state": d.get("exec_state"),
                        "families": d["families"], "sources": d["sources"][:6],
                        "created_at": d["created_at"], "expires_at": d["expires_at"]}
        return None

    def thesis_state(self, pid: int | None, *, product: str, side: str,
                     now: datetime | None = None) -> str:
        """'live' | 'expired' | 'invalidated' (opposite live proposal) | 'unknown'."""
        now = now or utcnow()
        opp = "short" if side == "long" else "long"
        if self.match_live(product=product, side=opp, now=now) is not None:
            return "invalidated"
        fam = family_of(product)
        with self._lock:
            opp_rows = self._conn.execute(
                "SELECT product FROM research_proposals WHERE status='conflicted' AND side=? AND expires_at > ?",
                (opp, _iso(now))).fetchall()
        if any(r[0] and (r[0] == product or family_of(r[0]) == fam) for r in opp_rows):
            return "invalidated"  # an opposite-direction view is now live (sources disagree)
        if pid is None:
            return "unknown"
        p = self.get(pid)
        if p is None:
            return "unknown"
        if p.get("status") == "conflicted":
            return "invalidated"  # sources now disagree on direction
        if p.get("status") == "expired":
            return "expired"
        exp = _parse(p.get("expires_at"))
        if exp is not None and exp <= now:
            return "expired"
        return "live"

    def mark_taken(self, pid: int, *, lane: str, fill_id: int, position_id: Any,
                   strategy: str, price: Any, ts: Any) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE research_proposals SET status='taken', taken_at=?, taken_lane=?, taken_strategy=?,
                   taken_fill_id=?, taken_position_id=?, taken_price=?, updated_at=?
                   WHERE id=? AND status='live'""",
                (str(ts or _iso(utcnow())), lane, strategy, int(fill_id),
                 None if position_id is None else int(position_id),
                 None if price is None else float(price), _iso(utcnow()), int(pid)),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM research_proposals WHERE (created_at >= ? AND created_at < ?) "
                "OR (updated_at >= ? AND updated_at < ?) OR (taken_at >= ? AND taken_at < ?) "
                "ORDER BY created_at DESC",
                (_iso(start), _iso(end)) * 3,
            ).fetchall()
            return [self._row(r) for r in rows if r is not None]

    def all_taken(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self._conn.execute(
                "SELECT * FROM research_proposals WHERE taken_fill_id IS NOT NULL ORDER BY taken_at").fetchall()]


# -------------------------------------------------------------- generators --


def _ro(path: Path) -> sqlite3.Connection | None:
    try:
        if not Path(path).exists():
            return None
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def yolo_transcript_ideas(db: Path, now: datetime, hours: float = 24.0) -> tuple[list[RawIdea], dict[str, Any]]:
    """Per-ticker stance from Yolo transcripts of videos *published* in the window.

    Keyed on the video's publish time (not transcript fetch time) so backfilled
    or retried transcripts of old videos never become fresh proposals.
    """
    status: dict[str, Any] = {"family": "yolo_youtube", "state": "ok"}
    c = _ro(db)
    if c is None:
        return [], {"family": "yolo_youtube", "state": "unavailable", "why": "snowball.db missing"}
    since = _iso(now - timedelta(hours=hours))
    agg: dict[str, dict[str, Any]] = {}
    try:
        rows = c.execute(
            """SELECT t.video_id, t.channel_handle, t.ideas_json, t.fetched_at, v.title, v.url, v.published_at
               FROM yolo_transcripts t LEFT JOIN yolo_videos v ON v.video_id = t.video_id
               WHERE t.status='ok' AND coalesce(v.published_at, t.fetched_at) >= ?""", (since,)).fetchall()
        blocked = c.execute(
            """SELECT count(*) FROM yolo_transcripts t LEFT JOIN yolo_videos v ON v.video_id = t.video_id
               WHERE t.status IN ('blocked','none','error') AND coalesce(v.published_at, t.fetched_at) >= ?""",
            (since,)).fetchone()[0]
        status.update({"transcripts_ok_24h": len(rows), "transcripts_unavailable_24h": int(blocked),
                       "window": "video published_at"})
        for r in rows:
            try:
                ideas = json.loads(r["ideas_json"] or "[]")
            except ValueError:
                continue
            for it in ideas if isinstance(ideas, list) else []:
                t = str(it.get("ticker") or "").upper()
                if not t:
                    continue
                a = agg.setdefault(t, {"bull": 0, "bear": 0, "hits": 0, "videos": []})
                a["bull"] += int(it.get("bull") or 0)
                a["bear"] += int(it.get("bear") or 0)
                a["hits"] += int(it.get("hits") or 0)
                a.setdefault("vids", []).append(str(r["video_id"]))
                a["videos"].append({"channel": r["channel_handle"], "title": r["title"] or "",
                                    "url": r["url"], "at": r["published_at"] or r["fetched_at"],
                                    "stance": it.get("stance"), "bull": it.get("bull"), "bear": it.get("bear")})
    except sqlite3.Error as exc:
        return [], {"family": "yolo_youtube", "state": "error", "why": str(exc)[:120]}
    finally:
        c.close()
    out: list[RawIdea] = []
    for t, a in agg.items():
        tot = a["bull"] + a["bear"]
        if tot <= 0 or a["bull"] == a["bear"]:
            continue
        side = "long" if a["bull"] > a["bear"] else "short"
        skew = abs(a["bull"] - a["bear"]) / float(tot)
        conf = 0.30 + 0.30 * skew * min(1.0, a["hits"] / 20.0)  # capped at 0.60: Yolo alone never autonomous
        v0 = a["videos"][0]
        out.append(RawIdea(
            family="yolo_youtube", ticker=t, side=side, confidence=round(conf, 3),
            thesis=f"Yolo transcripts {a['bull']} bull / {a['bear']} bear hits on {t} across {len(a['videos'])} video(s)",
            headline=f"@{v0['channel']}: {v0['title']}", source="Yolo Demon · YouTube transcript",
            url=v0.get("url"), at=v0.get("at"), ref=f"yolo:{t}",
            extra={"bull": a["bull"], "bear": a["bear"], "hits": a["hits"], "videos": len(a["videos"]),
                   "video_ids": list(dict.fromkeys(a.get("vids") or []))[:12]},
        ))
    status["directional_tickers"] = len(out)
    return out, status


def fed_ideas(fed_db: Path, now: datetime, hours: float = 24.0) -> tuple[list[RawIdea], dict[str, Any]]:
    """Fed desk (CME FedWatch) next-meeting view → macro index view (official)."""
    c = _ro(fed_db)
    if c is None:
        return [], {"family": "fed", "state": "unavailable", "why": "snowball_fed.db missing"}
    try:
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta").fetchall()}
    except sqlite3.Error as exc:
        return [], {"family": "fed", "state": "error", "why": str(exc)[:120]}
    finally:
        c.close()
    st: dict[str, Any] = {"family": "fed", "state": "ok", "last_research_at": meta.get("last_research_at"),
                          "last_error": (meta.get("last_research_error") or "")[:160] or None}
    at = _parse(meta.get("last_research_at"))
    if at is None or now - at > timedelta(hours=hours):
        st["state"] = "stale"
        return [], st
    try:
        research = json.loads(meta.get("last_research_json") or "{}")
    except ValueError:
        return [], {**st, "state": "error", "why": "bad research json"}
    meetings = research.get("meetings") or []
    nm = meetings[0] if meetings else {}
    dom = str(nm.get("dominant") or "")
    maxp = float(nm.get("max_prob") or 0.0)
    st.update({"next_meeting": nm.get("date"), "dominant": dom, "max_prob": maxp})
    if dom not in ("hike", "cut"):
        return [], st
    side = "short" if dom == "hike" else "long"
    thesis = (f"FedWatch prices {dom} {maxp:.1%} for {nm.get('date')} "
              f"({'hawkish → index short' if side == 'short' else 'dovish → index long'})")
    out = [RawIdea(family="fed", ticker=t, side=side, confidence=round(maxp, 3), thesis=thesis,
                   headline=f"CME FedWatch next meeting {nm.get('date')}: {dom} {maxp:.1%}",
                   source="Fed desk · CME FedWatch", at=meta.get("last_research_at"),
                   ref=f"fed:{nm.get('date')}")
           for t in ("SPY", "QQQ")]
    return out, st


def clerk_ideas(db: Path, now: datetime, hours: float = 24.0) -> tuple[list[RawIdea], dict[str, Any]]:
    c = _ro(db)
    if c is None:
        return [], {"family": "clerk", "state": "unavailable", "why": "snowball_clerk.db missing"}
    since = _iso(now - timedelta(hours=hours))
    try:
        rows = c.execute(
            "SELECT member, ticker, tx_type, tx_date, amount_range, filing_date, pdf_url, created_at "
            "FROM clerk_transactions WHERE created_at >= ? AND ticker IS NOT NULL AND ticker != '' "
            "AND tx_type IN ('P','S')", (since,)).fetchall()
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM clerk_meta").fetchall()}
    except sqlite3.Error as exc:
        return [], {"family": "clerk", "state": "error", "why": str(exc)[:120]}
    finally:
        c.close()
    st = {"family": "clerk", "state": "ok", "new_transactions_24h": len(rows),
          "last_error": (meta.get("last_error") or "")[:160] or None}
    out = [RawIdea(family="clerk", ticker=str(r["ticker"]).upper(),
                   side="long" if r["tx_type"] == "P" else "short", confidence=0.35,
                   thesis=f"{r['member']} {'bought' if r['tx_type'] == 'P' else 'sold'} {r['ticker']} "
                          f"({r['amount_range']}, tx {r['tx_date']})",
                   headline=f"House PTR: {r['member']} {r['tx_type']} {r['ticker']}",
                   source="House Clerk PTR", url=r["pdf_url"], at=r["created_at"],
                   ref=f"clerk:{r['member']}:{r['ticker']}:{r['tx_type']}:{r['tx_date']}")
           for r in rows]
    return out, st


def earnings_ideas(db: Path, now: datetime, hours: float = 24.0) -> tuple[list[RawIdea], dict[str, Any]]:
    c = _ro(db)
    if c is None:
        return [], {"family": "earnings", "state": "unavailable", "why": "snowball_earnings.db missing"}
    since = _iso(now - timedelta(hours=hours))
    try:
        rows = c.execute(
            "SELECT ticker, report_date, post_ret_1d, post_ret_0d, updated_at FROM earnings_events "
            "WHERE updated_at >= ? AND (post_ret_1d IS NOT NULL OR post_ret_0d IS NOT NULL)", (since,)).fetchall()
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM earnings_meta").fetchall()}
    except sqlite3.Error as exc:
        return [], {"family": "earnings", "state": "error", "why": str(exc)[:120]}
    finally:
        c.close()
    err = (meta.get("last_error") or "")
    st = {"family": "earnings", "state": "unavailable" if ("HTTP 0" in err or "HTTP 4" in err) and not rows else "ok",
          "rows_24h": len(rows), "last_error": err[:160] or None}
    out = []
    for r in rows:
        ret = r["post_ret_1d"] if r["post_ret_1d"] is not None else r["post_ret_0d"]
        if ret is None or abs(float(ret)) < 0.03:
            continue
        side = "long" if float(ret) > 0 else "short"
        out.append(RawIdea(family="earnings", ticker=str(r["ticker"]).upper(), side=side,
                           confidence=min(0.55, 0.35 + abs(float(ret))), thesis=f"post-earnings drift {float(ret):+.1%}",
                           headline=f"{r['ticker']} reported {r['report_date']} ({float(ret):+.1%})",
                           source="Earnings scout", at=r["updated_at"], ref=f"earn:{r['ticker']}:{r['report_date']}"))
    return out, st


def watcher_status(db: Path, now: datetime, hours: float = 24.0) -> dict[str, Any]:
    c = _ro(db)
    if c is None:
        return {"family": "watcher", "state": "unavailable"}
    try:
        n = c.execute("SELECT count(*) FROM research_events WHERE fetched_at >= ?",
                      (_iso(now - timedelta(hours=hours)),)).fetchone()[0]
    except sqlite3.Error:
        n = None
    finally:
        c.close()
    return {"family": "watcher", "state": "ok", "events_24h": n,
            "why": "no directional output (keyword tags only, no sentiment); acts as HALT/FOMC gate"}


def combine(ideas: list[RawIdea], *, now: datetime, ttl_hours: float, min_confidence: float,
            crypto_products: list[str], futures_products: list[str],
            futures_short_products: list[str]) -> list[dict[str, Any]]:
    """Merge raw ideas per (ticker, side); confidence = 1 - Π(1 - max conf per family)."""
    groups: dict[tuple[str, str], list[RawIdea]] = {}
    for it in ideas:
        groups.setdefault((it.ticker.upper(), it.side), []).append(it)
    sides_by_ticker: dict[str, set[str]] = {}
    for (t, s) in groups:
        sides_by_ticker.setdefault(t, set()).add(s)
    ct_date = now.astimezone(CT).date().isoformat()
    out: list[dict[str, Any]] = []
    for (t, side), items in groups.items():
        fam_best: dict[str, float] = {}
        for it in items:
            fam_best[it.family] = max(fam_best.get(it.family, 0.0), float(it.confidence))
        conf = 1.0 - math.prod(1.0 - min(0.99, c) for c in fam_best.values())
        fams = sorted(fam_best)
        official = bool(OFFICIAL_FAMILIES & set(fams))
        m = map_ticker(t, crypto_products=crypto_products, futures_products=futures_products,
                       futures_short_products=futures_short_products, side=side)
        conflicted = len(sides_by_ticker.get(t, ())) > 1
        if conflicted:
            status, why = "conflicted", "sources disagree on direction"
        elif not m["tradable"]:
            status, why = "logged_only", m["why"]
        else:
            status, why = "live", m["why"]
        autonomous = (status == "live" and conf >= min_confidence and (len(fams) >= 2 or official))
        thesis = " + ".join(dict.fromkeys(it.thesis for it in items))[:400]
        refs: list[str] = []
        for it in items:
            vids = it.extra.get("video_ids") if isinstance(it.extra, dict) else None
            refs.extend(f"yt:{v}" for v in vids) if vids else refs.append(it.ref or f"{it.family}:{t}")
        out.append({
            "ticker": t, "side": side, "lane": m["lane"], "product": m["product"],
            "thesis": thesis, "sources": [it.as_source() for it in items][:12],
            "families": fams, "n_families": len(fams), "official": official,
            "confidence": round(conf, 4), "autonomous": autonomous, "status": status,
            "status_reason": why, "ct_date": ct_date,
            "expires_at": _iso(now + timedelta(hours=ttl_hours)),
            "fingerprint": f"{ct_date}|{t}|{m['product'] or '-'}|{side}",
            "idea_refs": sorted(set(refs)),
        })
    return out


class ProposalGenerator:
    def __init__(self, store: ProposalStore, settings: Any, state: Any = None,
                 history: Any = None, track_history: Any = None) -> None:
        self.store = store
        self.settings = settings
        self.state = state
        self.last_status: dict[str, Any] = {}
        from snowball.research.probability import HistoryCache

        rng = str(getattr(settings, "research_prob_history_range", "180d") or "180d")
        self.history = history or HistoryCache(range_=rng)
        self.track_history = track_history or HistoryCache(range_=rng, ttl_sec=24 * 3600.0)
        self.track: dict[str, Any] | None = None
        self._track_mono: float | None = None
        self._track_thread: threading.Thread | None = None
        self.track_sync = False  # tests: compute the family track record inline

    # ------------------------------------------------ probability of success --

    def _prob_enabled(self) -> bool:
        return bool(getattr(self.settings, "research_prob_enabled", True))

    def _trade_outcomes(self) -> dict[str, list[int]]:
        """Closed research_idea trades per source family: [wins, closed]."""
        from snowball.reports.closed_pnl import LANE_DB_FILES

        data = self._paths()["main"].parent
        lane_db = dict(LANE_DB_FILES)
        out: dict[str, list[int]] = {}
        for r in self.store.all_taken():
            db = lane_db.get(str(r.get("taken_lane") or ""))
            if not db or r.get("taken_position_id") is None:
                continue
            c = _ro(data / db)
            if c is None:
                continue
            try:
                p = c.execute("SELECT status, realized_pnl FROM positions WHERE id=?",
                              (int(r["taken_position_id"]),)).fetchone()
            except sqlite3.Error:
                p = None
            finally:
                c.close()
            if p is None or p["status"] != "closed":
                continue
            for fam in r.get("families") or []:
                o = out.setdefault(str(fam), [0, 0])
                o[0] += int(float(p["realized_pnl"] or 0) > 0)
                o[1] += 1
        return out

    def _compute_track(self) -> None:
        from snowball.research.probability import family_track_record, past_calls

        s = self.settings
        try:
            p = self._paths()
            calls = past_calls(p["main"], p["clerk"])
            bases = {str(x).upper().split("-", 1)[0] for x in (getattr(s, "product_list", []) or [])}
            tr = family_track_record(
                calls, self.track_history,
                target_pct=float(getattr(s, "research_prob_ref_target_pct", 0.03) or 0.03),
                stop_pct=float(getattr(s, "research_prob_ref_stop_pct", 0.03) or 0.03),
                horizon_hours=float(getattr(s, "research_prob_horizon_hours", 72.0) or 72.0),
                crypto_bases=bases, trade_outcomes=self._trade_outcomes())
            self.track = {"at": _iso(utcnow()), "families": tr, "calls": len(calls)}
            self.store.set_meta("track_record", json.dumps(self.track, default=str))
            log.info("research family track record computed",
                     extra={"data": {k: {"n": v.get("n"), "hit": v.get("hit_rate"), "delta": v.get("delta")}
                                     for k, v in tr.items()}})
        except Exception:  # noqa: BLE001
            log.exception("research family track record failed")

    def _maybe_refresh_track(self) -> None:
        if self.track is None:
            raw = self.store.get_meta("track_record")
            if raw:
                try:
                    self.track = json.loads(raw)
                except ValueError:
                    self.track = None
        fresh = self._track_mono is not None and time.monotonic() - self._track_mono < 12 * 3600.0
        if fresh or (self._track_thread is not None and self._track_thread.is_alive()):
            return
        self._track_mono = time.monotonic()
        if self.track_sync:
            self._compute_track()
            return
        self._track_thread = threading.Thread(target=self._compute_track, name="snowball-research-track",
                                              daemon=True)
        self._track_thread.start()

    def _mark(self, lane: str | None, product: str | None) -> float | None:
        st = self.state
        if st is None or not product:
            return None
        pairs = getattr(st, "futures_pairs", None) if lane == "futures" else getattr(st, "pairs", None)
        snap = (pairs or {}).get(product)
        v = getattr(snap, "last", None) if snap is not None else None
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    def bracket(self, prop: dict[str, Any]) -> dict[str, Any]:
        """Target / stop the lane would really use for this proposal (fractions of entry)."""
        s = self.settings
        lane, product = prop.get("lane"), prop.get("product")
        if lane == "futures" and product:
            tp = float(getattr(s, "futures_momentum_take_profit_pct", 0.008) or 0.008)
            mark = self._mark(lane, product)
            from snowball.futures.market import contract_size_for_product
            from snowball.futures.stops import round_trip_fee_per_contract, stop_distance_usd_per_contract

            fam = family_of(product)
            pct = float(getattr(s, "futures_stop_pct", 0.08) or 0.08)
            pct_fams = {x.strip().upper() for x in str(getattr(s, "futures_stop_pct_families", "US5,TEK")).split(",")}
            if mark is None:
                if fam in pct_fams:
                    return {"kind": "futures", "target_pct": tp, "stop_pct": pct, "fee_pct": 0.0,
                            "stop_rule": f"FUTURES_STOP_PCT {pct:.0%} (no live mark for $ cap)", "mark": None}
                return {"kind": "futures", "target_pct": tp, "stop_pct": None, "fee_pct": 0.0,
                        "error": "no live futures mark for the $/contract stop", "mark": None}
            cs = float(contract_size_for_product(product) or 1.0)
            dist, leg = stop_distance_usd_per_contract(product, mark, s)
            fee = round_trip_fee_per_contract(mark, product, s)
            return {"kind": "futures", "target_pct": tp, "stop_pct": round(dist / (mark * cs), 5),
                    "fee_pct": round(fee / (mark * cs), 6), "mark": mark,
                    "stop_rule": ("FUTURES_STOP_PCT" if leg == "pct" else "FUTURES_STOP_USD_PER_CONTRACT")
                                 + f" (${dist:,.0f}/contract)"}
        if lane == "crypto":
            try:
                tp = float(s.effective_min_take_profit_pct_for("research_idea"))
            except Exception:  # noqa: BLE001
                tp = float(getattr(s, "min_take_profit_pct", 0.06) or 0.06) + float(getattr(s, "fee_buffer_pct", 0.0) or 0.0)
            return {"kind": "spot", "target_pct": tp, "stop_pct": None, "fee_pct": 0.0,
                    "stop_rule": "none (spot never-sell-red: held until green)", "mark": self._mark(lane, product)}
        return {"kind": "reference", "target_pct": float(getattr(s, "research_prob_ref_target_pct", 0.03) or 0.03),
                "stop_pct": float(getattr(s, "research_prob_ref_stop_pct", 0.03) or 0.03), "fee_pct": 0.0,
                "stop_rule": "reference bracket (not tradable; log only)", "mark": None}

    def score_proposal(self, prop: dict[str, Any], now: datetime) -> dict[str, Any]:
        from snowball.research.probability import history_symbol, score

        s = self.settings
        horizon = float(getattr(s, "research_prob_horizon_hours", 72.0) or 72.0)
        br = self.bracket(prop)
        bases = {str(x).upper().split("-", 1)[0] for x in (getattr(s, "product_list", []) or [])}
        sym = history_symbol(prop["ticker"], prop.get("lane"), prop.get("product"), bases)
        fams = (self.track or {}).get("families") or {}
        deltas = {f: float((fams.get(f) or {}).get("delta") or 0.0) for f in prop.get("families") or []}
        if br.get("error"):
            res: dict[str, Any] = {"probability": None, "why": br["error"]}
        else:
            bars = self.history.get(sym)
            if not bars:
                res = {"probability": None, "why": f"no price history for {sym}"}
            else:
                res = score(bars, side=prop["side"], target_pct=br["target_pct"], stop_pct=br.get("stop_pct"),
                            horizon_hours=horizon, family_deltas=deltas, fee_pct=float(br.get("fee_pct") or 0.0))
        if res.get("probability") is not None:
            self._apply_regime_input(res, sym, prop["side"], br)
        res.update({"symbol": sym, "bracket": br, "track_at": (self.track or {}).get("at"),
                    "track": {f: {k: (fams.get(f) or {}).get(k) for k in ("n", "hit_rate", "base", "delta", "why")}
                              for f in prop.get("families") or []}})
        return res

    def _apply_regime_input(self, res: dict[str, Any], sym: str, side: str, br: dict[str, Any]) -> None:
        """Forward Guidance regime input: logit tilt only where its OOS 1w P(up) skill is positive."""
        s = self.settings
        if not bool(getattr(s, "forward_research_input_enabled", False)) or self.state is None:
            return
        try:
            from snowball.forward.runner import fresh_guidance
            from snowball.research.probability import logit, sigmoid

            fg = fresh_guidance(self.state)
            if not fg:
                return
            mk = {"SPY": "US5", "QQQ": "TEK", "CL=F": "NOL"}.get(sym, str(sym).upper().split("-", 1)[0])
            ri = (fg.get("research_input") or {}).get(mk)
            if not ri:
                return
            tilt = float(ri.get("long_logit") or 0.0) * (1.0 if side == "long" else -1.0)
            res["regime_input"] = {"market": mk, "regime_1w": ri.get("regime_1w"), "skill_up_1w": ri.get("skill_up_1w"),
                                   "enabled": bool(ri.get("enabled")), "logit": round(tilt, 4) if ri.get("enabled") else 0.0}
            if not ri.get("enabled") or abs(tilt) < 1e-9:
                return
            p = min(0.99, max(0.01, sigmoid(logit(float(res["probability"])) + tilt)))
            res["probability_before_regime"] = res["probability"]
            res["probability"] = round(p, 4)
            stop = res.get("stop_pct")
            if stop is not None and float(stop) > 0:
                tgt, fee = float(res["target_pct"]), float(br.get("fee_pct") or 0.0)
                res["ev_pct"] = round(p * tgt - (1 - p) * float(stop) - fee, 5)
        except Exception:  # noqa: BLE001
            log.exception("forward regime input failed (ignored)")

    def decide(self, prop: dict[str, Any], res: dict[str, Any], now: datetime) -> tuple[str, str]:
        s = self.settings
        thr = float(getattr(s, "research_exec_min_prob", 0.70) or 0.70)
        p = res.get("probability")
        if prop["status"] == "conflicted":
            return "blocked", "conflicted: sources disagree on direction"
        if prop["status"] == "logged_only":
            return "blocked", f"not tradable: {prop.get('status_reason') or 'no product'}"
        if p is None:
            return "blocked", f"no probability ({res.get('why') or 'unavailable'})"
        if p < thr:
            return "below_threshold", f"p {p:.0%} < {thr:.0%} (logged only)"
        ev = res.get("ev_pct")
        if bool(getattr(s, "research_exec_require_positive_ev", True)) and ev is not None and ev <= 0:
            return "blocked", (f"negative_ev: p {p:.0%} is below break-even {float(res.get('break_even_p') or 0):.0%} "
                               f"for target {res['target_pct']:.2%} vs stop {float(res['stop_pct']):.2%}")
        hrs = float(getattr(s, "research_exec_dedupe_hours", 72.0) or 72.0)
        since = now - timedelta(hours=hrs)
        refs = set(prop.get("idea_refs") or [])
        if refs:
            done = self.store.taken_refs(ticker=prop["ticker"], side=prop["side"], since=since)
            if refs <= done:
                return "blocked", "duplicate: this idea (same sources) was already executed"
        elif self.store.taken_any(ticker=prop["ticker"], side=prop["side"], since=since):
            return "blocked", "duplicate: same ticker/side already executed in the dedupe window"
        return "eligible", f"p {p:.0%} >= {thr:.0%}" + (f", EV {ev:+.2%}" if ev is not None else "")

    def apply_probabilities(self, props: list[dict[str, Any]], now: datetime) -> None:
        for prop in props:
            try:
                res = self.score_proposal(prop, now)
            except Exception as exc:  # noqa: BLE001
                log.exception("research probability failed", extra={"data": {"ticker": prop.get("ticker")}})
                res = {"probability": None, "why": f"error: {str(exc)[:80]}"}
            state, why = self.decide(prop, res, now)
            prop["probability"] = res.get("probability")
            prop["prob"] = res
            prop["exec_state"] = state
            prop["exec_reason"] = why
            prop["autonomous"] = state == "eligible"

    def _paths(self) -> dict[str, Path]:
        s = self.settings
        data = Path(getattr(s, "sqlite_path", "./data/snowball.db")).parent
        return {
            "main": Path(getattr(s, "sqlite_path", data / "snowball.db")),
            "fed": Path(getattr(s, "fed_sqlite_path", data / "snowball_fed.db")),
            "clerk": Path(getattr(s, "clerk_sqlite_path", data / "snowball_clerk.db")),
            "earnings": Path(getattr(s, "earnings_sqlite_path", data / "snowball_earnings.db")),
        }

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        s = self.settings
        p = self._paths()
        expired = self.store.expire(now)
        ideas: list[RawIdea] = []
        status: dict[str, Any] = {}
        for name, fn, path in (("yolo_youtube", yolo_transcript_ideas, p["main"]),
                               ("fed", fed_ideas, p["fed"]),
                               ("clerk", clerk_ideas, p["clerk"]),
                               ("earnings", earnings_ideas, p["earnings"])):
            try:
                got, st = fn(path, now)
            except Exception as exc:  # noqa: BLE001
                got, st = [], {"family": name, "state": "error", "why": str(exc)[:120]}
            ideas.extend(got)
            status[name] = st
        status["watcher"] = watcher_status(p["main"], now)
        yx = "missing_key"
        try:
            c = _ro(p["main"])
            if c is not None:
                r = c.execute("SELECT value FROM yolo_meta WHERE key='source_status'").fetchone()
                c.close()
                if r and "x=" in str(r[0]):
                    yx = str(r[0]).split("x=", 1)[1].split(",")[0]
        except sqlite3.Error:
            pass
        status["yolo_x"] = {"family": "yolo_x", "state": "ok" if yx == "ok" else "unavailable", "why": f"x={yx}"}
        props = combine(
            ideas, now=now,
            ttl_hours=float(getattr(s, "research_proposal_ttl_hours", 24.0) or 24.0),
            min_confidence=float(getattr(s, "research_min_confidence", 0.70) or 0.70),
            crypto_products=list(getattr(s, "product_list", []) or []),
            futures_products=list(getattr(s, "futures_product_list", []) or []),
            futures_short_products=list(getattr(s, "futures_short_product_list", []) or []),
        )
        if self._prob_enabled():
            self.history.budget = int(getattr(s, "research_prob_fetch_budget", 12) or 12)
            self._maybe_refresh_track()
            self.apply_probabilities(props, now)
        new = 0
        for prop in props:
            _pid, created = self.store.upsert(prop, now)
            new += int(created)
        summary = {"at": _iso(now), "raw_ideas": len(ideas), "proposals": len(props), "new": new,
                   "expired": expired, "sources": status}
        if self._prob_enabled():
            ex: dict[str, int] = {}
            for prop in props:
                k = str(prop.get("exec_state"))
                ex[k] = ex.get(k, 0) + 1
            summary["exec"] = ex
            summary["exec_min_prob"] = float(getattr(s, "research_exec_min_prob", 0.70) or 0.70)
            summary["probabilities"] = {f"{p_['ticker']} {p_['side']}": p_.get("probability") for p_ in props}
        self.store.set_meta("last_run", json.dumps(summary, default=str))
        self.last_status = summary
        log.info("research proposals generated", extra={"data": {k: v for k, v in summary.items() if k != "sources"}})
        return summary
