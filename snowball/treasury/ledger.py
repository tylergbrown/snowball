"""Minimal SQLite Bitcoin treasury ledger (holdings, cost, contributions, marks)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

DB_NAME = "snowball_treasury.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    usd_amount REAL NOT NULL,
    btc_qty REAL NOT NULL,
    btc_price REAL NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_contrib_ts ON contributions(ts);

CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    btc_usd REAL NOT NULL,
    holdings_btc REAL NOT NULL,
    cost_basis_usd REAL NOT NULL,
    mark_value_usd REAL NOT NULL,
    unrealized_pnl_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marks_ts ON marks(ts);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Documented seed policy — do not auto-apply the CRYPTO fuel deposit.
SEED_POLICY = {
    "crypto_fuel_deposit_usd": 620.0,
    "crypto_fuel_deposit_date_ct": "2026-09-27",
    "seeded_as_treasury": False,
    "reason": (
        "2026-09-27 $620 BTC Coinbase Advanced deposit is live CRYPTO-lane "
        "bankroll fuel (SnowBall profile). Bitcoin treasury is a separate "
        "backstop; ledger starts empty until friday_50 / pnl_sweep / manual "
        "contributions are recorded."
    ),
}


@dataclass(frozen=True)
class Contribution:
    id: int
    ts: str
    kind: str
    usd_amount: float
    btc_qty: float
    btc_price: float
    note: str | None


@dataclass(frozen=True)
class TreasurySnapshot:
    """Point-in-time treasury metrics for the daily PDF."""

    as_of: datetime
    holdings_btc: float
    cost_basis_usd: float
    avg_price_usd: float | None  # cost / qty; None if empty
    mark_btc_usd: float | None
    mark_value_usd: float
    total_pnl_usd: float | None  # mark_value - cost; None if empty
    # 30-day BTC-USD spot % change (not treasury NAV); labeled in PDF
    trend_30d_btc_spot_pct: float | None
    trend_30d_label: str
    # Trailing 7 CT-day treasury mark P/L (ex-contributions)
    weekly_pnl_usd: float | None
    weekly_pnl_label: str
    contribution_count: int
    empty: bool


def default_treasury_db(data_dir: Path) -> Path:
    return Path(data_dir) / DB_NAME


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(db_path: Path) -> Path:
    con = _connect(db_path)
    try:
        con.executescript(SCHEMA)
        existing = con.execute(
            "SELECT value FROM meta WHERE key = ?", ("seed_policy",)
        ).fetchone()
        if existing is None:
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("seed_policy", json.dumps(SEED_POLICY)),
            )
        con.commit()
    finally:
        con.close()
    return db_path


def record_contribution(
    db_path: Path,
    *,
    usd_amount: float,
    btc_qty: float,
    btc_price: float,
    kind: str,
    ts: datetime | None = None,
    note: str | None = None,
) -> Contribution:
    """Append a treasury BTC buy (friday_50 / pnl_sweep / manual / …)."""
    ensure_schema(db_path)
    if btc_qty <= 0 or usd_amount <= 0 or btc_price <= 0:
        raise ValueError("usd_amount, btc_qty, and btc_price must be positive")
    dt = ts or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts_s = dt.astimezone(timezone.utc).isoformat()
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            INSERT INTO contributions (ts, kind, usd_amount, btc_qty, btc_price, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts_s, kind, float(usd_amount), float(btc_qty), float(btc_price), note),
        )
        con.commit()
        row_id = int(cur.lastrowid)
    finally:
        con.close()
    return Contribution(
        id=row_id,
        ts=ts_s,
        kind=kind,
        usd_amount=float(usd_amount),
        btc_qty=float(btc_qty),
        btc_price=float(btc_price),
        note=note,
    )


def holdings_and_cost(db_path: Path) -> tuple[float, float, int]:
    """Return (btc_qty, cost_basis_usd, contribution_count)."""
    if not db_path.exists():
        return 0.0, 0.0, 0
    con = _connect(db_path)
    try:
        row = con.execute(
            """
            SELECT COALESCE(SUM(btc_qty), 0),
                   COALESCE(SUM(usd_amount), 0),
                   COUNT(*)
            FROM contributions
            """
        ).fetchone()
        return float(row[0] or 0), float(row[1] or 0), int(row[2] or 0)
    except sqlite3.Error:
        return 0.0, 0.0, 0
    finally:
        con.close()


def record_mark(
    db_path: Path,
    *,
    btc_usd: float,
    ts: datetime | None = None,
) -> None:
    ensure_schema(db_path)
    qty, cost, _ = holdings_and_cost(db_path)
    dt = ts or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mark_value = qty * float(btc_usd)
    pnl = mark_value - cost
    con = _connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO marks
              (ts, btc_usd, holdings_btc, cost_basis_usd, mark_value_usd, unrealized_pnl_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                dt.astimezone(timezone.utc).isoformat(),
                float(btc_usd),
                qty,
                cost,
                mark_value,
                pnl,
            ),
        )
        con.commit()
    finally:
        con.close()


def _parse_ts(ts: object) -> datetime | None:
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


def _net_contributions_usd(
    db_path: Path, start: datetime, end: datetime
) -> float:
    if not db_path.exists():
        return 0.0
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT ts, usd_amount FROM contributions"
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    finally:
        con.close()
    total = 0.0
    for ts, usd in rows:
        dt = _parse_ts(ts)
        if dt is None:
            continue
        if start <= dt < end:
            total += float(usd or 0)
    return total


def _mark_nearest(
    db_path: Path, target: datetime, *, within_hours: float = 36.0
) -> tuple[float, float] | None:
    """Return (mark_value_usd, cost_basis_usd) nearest to target, or None."""
    if not db_path.exists():
        return None
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT ts, mark_value_usd, cost_basis_usd FROM marks ORDER BY ts"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    best: tuple[float, float, float] | None = None  # abs_delta, mark, cost
    for ts, mark_v, cost in rows:
        dt = _parse_ts(ts)
        if dt is None:
            continue
        delta = abs((dt - target).total_seconds())
        if delta > within_hours * 3600:
            continue
        if best is None or delta < best[0]:
            best = (delta, float(mark_v or 0), float(cost or 0))
    if best is None:
        return None
    return best[1], best[2]


def trailing_7d_weekly_pnl(
    db_path: Path,
    *,
    now: datetime,
    mark_value_now: float,
    cost_now: float,
) -> float | None:
    """Treasury P/L over trailing 7 CT days, excluding net contributions.

    weekly_pnl = (mark_now - cost_now) - (mark_then - cost_then)
               = change in unrealized, which already nets out new cost basis
                 from contributions in the window when both ends are marked.
    If no historical mark near (now-7d), return None (PDF shows empty).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    then = now - timedelta(days=7)
    hist = _mark_nearest(db_path, then)
    if hist is None:
        # Fallback: if no marks yet but also no holdings then, weekly = 0
        qty, cost, n = holdings_and_cost(db_path)
        if n == 0 and abs(mark_value_now) < 1e-12:
            return 0.0
        # Reconstruct then-cost from contributions before `then`
        con_cost_before = 0.0
        con_qty_before = 0.0
        if db_path.exists():
            c = _connect(db_path)
            try:
                for ts, usd, qty_r in c.execute(
                    "SELECT ts, usd_amount, btc_qty FROM contributions"
                ):
                    dt = _parse_ts(ts)
                    if dt is not None and dt < then:
                        con_cost_before += float(usd or 0)
                        con_qty_before += float(qty_r or 0)
            except sqlite3.Error:
                pass
            finally:
                c.close()
        if con_qty_before <= 1e-12 and qty <= 1e-12:
            return 0.0
        # Without a historical BTC mark we cannot attribute price P/L.
        return None
    mark_then, cost_then = hist
    pnl_now = mark_value_now - cost_now
    pnl_then = mark_then - cost_then
    return pnl_now - pnl_then


