"""Book-wide closed realized P/L aggregators for the daily PDF."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from snowball.reports.closed_pnl import (
    aggregate_closed_pnl,
    ct_year_of,
    current_ct_year,
    default_lane_dbs,
    parse_closed_at,
    sum_lane_closed_pnl,
)

POS_SCHEMA = """
CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    product TEXT,
    status TEXT,
    realized_pnl REAL,
    closed_at TEXT
)
"""


def _seed(path: Path, rows: list[tuple[str, object, object]]) -> Path:
    con = sqlite3.connect(path)
    con.execute(POS_SCHEMA)
    for status, pnl, closed_at in rows:
        con.execute(
            "INSERT INTO positions (product, status, realized_pnl, closed_at) VALUES (?,?,?,?)",
            ("X", status, pnl, closed_at),
        )
    con.commit()
    con.close()
    return path


def test_ct_year_boundary_december_stays_prior_year() -> None:
    # 2026-01-01 05:59:59 UTC = 2025-12-31 23:59:59 CST
    assert ct_year_of("2026-01-01T05:59:59+00:00") == 2025
    # 2026-01-01 06:00:00 UTC = 2026-01-01 00:00:00 CST
    assert ct_year_of("2026-01-01T06:00:00+00:00") == 2026
    assert ct_year_of("2026-01-01T06:00:00Z") == 2026


def test_naive_iso_treated_as_utc() -> None:
    dt = parse_closed_at("2026-01-01T06:00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert ct_year_of("2026-01-01T06:00:00") == 2026
    assert ct_year_of("2026-01-01T05:59:59") == 2025


def test_current_ct_year_uses_chicago_calendar() -> None:
    # Still 2025 in Chicago, already 2026 UTC
    assert current_ct_year(datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)) == 2025
    assert current_ct_year(datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)) == 2026
    assert current_ct_year(datetime(2026, 9, 11, 12, 0)) == 2026


def test_sum_lane_ytd_vs_all_time(tmp_path: Path) -> None:
    db = _seed(
        tmp_path / "lane.db",
        [
            ("closed", 10.0, "2025-12-31T23:30:00-06:00"),  # 2025 CT
            ("closed", 7.5, "2026-01-01T00:30:00-06:00"),  # 2026 CT
            ("closed", -2.0, "2026-09-11T19:55:13+00:00"),  # 2026 CT
            ("open", 99.0, None),
            ("closed", None, "2026-06-01T12:00:00+00:00"),  # no pnl
        ],
    )
    got = sum_lane_closed_pnl(db, year=2026)
    assert got.all_time == 15.5
    assert got.all_time_count == 3
    assert got.ytd == 5.5
    assert got.ytd_count == 2


def test_aggregate_sums_all_live_lanes(tmp_path: Path) -> None:
    crypto = _seed(
        tmp_path / "snowball.db",
        [("closed", 24.87, "2026-09-08T01:53:10+00:00")],
    )
    stocks = _seed(
        tmp_path / "snowball_stocks.db",
        [("closed", 6.50, "2026-09-09T15:08:47+00:00")],
    )
    futures = _seed(
        tmp_path / "snowball_futures.db",
        [
            ("closed", 0.18, "2026-09-11T19:55:13+00:00"),
            ("closed", 4.00, "2025-06-01T12:00:00+00:00"),
        ],
    )
    crash = _seed(tmp_path / "snowball_crash.db", [])
    # fed DB omitted — missing file counts as zero
    book = aggregate_closed_pnl(
        {
            "crypto": crypto,
            "stocks": stocks,
            "futures": futures,
            "crash": crash,
            "fed": tmp_path / "snowball_fed.db",
        },
        year=2026,
    )
    assert book.year == 2026
    assert book.ytd == 31.55
    assert book.all_time == 35.55
    assert book.ytd_count == 3
    assert book.all_time_count == 4
    by = {r.lane: r for r in book.by_lane}
    assert set(by) == {"crypto", "stocks", "futures", "crash", "fed"}
    assert by["crypto"].ytd == 24.87
    assert by["stocks"].all_time == 6.50
    assert by["futures"].ytd == 0.18
    assert by["futures"].all_time == 4.18
    assert by["crash"].all_time == 0.0
    assert by["fed"].all_time == 0.0


def test_unparseable_closed_at_counts_all_time_only(tmp_path: Path) -> None:
    db = _seed(
        tmp_path / "x.db",
        [
            ("closed", 3.0, "not-a-timestamp"),
            ("closed", 1.0, "2026-07-04T12:00:00+00:00"),
        ],
    )
    got = sum_lane_closed_pnl(db, year=2026)
    assert got.all_time == 4.0
    assert got.all_time_count == 2
    assert got.ytd == 1.0
    assert got.ytd_count == 1


def test_default_lane_dbs_names(tmp_path: Path) -> None:
    dbs = default_lane_dbs(tmp_path)
    assert list(dbs) == ["crypto", "stocks", "futures", "crash", "fed"]
    assert dbs["crypto"] == tmp_path / "snowball.db"
    assert dbs["fed"] == tmp_path / "snowball_fed.db"


def test_missing_positions_table_is_zero(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (k TEXT)")
    con.commit()
    con.close()
    got = sum_lane_closed_pnl(db, year=2026)
    assert got.all_time == 0.0
    assert got.ytd == 0.0
    assert got.all_time_count == 0
