"""Own sqlite for Earnings Scout. Never opens crypto/stock/futures ledgers."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from snowball.models import utcnow

FORBIDDEN_DB_NAMES = {"snowball.db", "snowball_stocks.db", "snowball_futures.db", "snowball_clerk.db"}


def event_to_dict(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in (
        "eps_forecast",
        "last_year_eps",
        "pre_ret_5d",
        "pre_ret_20d",
        "post_ret_0d",
        "post_ret_1d",
        "post_ret_5d",
        "pre_last_close",
        "post_anchor_close",
    ):
        if d.get(key) is not None:
            try:
                d[key] = float(d[key])
            except (TypeError, ValueError):
                pass
    if d.get("no_of_ests") is not None:
        try:
            d["no_of_ests"] = int(d["no_of_ests"])
        except (TypeError, ValueError):
            pass
    return d


class EarningsStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        if self._path.name in FORBIDDEN_DB_NAMES:
            raise ValueError(
                f"Earnings Scout refuses ledger database {self._path.name}; "
                "use data/snowball_earnings.db"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS earnings_events (
                ticker TEXT NOT NULL,
                report_date TEXT NOT NULL,
                name TEXT,
                time_token TEXT,
                session TEXT,
                eps_forecast REAL,
                no_of_ests INTEGER,
                fiscal_quarter_ending TEXT,
                market_cap TEXT,
                last_year_eps REAL,
                last_year_rpt_dt TEXT,
                pre_ret_5d REAL,
                pre_ret_20d REAL,
                pre_momentum TEXT,
                pre_asof_date TEXT,
                pre_last_close REAL,
                post_ret_0d REAL,
                post_ret_1d REAL,
                post_ret_5d REAL,
                post_anchor_date TEXT,
                post_anchor_close REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, report_date)
            );
            CREATE INDEX IF NOT EXISTS idx_earnings_report_date
                ON earnings_events(report_date);
            CREATE INDEX IF NOT EXISTS idx_earnings_session
                ON earnings_events(session, report_date);
            CREATE TABLE IF NOT EXISTS earnings_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM earnings_meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO earnings_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def upsert_event(self, row: dict[str, Any]) -> None:
        now = utcnow().isoformat()
        ticker = str(row["ticker"]).upper()
        report_date = str(row["report_date"])
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO earnings_events(
                    ticker, report_date, name, time_token, session,
                    eps_forecast, no_of_ests, fiscal_quarter_ending, market_cap,
                    last_year_eps, last_year_rpt_dt,
                    pre_ret_5d, pre_ret_20d, pre_momentum, pre_asof_date, pre_last_close,
                    post_ret_0d, post_ret_1d, post_ret_5d, post_anchor_date, post_anchor_close,
                    updated_at
                ) VALUES (
                    :ticker, :report_date, :name, :time_token, :session,
                    :eps_forecast, :no_of_ests, :fiscal_quarter_ending, :market_cap,
                    :last_year_eps, :last_year_rpt_dt,
                    :pre_ret_5d, :pre_ret_20d, :pre_momentum, :pre_asof_date, :pre_last_close,
                    :post_ret_0d, :post_ret_1d, :post_ret_5d, :post_anchor_date, :post_anchor_close,
                    :updated_at
                )
                ON CONFLICT(ticker, report_date) DO UPDATE SET
                    name=COALESCE(excluded.name, earnings_events.name),
                    time_token=COALESCE(excluded.time_token, earnings_events.time_token),
                    session=COALESCE(excluded.session, earnings_events.session),
                    eps_forecast=COALESCE(excluded.eps_forecast, earnings_events.eps_forecast),
                    no_of_ests=COALESCE(excluded.no_of_ests, earnings_events.no_of_ests),
                    fiscal_quarter_ending=COALESCE(excluded.fiscal_quarter_ending, earnings_events.fiscal_quarter_ending),
                    market_cap=COALESCE(excluded.market_cap, earnings_events.market_cap),
                    last_year_eps=COALESCE(excluded.last_year_eps, earnings_events.last_year_eps),
                    last_year_rpt_dt=COALESCE(excluded.last_year_rpt_dt, earnings_events.last_year_rpt_dt),
                    pre_ret_5d=COALESCE(excluded.pre_ret_5d, earnings_events.pre_ret_5d),
                    pre_ret_20d=COALESCE(excluded.pre_ret_20d, earnings_events.pre_ret_20d),
                    pre_momentum=COALESCE(excluded.pre_momentum, earnings_events.pre_momentum),
                    pre_asof_date=COALESCE(excluded.pre_asof_date, earnings_events.pre_asof_date),
                    pre_last_close=COALESCE(excluded.pre_last_close, earnings_events.pre_last_close),
                    post_ret_0d=COALESCE(excluded.post_ret_0d, earnings_events.post_ret_0d),
                    post_ret_1d=COALESCE(excluded.post_ret_1d, earnings_events.post_ret_1d),
                    post_ret_5d=COALESCE(excluded.post_ret_5d, earnings_events.post_ret_5d),
                    post_anchor_date=COALESCE(excluded.post_anchor_date, earnings_events.post_anchor_date),
                    post_anchor_close=COALESCE(excluded.post_anchor_close, earnings_events.post_anchor_close),
                    updated_at=excluded.updated_at
                """,
                {
                    "ticker": ticker,
                    "report_date": report_date,
                    "name": row.get("name"),
                    "time_token": row.get("time_token"),
                    "session": row.get("session"),
                    "eps_forecast": row.get("eps_forecast"),
                    "no_of_ests": row.get("no_of_ests"),
                    "fiscal_quarter_ending": row.get("fiscal_quarter_ending"),
                    "market_cap": row.get("market_cap"),
                    "last_year_eps": row.get("last_year_eps"),
                    "last_year_rpt_dt": row.get("last_year_rpt_dt"),
                    "pre_ret_5d": row.get("pre_ret_5d"),
                    "pre_ret_20d": row.get("pre_ret_20d"),
                    "pre_momentum": row.get("pre_momentum"),
                    "pre_asof_date": row.get("pre_asof_date"),
                    "pre_last_close": row.get("pre_last_close"),
                    "post_ret_0d": row.get("post_ret_0d"),
                    "post_ret_1d": row.get("post_ret_1d"),
                    "post_ret_5d": row.get("post_ret_5d"),
                    "post_anchor_date": row.get("post_anchor_date"),
                    "post_anchor_close": row.get("post_anchor_close"),
                    "updated_at": now,
                },
            )
            self._conn.commit()

    def update_momentum(self, ticker: str, report_date: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        allowed = {
            "pre_ret_5d",
            "pre_ret_20d",
            "pre_momentum",
            "pre_asof_date",
            "pre_last_close",
            "post_ret_0d",
            "post_ret_1d",
            "post_ret_5d",
            "post_anchor_date",
            "post_anchor_close",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        cols["updated_at"] = utcnow().isoformat()
        sets = ", ".join(f"{k}=:{k}" for k in cols)
        cols["ticker"] = ticker.upper()
        cols["report_date"] = report_date
        with self._lock:
            self._conn.execute(
                f"UPDATE earnings_events SET {sets} WHERE ticker=:ticker AND report_date=:report_date",
                cols,
            )
            self._conn.commit()

    def upcoming(self, *, from_date: str, to_date: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM earnings_events
                WHERE report_date >= ? AND report_date <= ?
                ORDER BY report_date ASC, ticker ASC
                LIMIT ?
                """,
                (from_date, to_date, limit),
            ).fetchall()
            return [event_to_dict(r) for r in rows]

    def recent_reported(self, *, from_date: str, to_date: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM earnings_events
                WHERE report_date >= ? AND report_date < ?
                ORDER BY report_date DESC, ticker ASC
                LIMIT ?
                """,
                (from_date, to_date, limit),
            ).fetchall()
            return [event_to_dict(r) for r in rows]

    def events_needing_pre(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM earnings_events
                WHERE report_date >= ? AND report_date <= ?
                ORDER BY report_date ASC, ticker ASC
                """,
                (from_date, to_date),
            ).fetchall()
            return [event_to_dict(r) for r in rows]

    def events_needing_post(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM earnings_events
                WHERE report_date >= ? AND report_date < ?
                ORDER BY report_date DESC, ticker ASC
                """,
                (from_date, to_date),
            ).fetchall()
            return [event_to_dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM earnings_events").fetchone()["n"]
            return {"events": int(total)}
