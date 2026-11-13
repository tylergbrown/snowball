"""snowball_forward.db: every daily forecast, its automatic grade, run summaries, calibration."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS fg_forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,            -- America/Chicago date of the forecast
  created_at TEXT NOT NULL,          -- UTC ISO
  market TEXT NOT NULL,              -- BTC, ETH, US5, TEK, NOL, <alt base>
  symbol TEXT NOT NULL,              -- history source key, e.g. yh:SPY, cb:BTC-USD
  asset_class TEXT,
  horizon TEXT NOT NULL,             -- 1d / 1w / 1m
  h_bars INTEGER NOT NULL,
  asof_ts INTEGER NOT NULL,          -- ms of the last complete daily bar used
  asof_date TEXT,
  asof_close REAL,
  regime TEXT NOT NULL,              -- bull / bear / range
  p_continue REAL, p_reversal REAL, p_up REAL, p_in_band REAL,
  base_continue REAL, base_up REAL, band REAL,
  n_regime INTEGER, n_total INTEGER,
  inputs_json TEXT,
  graded_at TEXT, outcome_ts INTEGER, outcome_close REAL, outcome_ret REAL,
  outcome_continue INTEGER, outcome_up INTEGER,
  brier_continue REAL, brier_base REAL, brier_up REAL, hit INTEGER,
  UNIQUE(run_date, market, horizon)
);
CREATE INDEX IF NOT EXISTS fg_forecasts_ungraded ON fg_forecasts(graded_at, symbol);
CREATE TABLE IF NOT EXISTS fg_runs (
  run_date TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, summary_json TEXT
);
CREATE TABLE IF NOT EXISTS fg_calibration (
  market TEXT NOT NULL, horizon TEXT NOT NULL, period TEXT NOT NULL,
  computed_at TEXT, metrics_json TEXT, PRIMARY KEY (market, horizon, period)
);
CREATE TABLE IF NOT EXISTS fg_meta (key TEXT PRIMARY KEY, value TEXT);
"""

