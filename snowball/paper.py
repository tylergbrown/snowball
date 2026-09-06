from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

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
        return "sma_15m"
    return str(val) if val else "sma_15m"


class PaperLedger:
    """Local fill simulator + sqlite persistence. Restarts keep cash, lots, fills."""

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
        self._migrate()
        self._ensure_account()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        cur = self._conn
        cur.executescript(
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
                side TEXT NOT NULL DEFAULT 'long',
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                notional_usd REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at TEXT,
                exit_price REAL,
                realized_pnl REAL,
                strategy TEXT NOT NULL DEFAULT 'sma_15m'
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
                slippage_bps REAL NOT NULL,
                ts TEXT NOT NULL,
                reason TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'sma_15m',
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
            CREATE TABLE IF NOT EXISTS pair_pause (
                product TEXT PRIMARY KEY,
                paused_until TEXT,
                reason TEXT,
                consecutive_losses INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_positions_open
                ON positions(product, status);
            CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
            """
        )
        cur.commit()

    def _columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    def _ensure_column(self, table: str, name: str, ddl: str) -> None:
        if name not in self._columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate(self) -> None:
        """Add strategy column to existing DBs; old rows default to sma_15m."""
        self._ensure_column("positions", "strategy", "TEXT NOT NULL DEFAULT 'sma_15m'")
        self._ensure_column("fills", "strategy", "TEXT NOT NULL DEFAULT 'sma_15m'")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(product, strategy, status)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pair_pause (
                product TEXT PRIMARY KEY,
                paused_until TEXT,
                reason TEXT,
                consecutive_losses INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    def _ensure_account(self) -> None:
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
        """Overwrite free cash (used to sync live exchange USD into the ledger)."""
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

    def last_entry_at(self, product: str, strategy: str | None = None) -> datetime | None:
        with self._lock:
            if strategy is None:
                row = self._conn.execute(
                    """
                    SELECT ts FROM fills
                    WHERE product = ? AND side = 'buy'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (product,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT ts FROM fills
                    WHERE product = ? AND side = 'buy' AND strategy = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (product, strategy),
                ).fetchone()
            if row is None:
                return None
            return _parse_ts(row["ts"])

    def open_positions(
        self, product: str | None = None, strategy: str | None = None
    ) -> list[Position]:
        with self._lock:
            sql = "SELECT * FROM positions WHERE status = 'open'"
            args: list[object] = []
            if product is not None:
                sql += " AND product = ?"
                args.append(product)
            if strategy is not None:
                sql += " AND strategy = ?"
                args.append(strategy)
            sql += " ORDER BY id"
            rows = self._conn.execute(sql, args).fetchall()
            return [self._position(r) for r in rows]

    def open_count(self, product: str, strategy: str | None = None) -> int:
        return len(self.open_positions(product, strategy=strategy))

    def recent_fills(self, limit: int = 50) -> list[Fill]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._fill(r) for r in rows]

    def equity_usd(self, marks: dict[str, float]) -> float:
        """Cash + mark-to-market of open lots."""
        cash = self.cash_usd()
        total = cash
        for pos in self.open_positions():
            px = marks.get(pos.product, pos.entry_price)
            total += pos.qty * px
        return total

    def unrealized_pnl(self, marks: dict[str, float]) -> float:
        total = 0.0
        for pos in self.open_positions():
            px = marks.get(pos.product, pos.entry_price)
            total += (px - pos.entry_price) * pos.qty
        return total

    def ensure_utc_day(self, now: datetime, equity: float) -> tuple[str, float, bool]:
        """Return (utc_date, start_equity, killed). Creates the row on first tick of the day."""
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

    def is_daily_killed(self, now: datetime) -> bool:
        utc_date = now.astimezone(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT killed FROM daily_state WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            return bool(row["killed"]) if row else False

    def open_buy(
        self,
        product: str,
        fill_px: float,
        notional_usd: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
        strategy: str = "sma_15m",
    ) -> tuple[Position, Fill]:
        if fill_px <= 0 or notional_usd <= 0:
            raise ValueError("fill price and notional must be positive")
        ts = ts or utcnow()
        qty = notional_usd / fill_px
        cost = qty * fill_px + fee_usd
        with self._lock:
            cash = self.cash_usd()
            if cash + 1e-9 < cost:
                raise ValueError("insufficient cash")
            cur = self._conn
            cur.execute(
                "UPDATE account SET cash_usd = cash_usd - ?, updated_at = ? WHERE id = 1",
                (cost, ts.isoformat()),
            )
            cur.execute(
                """
                INSERT INTO positions
                    (product, side, qty, entry_price, notional_usd, opened_at, status, strategy)
                VALUES (?, 'long', ?, ?, ?, ?, 'open', ?)
                """,
                (product, qty, fill_px, notional_usd, ts.isoformat(), strategy),
            )
            pos_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])
            cur.execute(
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
            fill_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])
            cur.commit()
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

    def close_position(
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


    def closed_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'closed' ORDER BY id"
            ).fetchall()
            return [self._position(r) for r in rows]

    def scorecard(self) -> dict:
        """Aggregate closed-trade stats per strategy and per product (measurement only)."""
        closed = self.closed_positions()
        open_lots = self.open_positions()

        def _bucket(key_fn):
            keys: dict[str, dict] = {}
            for pos in closed:
                key = key_fn(pos)
                b = keys.setdefault(
                    key,
                    {
                        "closed_trades": 0,
                        "wins": 0,
                        "realized_pnl": 0.0,
                        "open_count": 0,
                    },
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
                    {
                        "closed_trades": 0,
                        "wins": 0,
                        "realized_pnl": 0.0,
                        "open_count": 0,
                    },
                )
                b["open_count"] += 1
            out = []
            for key, b in sorted(keys.items()):
                n = b["closed_trades"]
                win_rate = (b["wins"] / n) if n else None
                out.append(
                    {
                        "key": key,
                        "closed_trades": n,
                        "wins": b["wins"],
                        "win_rate": win_rate,
                        "realized_pnl": b["realized_pnl"],
                        "open_count": b["open_count"],
                    }
                )
            return out

        by_strategy = _bucket(lambda p: p.strategy or "sma_15m")
        by_product = _bucket(lambda p: p.product)
        # Rename key field for clarity in payload
        for row in by_strategy:
            row["strategy"] = row.pop("key")
        for row in by_product:
            row["product"] = row.pop("key")
        return {"by_strategy": by_strategy, "by_product": by_product}

    def _pause_row(self, product: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM pair_pause WHERE product = ?", (product,)
        ).fetchone()

    def consecutive_losses(self, product: str) -> int:
        with self._lock:
            row = self._pause_row(product)
            return int(row["consecutive_losses"]) if row else 0

    def pair_paused_until(self, product: str, now: datetime | None = None) -> datetime | None:
        """Return active pause expiry, or None if not paused / expired."""
        now = now or utcnow()
        with self._lock:
            row = self._pause_row(product)
            if row is None or not row["paused_until"]:
                return None
            until = _parse_ts(row["paused_until"])
            if until <= now:
                return None
            return until

    def is_pair_paused(self, product: str, now: datetime | None = None) -> bool:
        return self.pair_paused_until(product, now) is not None

    def list_pair_pauses(self, now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM pair_pause ORDER BY product").fetchall()
            out = []
            for row in rows:
                until = _parse_ts(row["paused_until"]) if row["paused_until"] else None
                active = until is not None and until > now
                out.append(
                    {
                        "product": str(row["product"]),
                        "paused_until": until.isoformat() if until else None,
                        "reason": row["reason"],
                        "consecutive_losses": int(row["consecutive_losses"]),
                        "active": active,
                    }
                )
            return out

    def clear_pair_pause(self, product: str) -> bool:
        """Manual clear: wipe pause expiry and consecutive loss streak for product."""
        with self._lock:
            row = self._pause_row(product)
            if row is None:
                return False
            self._conn.execute(
                """
                UPDATE pair_pause
                SET paused_until = NULL, reason = NULL, consecutive_losses = 0
                WHERE product = ?
                """,
                (product,),
            )
            self._conn.commit()
            return True

    def record_closed_trade_for_pause(
        self,
        product: str,
        realized_pnl: float,
        *,
        now: datetime | None = None,
        enabled: bool = True,
        loss_threshold: int = 3,
        pause_hours: float = 24.0,
    ) -> dict | None:
        """Update consecutive losses; auto-pause product when threshold hit.

        Returns the pause row dict if a new pause was applied, else None.
        """
        now = now or utcnow()
        with self._lock:
            row = self._pause_row(product)
            streak = int(row["consecutive_losses"]) if row else 0
            if realized_pnl < 0:
                streak += 1
            else:
                streak = 0
            paused_until = row["paused_until"] if row else None
            reason = row["reason"] if row else None
            newly_paused = False
            if enabled and realized_pnl < 0 and streak >= max(1, int(loss_threshold)):
                # Only extend/set if not already actively paused
                active = False
                if paused_until:
                    try:
                        active = _parse_ts(paused_until) > now
                    except Exception:
                        active = False
                if not active:
                    until = now.timestamp() + float(pause_hours) * 3600.0
                    paused_until = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
                    reason = f"consecutive_losses:{streak}"
                    newly_paused = True
            self._conn.execute(
                """
                INSERT INTO pair_pause (product, paused_until, reason, consecutive_losses)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(product) DO UPDATE SET
                    paused_until = excluded.paused_until,
                    reason = excluded.reason,
                    consecutive_losses = excluded.consecutive_losses
                """,
                (product, paused_until, reason, streak),
            )
            self._conn.commit()
            if newly_paused:
                return {
                    "product": product,
                    "paused_until": paused_until,
                    "reason": reason,
                    "consecutive_losses": streak,
                }
            return None

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
