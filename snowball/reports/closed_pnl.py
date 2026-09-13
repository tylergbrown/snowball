"""Book-wide closed realized P/L for the daily trade PDF.

Sums closed positions across live lanes that feed the daily report:
crypto, stocks/CFM, futures/FT, crash, and fed. Unrealized is excluded.
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


@dataclass(frozen=True)
class LaneClosedPnl:
    lane: str
    ytd: float
    all_time: float
    ytd_count: int
    all_time_count: int


@dataclass(frozen=True)
class BookClosedPnl:
    year: int
    ytd: float
    all_time: float
    ytd_count: int
    all_time_count: int
    by_lane: tuple[LaneClosedPnl, ...]


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


def _closed_rows(db_path: Path) -> list[tuple[object, float]]:
    """Return (closed_at, realized_pnl) for closed rows with a PnL figure."""
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
        rows = con.execute(
            """
            SELECT closed_at, realized_pnl
            FROM positions
            WHERE status = 'closed' AND realized_pnl IS NOT NULL
            """
        ).fetchall()
        out: list[tuple[object, float]] = []
        for closed_at, pnl in rows:
            try:
                out.append((closed_at, float(pnl)))
            except (TypeError, ValueError):
                continue
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def sum_lane_closed_pnl(db_path: Path, *, year: int) -> LaneClosedPnl:
    """Closed realized P/L for one lane DB. Missing/unreadable DB -> zeros."""
    ytd = 0.0
    all_time = 0.0
    ytd_count = 0
    all_time_count = 0
    for closed_at, pnl in _closed_rows(db_path):
        all_time += pnl
        all_time_count += 1
        if ct_year_of(closed_at) == year:
            ytd += pnl
            ytd_count += 1
    return LaneClosedPnl(
        lane=db_path.name,
        ytd=ytd,
        all_time=all_time,
        ytd_count=ytd_count,
        all_time_count=all_time_count,
    )


def aggregate_closed_pnl(
    lane_dbs: dict[str, Path],
    *,
    year: int | None = None,
    now: datetime | None = None,
) -> BookClosedPnl:
    """Sum closed realized P/L across lane DBs.

    YTD = rows whose closed_at falls in the America/Chicago calendar *year*.
    All-time = every closed row with a realized_pnl (any year).
    Unparseable closed_at is included in all-time only.
    """
    y = year if year is not None else current_ct_year(now)
    lanes: list[LaneClosedPnl] = []
    ytd = 0.0
    all_time = 0.0
    ytd_count = 0
    all_time_count = 0
    for lane, path in lane_dbs.items():
        one = sum_lane_closed_pnl(path, year=y)
        one = LaneClosedPnl(
            lane=lane,
            ytd=one.ytd,
            all_time=one.all_time,
            ytd_count=one.ytd_count,
            all_time_count=one.all_time_count,
        )
        lanes.append(one)
        ytd += one.ytd
        all_time += one.all_time
        ytd_count += one.ytd_count
        all_time_count += one.all_time_count
    return BookClosedPnl(
        year=y,
        ytd=ytd,
        all_time=all_time,
        ytd_count=ytd_count,
        all_time_count=all_time_count,
        by_lane=tuple(lanes),
    )