PriceFn = Callable[[], float | None]
SpotHistoryFn = Callable[[datetime], float | None]


def fetch_btc_usd_spot() -> float | None:
    """Public Coinbase spot (no secrets). Returns None on failure."""
    import urllib.request

    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
        amt = data.get("data", {}).get("amount")
        return float(amt) if amt is not None else None
    except Exception:
        return None


def fetch_btc_usd_spot_at(when: datetime) -> float | None:
    """Approx historical BTC-USD via Coinbase Exchange daily candle close."""
    import urllib.request
    from urllib.parse import urlencode

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Daily candles; request a small window around the day
    start = (when - timedelta(days=1)).astimezone(timezone.utc)
    end = (when + timedelta(days=2)).astimezone(timezone.utc)
    qs = urlencode(
        {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "granularity": "86400",
        }
    )
    url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "snowball-treasury/1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            candles = json.loads(r.read().decode())
        if not isinstance(candles, list) or not candles:
            return None
        # candle: [time, low, high, open, close, volume]
        target = when.timestamp()
        best = None
        for c in candles:
            try:
                t, _lo, _hi, _o, close, _v = c
                delta = abs(float(t) - target)
                if best is None or delta < best[0]:
                    best = (delta, float(close))
            except (TypeError, ValueError, IndexError):
                continue
        return best[1] if best else None
    except Exception:
        return None


