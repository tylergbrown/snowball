"""Treasury equity holdings (e.g. SPCX shares) — receive-only, never traded.

Stored in a separate ``equity_contributions`` table of snowball_treasury.db so the
BTC sums (holdings_and_cost / soft reserve / dividend) are untouched. Treasury
equity value is reported in the PDF treasury section only; it is never counted
as trading equity, bankroll, budget, or P/L.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Symbols held in the treasury. No Snowball lane may place an order on the
# spot/equity product for these (see snowball.live / gates).
class TreasuryHoldingRefused(RuntimeError):
    """Order refused: product is a treasury holding (receive-only)."""


TREASURY_EQUITY_SYMBOLS: frozenset[str] = frozenset({"SPCX"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    shares REAL NOT NULL,
    usd_cost REAL NOT NULL,
    price REAL NOT NULL,
    kind TEXT NOT NULL,
    source TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_contrib_sym ON equity_contributions(symbol);
CREATE TABLE IF NOT EXISTS equity_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL
);
"""


@dataclass(frozen=True)
class EquityHolding:
    symbol: str
    shares: float
    cost_usd: float
    price: float | None
    value_usd: float
    price_source: str
    contribution_count: int


def _base(product: str) -> str:
    p = (product or "").strip().upper().replace("/", "-")
    return p.split("-")[0]


def is_treasury_equity_spot(product: str) -> bool:
    """True for spot/equity products of a treasury symbol (e.g. SPCX, SPCX-USD).

    Futures/perp contracts are separate instruments and are not matched here.
    """
    p = (product or "").strip().upper()
    if "PERP" in p or "CDE" in p:
        return False
    return _base(p) in TREASURY_EQUITY_SYMBOLS


def ensure_equity_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def record_equity_contribution(
    db_path: Path, *, symbol: str, shares: float, usd_cost: float, price: float,
    kind: str = "external_deposit", source: str | None = None,
    note: str | None = None, ts: datetime | None = None,
) -> int:
    ensure_equity_schema(db_path)
    dt = ts or datetime.now(timezone.utc)
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            "INSERT INTO equity_contributions (ts, symbol, shares, usd_cost, price, kind, source, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dt.isoformat(), symbol.upper(), float(shares), float(usd_cost), float(price), kind, source, note),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _last_mark(con: sqlite3.Connection, symbol: str) -> float | None:
    row = con.execute(
        "SELECT price FROM equity_marks WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def fetch_equity_price(symbol: str) -> float | None:
    try:
        from snowball.stocks.market import YahooPaperMarket

        t = YahooPaperMarket(timeout=10.0).fetch_ticker(symbol)
        return float(t.last) if t.last and t.last > 0 else None
    except Exception:  # noqa: BLE001
        return None


def equity_holdings(
    db_path: Path, *, price_fn: Callable[[str], float | None] | None = None,
    write_mark: bool = True,
) -> list[EquityHolding]:
    if not Path(db_path).exists():
        return []
    ensure_equity_schema(db_path)
    fn = price_fn or fetch_equity_price
    con = sqlite3.connect(str(db_path))
    out: list[EquityHolding] = []
    try:
        rows = con.execute(
            "SELECT symbol, SUM(shares), SUM(usd_cost), COUNT(*), "
            "(SELECT price FROM equity_contributions e2 WHERE e2.symbol=e.symbol ORDER BY ts DESC LIMIT 1) "
            "FROM equity_contributions e GROUP BY symbol ORDER BY symbol"
        ).fetchall()
        for sym, shares, cost, n, contrib_px in rows:
            px = fn(sym)
            src = "live"
            if px is not None and px > 0 and write_mark:
                con.execute(
                    "INSERT INTO equity_marks (ts, symbol, price) VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), sym, float(px)),
                )
                con.commit()
            if px is None or px <= 0:
                px = _last_mark(con, sym)
                src = "last_mark"
            if px is None or px <= 0:
                px = float(contrib_px) if contrib_px else None
                src = "contribution_price"
            shares_f = float(shares or 0)
            out.append(EquityHolding(
                symbol=sym, shares=shares_f, cost_usd=float(cost or 0), price=px,
                value_usd=shares_f * px if px else 0.0, price_source=src,
                contribution_count=int(n or 0),
            ))
    finally:
        con.close()
    return out
