"""Dashboard / API snapshot for the isolated STOCK PAPER book."""

from __future__ import annotations

from typing import Any

from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState


def build_stocks_snapshot(state: AppState) -> dict[str, Any]:
    settings = state.settings
    now = utcnow()
    ledger = state.stock_ledger
    if ledger is None or not settings.stock_enabled:
        return {
            "enabled": False,
            "mode": "paper",
            "label": "STOCK PAPER (disabled)",
            "pairs": [],
            "positions": [],
            "fills": [],
            "risk": {},
            "universe": {},
        }

    with state.lock:
        marks: dict[str, float] = {}
        for product, snap in state.stock_pairs.items():
            if snap.last is not None:
                marks[product] = snap.last
        for pos in ledger.open_positions():
            marks.setdefault(pos.product, pos.entry_price)

        cash = ledger.cash_usd()
        equity = ledger.equity_usd(marks)
        utc_date, start_eq, killed = ledger.ensure_utc_day(now, equity)
        daily_pnl = equity - start_eq
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings) and settings.stock_mode == "paper"

        positions = []
        for pos in ledger.open_positions():
            mark = marks.get(pos.product, pos.entry_price)
            u_pnl = (mark - pos.entry_price) * pos.qty
            positions.append(
                {
                    "id": pos.id,
                    "product": pos.product,
                    "side": pos.side,
                    "qty": pos.qty,
                    "entry_price": pos.entry_price,
                    "mark": mark,
                    "notional_usd": pos.notional_usd,
                    "unrealized_pnl": u_pnl,
                    "opened_at": pos.opened_at.isoformat(),
                    "strategy": pos.strategy,
                }
            )

        fills = []
        for fill in ledger.recent_fills(40):
            fills.append(
                {
                    "id": fill.id,
                    "position_id": fill.position_id,
                    "product": fill.product,
                    "side": fill.side,
                    "qty": fill.qty,
                    "price": fill.price,
                    "notional_usd": fill.notional_usd,
                    "reason": fill.reason,
                    "ts": fill.ts.isoformat(),
                    "strategy": fill.strategy,
                }
            )

        pairs = []
        pause_rows = {p["product"]: p for p in ledger.list_pair_pauses(now)}
        max_open = settings.stock_max_positions
        for product in state.stock_universe_active or list(state.stock_pairs.keys()):
            snap = state.stock_pairs.get(product)
            paused_until = ledger.pair_paused_until(product, now)
            pause_info = pause_rows.get(product)
            pairs.append(
                {
                    "product": product,
                    "last": snap.last if snap else None,
                    "sma_fast": snap.sma_fast if snap else None,
                    "sma_slow": snap.sma_slow if snap else None,
                    "signal": snap.signal if snap else "hold",
                    "candle_ts": snap.candle_ts.isoformat() if snap and snap.candle_ts else None,
                    "sma_fast_15m": snap.sma_fast if snap else None,
                    "sma_slow_15m": snap.sma_slow if snap else None,
                    "signal_15m": snap.signal if snap else "hold",
                    "sma_fast_5m": snap.sma_fast_5m if snap else None,
                    "sma_slow_5m": snap.sma_slow_5m if snap else None,
                    "signal_5m": snap.signal_5m if snap else "hold",
                    "sma_fast_1d": getattr(snap, "sma_fast_1d", None) if snap else None,
                    "sma_slow_1d": getattr(snap, "sma_slow_1d", None) if snap else None,
                    "signal_1d": getattr(snap, "signal_1d", "hold") if snap else "hold",
                    "open_count": ledger.open_count(product),
                    "max_open": max_open,
                    "last_error": snap.last_error if snap else None,
                    "paused": paused_until is not None,
                    "paused_until": paused_until.isoformat() if paused_until else None,
                    "pause_reason": pause_info.get("reason") if pause_info else None,
                    "mark_source": state.stock_mark_source,
                }
            )

        block: list[str] = []
        if settings.stock_mode != "paper":
            block.append("stock_mode_not_paper")
        if halted:
            block.append("halt_file")
        if not can_trade:
            block.append("trading_disabled")
        if killed:
            block.append("daily_loss_kill")
        if daily_loss_breached(equity, start_eq, settings.stock_daily_loss_kill_usd) and not killed:
            block.append("daily_loss_kill_pending")

        return {
            "enabled": True,
            "ts": now.isoformat(),
            "last_tick_at": state.stock_last_tick_at.isoformat() if state.stock_last_tick_at else None,
            "mode": "paper",
            "stock_mode": settings.stock_mode,
            "label": "STOCK PAPER — Yahoo marks; isolated book; never live stock orders",
            "mark_source": state.stock_mark_source,
            "status": {
                "halt_active": halted,
                "trading_enabled": can_trade,
                "daily_killed": killed,
                "utc_date": utc_date,
                "block_reasons": block,
                "strategies": settings.stock_strategy_list,
            },
            "risk": {
                "bankroll_usd": settings.stock_bankroll_usd,
                "cash_usd": cash,
                "equity_usd": equity,
                "daily_pnl_usd": daily_pnl,
                "daily_loss_kill_usd": settings.stock_daily_loss_kill_usd,
                "start_of_day_equity": start_eq,
                "unrealized_pnl_usd": ledger.unrealized_pnl(marks),
                "max_positions_per_pair": settings.stock_max_positions,
                "max_position_notional_usd": settings.stock_max_notional_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.stock_max_positions
                * max(1, len(state.stock_universe_active or [])),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "never_sell_red": settings.never_sell_red,
            },
            "universe": {
                "active": list(state.stock_universe_active or []),
                "all": list(state.stock_universe_all or []),
                "dynamic": list(state.stock_universe_dynamic or []),
                "sources": state.stock_universe_sources or {},
            },
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "scorecard": ledger.scorecard(),
            "pair_pauses": [p for p in pause_rows.values() if p.get("active")],
            "coinbase_equity_ids": state.stock_coinbase_ids or {},
        }