def btc_spot_trend_pct(
    *,
    now: datetime,
    spot_now: float | None,
    spot_at: SpotHistoryFn | None = None,
) -> float | None:
    """30-calendar-day BTC-USD spot % change ending at *now*."""
    if spot_now is None or spot_now <= 0:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    then = now - timedelta(days=30)
    fn = spot_at or fetch_btc_usd_spot_at
    past = fn(then)
    if past is None or past <= 0:
        return None
    return (spot_now - past) / past * 100.0


def snapshot(
    db_path: Path,
    *,
    now: datetime | None = None,
    spot_now: float | None = None,
    fetch_spot: PriceFn | None = None,
    spot_at: SpotHistoryFn | None = None,
    write_mark: bool = True,
) -> TreasurySnapshot:
    """Build PDF metrics; optionally persist a mark row."""
    ensure_schema(db_path)
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    qty, cost, n = holdings_and_cost(db_path)
    empty = qty <= 1e-12 or n == 0

    getter = fetch_spot or fetch_btc_usd_spot
    mark_px = spot_now if spot_now is not None else getter()
    mark_value = (qty * mark_px) if (mark_px is not None and not empty) else 0.0
    avg = (cost / qty) if qty > 1e-12 else None
    total_pnl = (mark_value - cost) if (not empty and mark_px is not None) else None

    if write_mark and mark_px is not None:
        try:
            record_mark(db_path, btc_usd=mark_px, ts=dt)
        except Exception:
            pass

    trend = btc_spot_trend_pct(now=dt, spot_now=mark_px, spot_at=spot_at)
    weekly = trailing_7d_weekly_pnl(
        db_path,
        now=dt,
        mark_value_now=mark_value if not empty else 0.0,
        cost_now=cost if not empty else 0.0,
    )
    if empty and weekly is None:
        weekly = 0.0

    return TreasurySnapshot(
        as_of=dt,
        holdings_btc=qty,
        cost_basis_usd=cost,
        avg_price_usd=avg,
        mark_btc_usd=mark_px,
        mark_value_usd=mark_value,
        total_pnl_usd=total_pnl,
        trend_30d_btc_spot_pct=trend,
        trend_30d_label="30-day BTC-USD spot",
        weekly_pnl_usd=weekly,
        weekly_pnl_label="Trailing 7-day treasury P/L",
        contribution_count=n,
        empty=empty,
    )