FORECAST_COLS = ("run_date", "created_at", "market", "symbol", "asset_class", "horizon", "h_bars", "asof_ts",
                 "asof_date", "asof_close", "regime", "p_continue", "p_reversal", "p_up", "p_in_band",
                 "base_continue", "base_up", "band", "n_regime", "n_total", "inputs_json")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForwardStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        with self._lock:
            self._c.execute("PRAGMA journal_mode=WAL")
            self._c.executescript(SCHEMA)
            self._c.commit()

    def close(self) -> None:
        with self._lock:
            self._c.close()

    # -- forecasts --
    def insert_forecast(self, row: dict[str, Any]) -> bool:
        """First forecast of the CT day stands (no same-day revisions). Returns True if inserted."""
        vals = [row.get(k) for k in FORECAST_COLS]
        with self._lock:
            cur = self._c.execute(
                f"INSERT OR IGNORE INTO fg_forecasts ({','.join(FORECAST_COLS)}) VALUES ({','.join('?' * len(FORECAST_COLS))})",
                vals)
            self._c.commit()
            return cur.rowcount > 0

    def forecasts_for(self, run_date: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM fg_forecasts WHERE run_date=? ORDER BY market, horizon", (run_date,))]

    def previous_run_date(self, run_date: str) -> str | None:
        with self._lock:
            r = self._c.execute("SELECT max(run_date) FROM fg_forecasts WHERE run_date < ?", (run_date,)).fetchone()
        return r[0] if r and r[0] else None

    def ungraded(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM fg_forecasts WHERE graded_at IS NULL ORDER BY id")]

    def grade(self, fid: int, g: dict[str, Any]) -> None:
        with self._lock:
            self._c.execute(
                "UPDATE fg_forecasts SET graded_at=?, outcome_ts=?, outcome_close=?, outcome_ret=?, outcome_continue=?, "
                "outcome_up=?, brier_continue=?, brier_base=?, brier_up=?, hit=? WHERE id=? AND graded_at IS NULL",
                (utc_iso(), g["outcome_ts"], g["outcome_close"], g["outcome_ret"], g["outcome_continue"],
                 g["outcome_up"], g["brier_continue"], g["brier_base"], g["brier_up"], g["hit"], fid))
            self._c.commit()

    def scorecard(self) -> dict[str, Any]:
        """Running scorecard of graded live forecasts, per horizon and overall."""
        out: dict[str, Any] = {"by_horizon": {}, "pending": 0}
        with self._lock:
            out["pending"] = self._c.execute("SELECT count(*) FROM fg_forecasts WHERE graded_at IS NULL").fetchone()[0]
            out["total"] = self._c.execute("SELECT count(*) FROM fg_forecasts").fetchone()[0]
            q = ("SELECT horizon, count(*) n, avg(hit) hit, avg(brier_continue) b, avg(brier_base) bb, avg(brier_up) bu, "
                 "avg(p_continue) mp, avg(outcome_continue) obs, min(run_date) d0, max(run_date) d1 "
                 "FROM fg_forecasts WHERE graded_at IS NOT NULL GROUP BY horizon")
            for r in self._c.execute(q):
                d = dict(r)
                d["skill"] = (1.0 - d["b"] / d["bb"]) if d.get("bb") else None
                out["by_horizon"][d["horizon"]] = d
            r = self._c.execute("SELECT count(*) n, avg(hit) hit, avg(brier_continue) b, avg(brier_base) bb "
                                "FROM fg_forecasts WHERE graded_at IS NOT NULL").fetchone()
            out["all"] = dict(r) if r else {}
            out["recent"] = [dict(x) for x in self._c.execute(
                "SELECT run_date, market, horizon, regime, round(p_continue,3) p_continue, round(outcome_ret,4) outcome_ret, "
                "outcome_continue, hit, round(brier_continue,4) brier FROM fg_forecasts WHERE graded_at IS NOT NULL "
                "ORDER BY graded_at DESC, id DESC LIMIT 12")]
        return out

    # -- runs / calibration / meta --
    def save_run(self, run_date: str, summary: dict[str, Any]) -> None:
        now = utc_iso()
        js = json.dumps(summary, default=str)
        with self._lock:
            self._c.execute("INSERT INTO fg_runs (run_date, created_at, updated_at, summary_json) VALUES (?,?,?,?) "
                            "ON CONFLICT(run_date) DO UPDATE SET updated_at=excluded.updated_at, summary_json=excluded.summary_json",
                            (run_date, now, now, js))
            self._c.commit()

    def run(self, run_date: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if run_date is None:
                r = self._c.execute("SELECT run_date, created_at, updated_at, summary_json FROM fg_runs ORDER BY run_date DESC LIMIT 1").fetchone()
            else:
                r = self._c.execute("SELECT run_date, created_at, updated_at, summary_json FROM fg_runs WHERE run_date=?", (run_date,)).fetchone()
        if not r:
            return None
        try:
            d = json.loads(r["summary_json"] or "{}")
        except ValueError:
            d = {}
        d.setdefault("run_date", r["run_date"])
        d["_created_at"], d["_updated_at"] = r["created_at"], r["updated_at"]
        return d

    def save_calibration(self, market: str, horizon: str, period: str, metrics: dict[str, Any]) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO fg_calibration (market, horizon, period, computed_at, metrics_json) "
                            "VALUES (?,?,?,?,?)", (market, horizon, period, utc_iso(), json.dumps(metrics, default=str)))
            self._c.commit()

    def calibration(self, period: str = "test") -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for r in self._c.execute("SELECT market, horizon, metrics_json FROM fg_calibration WHERE period=?", (period,)):
                try:
                    out.setdefault(r["market"], {})[r["horizon"]] = json.loads(r["metrics_json"])
                except ValueError:
                    pass
        return out

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO fg_meta (key, value) VALUES (?,?)", (k, v))
            self._c.commit()

    def get_meta(self, k: str) -> str | None:
        with self._lock:
            r = self._c.execute("SELECT value FROM fg_meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else None


def default_path(settings: Any) -> Path:
    p = getattr(settings, "forward_sqlite_path", None)
    if p:
        return Path(p)
    return Path(getattr(settings, "sqlite_path", "./data/snowball.db")).parent / "snowball_forward.db"
