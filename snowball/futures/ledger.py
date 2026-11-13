"""Future Trader ledger: PaperLedger + side-aware SHORT support (2026-09-30).

The FT lane was long-only on US5/TEK. Crude oil (NOL) may now SHORT via
FUTURES_SHORT_PRODUCTS. Long lots behave exactly as PaperLedger (same schema,
same snowball_futures.db). Short lots follow CrashStore accounting: lock
notional as margin on open (sell fill), return margin + (entry-exit)*qty - fee
on cover (buy fill). qty is in UNDERLYING units (contracts * contract_size).
"""

from __future__ import annotations

from snowball.signal_context import attach_fill_context, ensure_fill_context_columns

from datetime import datetime

from snowball.models import Fill, Position, utcnow
from snowball.paper import PaperLedger, _parse_ts, _row_strategy


class FuturesLedger(PaperLedger):
    def equity_usd(self, marks: dict[str, float]) -> float:
        """Cash + long MTM (qty*mark) + short locked margin + short PnL."""
        total = self.cash_usd()
        for pos in self.open_positions():
            px = marks.get(pos.product, pos.entry_price)
            if pos.side == "short":
                total += pos.notional_usd + (pos.entry_price - px) * pos.qty
            else:
                total += pos.qty * px
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

    def open_fee_usd(self, position_id: int) -> float | None:
        """Fee recorded on the lot's OPENING fill (first fill of the position); None if unknown."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT fee_usd FROM fills WHERE position_id = ? ORDER BY id ASC LIMIT 1",
                    (int(position_id),),
                ).fetchone()
        except Exception:  # noqa: BLE001 — fee lookup must never break a close
            return None
        if row is None or row["fee_usd"] is None:
            return None
        return float(row["fee_usd"])

    def last_entry_at(self, product: str, strategy: str | None = None) -> datetime | None:
        """Last OPEN time (long or short). A short cover is a 'buy' fill, so the
        PaperLedger buy-fill query would mistake covers for entries."""
        with self._lock:
            sql = "SELECT opened_at FROM positions WHERE product = ?"
            args: list[object] = [product]
            if strategy is not None:
                sql += " AND strategy = ?"
                args.append(strategy)
            sql += " ORDER BY id DESC LIMIT 1"
            row = self._conn.execute(sql, args).fetchone()
            if row is None:
                return None
            return _parse_ts(row["opened_at"])

    def open_short(
        self,
        product: str,
        fill_px: float,
        notional_usd: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
        strategy: str = "momentum_15m",
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
            cur = self._conn
            cur.execute(
                "UPDATE account SET cash_usd = cash_usd - ?, updated_at = ? WHERE id = 1",
                (margin, ts.isoformat()),
            )
            cur.execute(
                """
                INSERT INTO positions
                    (product, side, qty, entry_price, notional_usd, opened_at, status, strategy)
                VALUES (?, 'short', ?, ?, ?, ?, 'open', ?)
                """,
                (product, qty, fill_px, notional_usd, ts.isoformat(), strategy),
            )
            pos_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])
            cur.execute(
                """
                INSERT INTO fills
                    (position_id, product, side, qty, price, notional_usd, fee_usd, slippage_bps, ts, reason, strategy)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pos_id, product, qty, fill_px, notional_usd, fee_usd, slippage_bps,
                 ts.isoformat(), reason, strategy),
            )
            fill_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])
            attach_fill_context(cur, fill_id)
            cur.commit()
            pos = Position(
                id=pos_id, product=product, side="short", qty=qty, entry_price=fill_px,
                notional_usd=notional_usd, opened_at=ts, status="open", strategy=strategy,
            )
            fill = Fill(
                id=fill_id, position_id=pos_id, product=product, side="sell", qty=qty,
                price=fill_px, notional_usd=notional_usd, fee_usd=fee_usd,
                slippage_bps=slippage_bps, ts=ts, reason=reason, strategy=strategy,
            )
            return pos, fill

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
                (position_id, row["product"], qty, fill_px, qty * fill_px, fee_usd,
                 slippage_bps, ts.isoformat(), reason, strategy),
            )
            fill_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            attach_fill_context(self._conn, fill_id)
            self._conn.commit()
            return Fill(
                id=fill_id, position_id=position_id, product=str(row["product"]), side="buy",
                qty=qty, price=fill_px, notional_usd=qty * fill_px, fee_usd=fee_usd,
                slippage_bps=slippage_bps, ts=ts, reason=reason, strategy=strategy,
            )

    def close_lot(
        self,
        position: Position,
        fill_px: float,
        slippage_bps: float,
        fee_usd: float,
        reason: str,
        ts: datetime | None = None,
    ) -> Fill:
        """Side-aware close: sell a long / cover a short."""
        if position.side == "short":
            return self.cover_short(position.id, fill_px, slippage_bps, fee_usd, reason, ts)
        return self.close_position(
            position_id=position.id, fill_px=fill_px, slippage_bps=slippage_bps,
            fee_usd=fee_usd, reason=reason, ts=ts,
        )
