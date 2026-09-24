"""Book-wide closed realized P/L for the daily trade PDF.

Sums closed positions across live lanes that feed the daily report:
crypto, stocks/CFM, futures/FT, crash, and fed. Unrealized is excluded.

Primary totals are **live-only**. Paper / Yahoo-sim closes (fee=0 paper
fills, stocks without ``:live``) are tracked separately so they do not
inflate the headline. Spot (crypto) vs CFM (stocks+FT+crash+fed) splits
are exposed for the PDF.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Stable headline order. Earnings / clerk are research-only and omitted.
LANE_DB_FILES: tuple[tuple[str, str], ...] = (
    ("crypto", "snowball.db"),
    ("stocks", "snowball_stocks.db"),
    ("futures", "snowball_futures.db"),
    ("crash", "snowball_crash.db"),
    ("fed", "snowball_fed.db"),
)

# Lanes whose live closed PnL rolls into the CFM (non-spot) bucket.
CFM_LANES: frozenset[str] = frozenset({"stocks", "futures", "crash", "fed"})
SPOT_LANES: frozenset[str] = frozenset({"crypto"})


@dataclass(frozen=True)
class LaneClosedPnl:
    lane: str
    ytd: float
    all_time: float
    ytd_count: int
    all_time_count: int
    # Live-only (same as ytd/all_time when live_only filtering is applied upstream)
    live_ytd: float = 0.0
    live_all_time: float = 0.0
    live_ytd_count: int = 0
    live_all_time_count: int = 0
    paper_ytd: float = 0.0
    paper_all_time: float = 0.0
    paper_ytd_count: int = 0
    paper_all_time_count: int = 0


@dataclass(frozen=True)
class BookClosedPnl:
    year: int
    # Primary headline = live closed realized only
    ytd: float
    all_time: float
    ytd_count: int
    all_time_count: int
    by_lane: tuple[LaneClosedPnl, ...]
    # Spot (crypto) vs CFM (stocks + futures/FT + crash + fed) live splits
    spot_ytd: float = 0.0
    spot_all_time: float = 0.0
    spot_ytd_count: int = 0
    spot_all_time_count: int = 0
    cfm_ytd: float = 0.0
    cfm_all_time: float = 0.0
    cfm_ytd_count: int = 0
    cfm_all_time_count: int = 0
    # Paper excluded from headline (still available for footnotes)
    paper_ytd: float = 0.0
    paper_all_time: float = 0.0
    paper_ytd_count: int = 0
    paper_all_time_count: int = 0


def current_ct_year(now: datetime | None = None) -> int:
    """America/Chicago calendar year for *now* (UTC-naive treated as UTC)."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT).year


def default_lane_dbs(data_dir: Path) -> dict[str, Path]:
    return {lane: data_dir / name for lane, name in LANE_DB_FILES}


def parse_closed_at(ts: object) -> datetime | None:
    """Parse a positions.closed_at value. Naive ISO times are treated as UTC."""
    if ts is None:
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


def ct_year_of(ts: object) -> int | None:
    dt = parse_closed_at(ts)
    if dt is None:
        return None
    return dt.astimezone(CT).year


def is_live_fill_evidence(
    *,
    reasons: list[str] | None,
    max_fee_usd: float | None,
    has_fills: bool,
) -> bool:
    """Classify a closed position as live vs paper from fill evidence.

    Live when:
      - any fill reason contains ``:live`` or ``orphan_live``, or
      - any fill has fee_usd > 0 (real Coinbase / CFM fee path).

    Paper when fills exist but show the Yahoo/paper-sim pattern (fee=0,
    typically slip=5) and no live markers.

    No fills at all (test DBs, legacy rows): treat as live so simple
    position-only seeds keep counting.
    """
    if not has_fills:
        return True
    joined = " ".join(reasons or [])
    if ":live" in joined or "orphan_live" in joined:
        return True
    try:
        fee = float(max_fee_usd or 0.0)
    except (TypeError, ValueError):
        fee = 0.0
    return fee > 0.0


