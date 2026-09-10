"""Isolated Fed Desk ledger — long and short lots + research snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowball.models import Fill, Position, utcnow


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_strategy(row: sqlite3.Row) -> str:
    try:
        val = row["strategy"]
    except (IndexError, KeyError):
        return "fed_desk"
    return str(val) if val else "fed_desk"


class FedStore:
    """Sqlite book for Fed Desk directional bets + research meta."""

    def __init__(self, db_path: Path, bankroll_usd: float) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._bankroll = float(bankroll_usd)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._ensure_account()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_usd REAL NOT NULL,
                bankroll_usd REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                notional_usd REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at TEXT,
                exit_price REAL,
                realized_pnl REAL,
                strategy TEXT NOT NULL DEFAULT 'fed_desk',
                meeting_date TEXT,
                dominant TEXT,
                direction TEXT
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                product TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                notional_usd REAL NOT NULL,
                fee_usd REAL NOT NULL DEFAULT 0,
                slippage_bps REAL NOT NULL DEFAULT 0,
                ts TEXT NOT NULL,
                reason TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'fed_desk',
                FOREIGN KEY(position_id) REFERENCES positions(id)
            );
            CREATE TABLE IF NOT EXISTS daily_state (
                utc_date TEXT PRIMARY KEY,
                start_equity REAL NOT NULL,
                killed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                meeting_date TEXT,
                payload_json TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _ensure_account(self) -> None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
            if row is None:
                now = utcnow().isoformat()
                self._conn.execute(
                    "INSERT INTO account (id, cash_usd, bankroll_usd, updated_at) VALUES (1, ?, ?, ?)",
                    (self._bankroll, self._bankroll, now),
                )
                self._conn.commit()

    def cash_usd(self) -> float:
        with self._lock:
            row = self._conn.execute("SELECT cash_usd FROM account WHERE id = 1").fetchone()
            return float(row["cash_usd"])

    def set_cash_usd(self, cash_usd: float, *, ts: datetime | None = None) -> None:
        ts = ts or utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE account SET cash_usd = ?, updated_at = ? WHERE id = 1",
                (float(cash_usd), ts.isoformat()),
            )
            self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def save_research_snapshot(self, payload: dict[str, Any], *, meeting_date: str | None = None) -> None:
        ts = utcnow().isoformat()
        blob = json.dumps(payload, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO research_snapshots (ts, meeting_date, payload_json) VALUES (?, ?, ?)",
                (ts, meeting_date, blob),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("last_research_json", blob),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("last_research_at", ts),
            )
            self._conn.commit()

    def last_research(self) -> dict[str, Any] | None:
        raw = self.get_meta("last_research_json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def open_positions(self, product: str | None = None) -> list[Position]:
        with self._lock:
            sql = "SELECT * FROM positions WHERE status = 'open'"
            args: list[object] = []
            if product is not None:
                sql += " AND product = ?"
                args.append(product)
            sql += " ORDER BY id"
            rows = self._conn.execute(sql, args).fetchall()
            return [self._position(r) for r in rows]

    def open_count(self, product: str) -> int:
        return len(self.open_positions(product))

    def recent_fills(self, limit: int = 50) -> list[Fill]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._fill(r) for r in rows]

    def equity_usd(self, marks: dict[str, float]) -> float:
        cash = self.cash_usd()
        total = cash
        for pos in self.open_positions():
            px = marks.get(pos.product, pos.entry_price)
            if pos.side == "short":
                total += pos.notional_usd + (pos.entry_price - px) * pos.qty
            else:
                total += px * pos.qty
        return total

    def unrealized_pnl(self, marks: dict[str, float]) -> float:
        total = 0.0
        for pos in self.open_positions():
            px = marks.get(pos.product, pos.entry_price)
            if pos.side == "short":
                total += (pos.entry_price - px) * pos.qty
            else:
                total += (px - pos.entry_price) * pos.qty
        return total

    def ensure_utc_day(self, now: datetime, equity: float) -> tuple[str, float, bool]:
        utc_date = now.astimezone(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT start_equity, killed FROM daily_state WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO daily_state (utc_date, start_equity, killed) VALUES (?, ?, 0)",
                    (utc_date, equity),
                )
                self._conn.commit()
                return utc_date, equity, False
            return utc_date, float(row["start_equity"]), bool(row["killed"])

    def set_daily_killed(self, utc_date: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daily_state SET killed = 1 WHERE utc_date = ?",
                (utc_date,),
            )
            self._conn.commit()

    def open_long(
        self,
        product: str,
        fill_px: float,
        notional_usd: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
        strategy: str = "fed_desk",
        meeting_date: str | None = None,
        dominant: str | None = None,
        direction: str = "long",
    ) -> tuple[Position, Fill]:
        if fill_px <= 0 or notional_usd <= 0:
            raise ValueError("fill price and notional must be positive")
        ts = ts or utcnow()
        qty = notional_usd / fill_px
        cost = notional_usd + fee_usd
        with self._lock:
            cash = self.cash_usd()
            if cash + 1e-9 < cost:
                raise ValueError("insufficient cash for long")
            self._conn.execute(
                "UPDATE account SET cash_usd = cash_usd - ?, updated_at = ? WHERE id = 1",
                (cost, ts.isoformat()),
            )
            self._conn.execute(
                """
                INSERT INTO positions
                    (product, side, qty, entry_price, notional_usd, opened_at, status, strategy,
                     meeting_date, dominant, direction)
                VALUES (?, 'long', ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    product,
                    qty,
                    fill_px,
                    notional_usd,
                    ts.isoformat(),
                    strategy,
                    meeting_date,
                    dominant,
                    direction,
                ),
            )
            pos_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.execute(
                """
                INSERT INTO fills
                    (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps, ts, reason, strategy)
                VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos_id,
                    product,
                    qty,
                    fill_px,
                    notional_usd,
                    fee_usd,
                    slippage_bps,
                    ts.isoformat(),
                    reason,
                    strategy,
                ),
            )
            fill_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.commit()
            pos = Position(
                id=pos_id,
                product=product,
                side="long",
                qty=qty,
                entry_price=fill_px,
                notional_usd=notional_usd,
                opened_at=ts,
                status="open",
                strategy=strategy,
            )
            fill = Fill(
                id=fill_id,
                position_id=pos_id,
                product=product,
                side="buy",
                qty=qty,
                price=fill_px,
                notional_usd=notional_usd,
                fee_usd=fee_usd,
                slippage_bps=slippage_bps,
                ts=ts,
                reason=reason,
                strategy=strategy,
            )
            return pos, fill

    def open_short(
        self,
        product: str,
        fill_px: float,
        notional_usd: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
        strategy: str = "fed_desk",
        meeting_date: str | None = None,
        dominant: str | None = None,
        direction: str = "short",
    ) -> tuple[Position, Fill]:
        if fill_px <= 0 or notional_usd <= 0:
            raise ValueError("fill price and notional must be positive")
        ts = ts or utcnow()
        qty = notional_usd / fill_px
        margin = notional_usd + fee_usd
        with self._lock:
            cash = self.cash_usd()
            if cash + 1e-9 < margin:
                raise ValueError("insufficient cash for short margin")
            self._conn.execute(
                "UPDATE account SET cash_usd = cash_usd - ?, updated_at = ? WHERE id = 1",
                (margin, ts.isoformat()),
            )
            self._conn.execute(
                """
                INSERT INTO positions
                    (product, side, qty, entry_price, notional_usd, opened_at, status, strategy,
                     meeting_date, dominant, direction)
                VALUES (?, 'short', ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    product,
                    qty,
                    fill_px,
                    notional_usd,
                    ts.isoformat(),
                    strategy,
                    meeting_date,
                    dominant,
                    direction,
                ),
            )
            pos_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.execute(
                """
                INSERT INTO fills
                    (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps, ts, reason, strategy)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos_id,
                    product,
                    qty,
                    fill_px,
                    notional_usd,
                    fee_usd,
                    slippage_bps,
                    ts.isoformat(),
                    reason,
                    strategy,
                ),
            )
            fill_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.commit()
            pos = Position(
                id=pos_id,
                product=product,
                side="short",
                qty=qty,
                entry_price=fill_px,
                notional_usd=notional_usd,
                opened_at=ts,
                status="open",
                strategy=strategy,
            )
            fill = Fill(
                id=fill_id,
                position_id=pos_id,
                product=product,
                side="sell",
                qty=qty,
                price=fill_px,
                notional_usd=notional_usd,
                fee_usd=fee_usd,
                slippage_bps=slippage_bps,
                ts=ts,
                reason=reason,
                strategy=strategy,
            )
            return pos, fill

    def close_long(
        self,
        position_id: int,
        fill_px: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
    ) -> Fill:
        ts = ts or utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"position {position_id} not found")
            if row["status"] != "open":
                raise ValueError(f"position {position_id} is not open")
            if str(row["side"]) != "long":
                raise ValueError(f"position {position_id} is not a long")
            qty = float(row["qty"])
            entry = float(row["entry_price"])
            strategy = _row_strategy(row)
            proceeds = qty * fill_px - fee_usd
            realized = (fill_px - entry) * qty - fee_usd
            self._conn.execute(
                "UPDATE account SET cash_usd = cash_usd + ?, updated_at = ? WHERE id = 1",
                (proceeds, ts.isoformat()),
            )
            self._conn.execute(
                """
                UPDATE positions
                SET status = 'closed', closed_at = ?, exit_price = ?, realized_pnl = ?
                WHERE id = ?
                """,
                (ts.isoformat(), fill_px, realized, position_id),
            )
            self._conn.execute(
                """
                INSERT INTO fills
                    (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps, ts, reason, strategy)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    row["product"],
                    qty,
                    fill_px,
                    qty * fill_px,
                    fee_usd,
                    slippage_bps,
                    ts.isoformat(),
                    reason,
                    strategy,
                ),
            )
            fill_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.commit()
            return Fill(
                id=fill_id,
                position_id=position_id,
                product=str(row["product"]),
                side="sell",
                qty=qty,
                price=fill_px,
                notional_usd=qty * fill_px,
                fee_usd=fee_usd,
                slippage_bps=slippage_bps,
                ts=ts,
                reason=reason,
                strategy=strategy,
            )

    def cover_short(
        self,
        position_id: int,
        fill_px: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
    ) -> Fill:
        ts = ts or utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"position {position_id} not found")
            if row["status"] != "open":
                raise ValueError(f"position {position_id} is not open")
            if str(row["side"]) != "short":
                raise ValueError(f"position {position_id} is not a short")
            qty = float(row["qty"])
            entry = float(row["entry_price"])
            notional = float(row["notional_usd"])
            strategy = _row_strategy(row)
            realized = (entry - fill_px) * qty - fee_usd
            proceeds = notional + realized
            self._conn.execute(
                "UPDATE account SET cash_usd = cash_usd + ?, updated_at = ? WHERE id = 1",
                (proceeds, ts.isoformat()),
            )
            self._conn.execute(
                """
                UPDATE positions
                SET status = 'closed', closed_at = ?, exit_price = ?, realized_pnl = ?
                WHERE id = ?
                """,
                (ts.isoformat(), fill_px, realized, position_id),
            )
            self._conn.execute(
                """
                INSERT INTO fills
                    (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps, ts, reason, strategy)
                VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    row["product"],
                    qty,
                    fill_px,
                    qty * fill_px,
                    fee_usd,
                    slippage_bps,
                    ts.isoformat(),
                    reason,
                    strategy,
                ),
            )
            fill_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._conn.commit()
            return Fill(
                id=fill_id,
                position_id=position_id,
                product=str(row["product"]),
                side="buy",
                qty=qty,
                price=fill_px,
                notional_usd=qty * fill_px,
                fee_usd=fee_usd,
                slippage_bps=slippage_bps,
                ts=ts,
                reason=reason,
                strategy=strategy,
            )

    def closed_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'closed' ORDER BY id"
            ).fetchall()
            return [self._position(r) for r in rows]

    def scorecard(self) -> dict:
        closed = self.closed_positions()
        open_lots = self.open_positions()

        def _bucket(key_fn):
            keys: dict[str, dict] = {}
            for pos in closed:
                key = key_fn(pos)
                b = keys.setdefault(
                    key,
                    {"closed_trades": 0, "wins": 0, "realized_pnl": 0.0, "open_count": 0},
                )
                b["closed_trades"] += 1
                pnl = float(pos.realized_pnl or 0.0)
                b["realized_pnl"] += pnl
                if pnl > 0:
                    b["wins"] += 1
            for pos in open_lots:
                key = key_fn(pos)
                b = keys.setdefault(
                    key,
                    {"closed_trades": 0, "wins": 0, "realized_pnl": 0.0, "open_count": 0},
                )
                b["open_count"] += 1
            out = []
            for key, b in sorted(keys.items()):
                n = b["closed_trades"]
                out.append(
                    {
                        "key": key,
                        "closed_trades": n,
                        "wins": b["wins"],
                        "win_rate": (b["wins"] / n) if n else None,
                        "realized_pnl": b["realized_pnl"],
                        "open_count": b["open_count"],
                    }
                )
            return out

        by_strategy = _bucket(lambda p: p.strategy or "fed_desk")
        by_product = _bucket(lambda p: p.product)
        for row in by_strategy:
            row["strategy"] = row.pop("key")
        for row in by_product:
            row["product"] = row.pop("key")
        return {"by_strategy": by_strategy, "by_product": by_product}

    @staticmethod
    def _position(row: sqlite3.Row) -> Position:
        return Position(
            id=int(row["id"]),
            product=str(row["product"]),
            side=str(row["side"]),
            qty=float(row["qty"]),
            entry_price=float(row["entry_price"]),
            notional_usd=float(row["notional_usd"]),
            opened_at=_parse_ts(row["opened_at"]),
            status=str(row["status"]),
            closed_at=_parse_ts(row["closed_at"]) if row["closed_at"] else None,
            exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
            realized_pnl=float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
            strategy=_row_strategy(row),
        )

    @staticmethod
    def _fill(row: sqlite3.Row) -> Fill:
        return Fill(
            id=int(row["id"]),
            position_id=int(row["position_id"]) if row["position_id"] is not None else None,
            product=str(row["product"]),
            side=str(row["side"]),
            qty=float(row["qty"]),
            price=float(row["price"]),
            notional_usd=float(row["notional_usd"]),
            fee_usd=float(row["fee_usd"]),
            slippage_bps=float(row["slippage_bps"]),
            ts=_parse_ts(row["ts"]),
            reason=str(row["reason"]),
            strategy=_row_strategy(row),
        )