def _closed_rows(db_path: Path) -> list[tuple[object, float, bool]]:
    """Return (closed_at, realized_pnl, is_live) for closed rows with PnL."""
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "positions" not in tables:
            return []
        has_fills = "fills" in tables
        if has_fills:
            rows = con.execute(
                """
                SELECT p.closed_at, p.realized_pnl, p.id,
                       (SELECT MAX(f.fee_usd) FROM fills f
                        WHERE f.position_id = p.id) AS max_fee,
                       (SELECT COUNT(*) FROM fills f
                        WHERE f.position_id = p.id) AS fill_count,
                       (SELECT GROUP_CONCAT(f.reason, ' ') FROM fills f
                        WHERE f.position_id = p.id) AS reasons
                FROM positions p
                WHERE p.status = 'closed' AND p.realized_pnl IS NOT NULL
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT closed_at, realized_pnl, id, NULL, 0, NULL
                FROM positions
                WHERE status = 'closed' AND realized_pnl IS NOT NULL
                """
            ).fetchall()
        out: list[tuple[object, float, bool]] = []
        for closed_at, pnl, _pid, max_fee, fill_count, reasons in rows:
            try:
                pnl_f = float(pnl)
            except (TypeError, ValueError):
                continue
            reason_list = (
                [reasons] if isinstance(reasons, str) and reasons else []
            )
            live = is_live_fill_evidence(
                reasons=reason_list,
                max_fee_usd=max_fee,
                has_fills=bool(fill_count and int(fill_count) > 0),
            )
            out.append((closed_at, pnl_f, live))
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def sum_lane_closed_pnl(db_path: Path, *, year: int) -> LaneClosedPnl:
    """Closed realized P/L for one lane DB. Missing/unreadable DB -> zeros.

    ``ytd`` / ``all_time`` on the returned lane are **live-only** (matches
    book headline). Paper totals are on the paper_* fields.
    """
    live_ytd = live_all = 0.0
    live_ytd_n = live_all_n = 0
    paper_ytd = paper_all = 0.0
    paper_ytd_n = paper_all_n = 0
    for closed_at, pnl, live in _closed_rows(db_path):
        in_ytd = ct_year_of(closed_at) == year
        if live:
            live_all += pnl
            live_all_n += 1
            if in_ytd:
                live_ytd += pnl
                live_ytd_n += 1
        else:
            paper_all += pnl
            paper_all_n += 1
            if in_ytd:
                paper_ytd += pnl
                paper_ytd_n += 1
    return LaneClosedPnl(
        lane=db_path.name,
        ytd=live_ytd,
        all_time=live_all,
        ytd_count=live_ytd_n,
        all_time_count=live_all_n,
        live_ytd=live_ytd,
        live_all_time=live_all,
        live_ytd_count=live_ytd_n,
        live_all_time_count=live_all_n,
        paper_ytd=paper_ytd,
        paper_all_time=paper_all,
        paper_ytd_count=paper_ytd_n,
        paper_all_time_count=paper_all_n,
    )


def aggregate_closed_pnl(
    lane_dbs: dict[str, Path],
    *,
    year: int | None = None,
    now: datetime | None = None,
) -> BookClosedPnl:
    """Sum **live** closed realized P/L across lane DBs.

    YTD = live rows whose closed_at falls in the America/Chicago calendar
    *year*. All-time = every live closed row with a realized_pnl.
    Paper closes are excluded from headline fields and reported on
    ``paper_*``. Spot vs CFM live splits are on ``spot_*`` / ``cfm_*``.
    Unparseable closed_at is included in all-time only (not YTD).
    """
    y = year if year is not None else current_ct_year(now)
    lanes: list[LaneClosedPnl] = []
    ytd = all_time = 0.0
    ytd_count = all_time_count = 0
    spot_ytd = spot_all = 0.0
    spot_ytd_n = spot_all_n = 0
    cfm_ytd = cfm_all = 0.0
    cfm_ytd_n = cfm_all_n = 0
    paper_ytd = paper_all = 0.0
    paper_ytd_n = paper_all_n = 0
    for lane, path in lane_dbs.items():
        one = sum_lane_closed_pnl(path, year=y)
        one = LaneClosedPnl(
            lane=lane,
            ytd=one.ytd,
            all_time=one.all_time,
            ytd_count=one.ytd_count,
            all_time_count=one.all_time_count,
            live_ytd=one.live_ytd,
            live_all_time=one.live_all_time,
            live_ytd_count=one.live_ytd_count,
            live_all_time_count=one.live_all_time_count,
            paper_ytd=one.paper_ytd,
            paper_all_time=one.paper_all_time,
            paper_ytd_count=one.paper_ytd_count,
            paper_all_time_count=one.paper_all_time_count,
        )
        lanes.append(one)
        ytd += one.ytd
        all_time += one.all_time
        ytd_count += one.ytd_count
        all_time_count += one.all_time_count
        paper_ytd += one.paper_ytd
        paper_all += one.paper_all_time
        paper_ytd_n += one.paper_ytd_count
        paper_all_n += one.paper_all_time_count
        if lane in SPOT_LANES:
            spot_ytd += one.ytd
            spot_all += one.all_time
            spot_ytd_n += one.ytd_count
            spot_all_n += one.all_time_count
        elif lane in CFM_LANES:
            cfm_ytd += one.ytd
            cfm_all += one.all_time
            cfm_ytd_n += one.ytd_count
            cfm_all_n += one.all_time_count
    return BookClosedPnl(
        year=y,
        ytd=ytd,
        all_time=all_time,
        ytd_count=ytd_count,
        all_time_count=all_time_count,
        by_lane=tuple(lanes),
        spot_ytd=spot_ytd,
        spot_all_time=spot_all,
        spot_ytd_count=spot_ytd_n,
        spot_all_time_count=spot_all_n,
        cfm_ytd=cfm_ytd,
        cfm_all_time=cfm_all,
        cfm_ytd_count=cfm_ytd_n,
        cfm_all_time_count=cfm_all_n,
        paper_ytd=paper_ytd,
        paper_all_time=paper_all,
        paper_ytd_count=paper_ytd_n,
        paper_all_time_count=paper_all_n,
    )
