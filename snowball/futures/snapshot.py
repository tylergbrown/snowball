"""Dashboard / API snapshot for the isolated Future Trader paper book."""

from __future__ import annotations

from typing import Any

from snowball.futures.market import DEFAULT_FUTURES_PRODUCTS, normalize_futures_product
from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState


def build_futures_snapshot(state: AppState) -> dict[str, Any]:
    settings = state.settings
    now = utcnow()
    ledger = state.futures_ledger
    if ledger is None or not settings.futures_enabled:
        return {
            "enabled": False,
            "mode": "paper",
            "label": "Future Trader (disabled)",
            "pairs": [],
            "positions": [],
            "fills": [],
            "risk": {},
            "products": [],
        }

    with state.lock:
        marks: dict[str, float] = {}
        for product, snap in state.futures_pairs.items():
            if snap.last is not None:
                marks[product] = snap.last
        for pos in ledger.open_positions():
            marks.setdefault(pos.product, pos.entry_price)

        cash = ledger.cash_usd()
        equity = ledger.equity_usd(marks)
        utc_date, start_eq, killed = ledger.ensure_utc_day(now, equity)
        daily_pnl = equity - start_eq
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings) and settings.futures_mode == "paper"

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

        products = [
            normalize_futures_product(p)
            for p in (settings.futures_product_list or list(DEFAULT_FUTURES_PRODUCTS))
        ]
        pairs = []
        pause_rows = {p["product"]: p for p in ledger.list_pair_pauses(now)}
        max_open = settings.futures_max_positions
        for product in products:
            snap = state.futures_pairs.get(product)
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
                    "sma_fast_1d": getattr(snap, "sma_fast_1d", None) if snap else None,
                    "sma_slow_1d": getattr(snap, "sma_slow_1d", None) if snap else None,
                    "signal_1d": getattr(snap, "signal_1d", "hold") if snap else "hold",
                    "donchian_high_1d": getattr(snap, "donchian_high_1d", None) if snap else None,
                    "donchian_low_1d": getattr(snap, "donchian_low_1d", None) if snap else None,
                    "signal_donchian_1d": getattr(snap, "signal_donchian_1d", "hold")
                    if snap
                    else "hold",
                    "open_count": ledger.open_count(product),
                    "max_open": max_open,
                    "last_error": snap.last_error if snap else None,
                    "paused": paused_until is not None,
                    "paused_until": paused_until.isoformat() if paused_until else None,
                    "pause_reason": pause_info.get("reason") if pause_info else None,
                    "mark_source": state.futures_mark_source,
                }
            )

        block: list[str] = []
        if settings.futures_mode != "paper":
            block.append("futures_mode_not_paper")
        if settings.futures_live_enabled:
            block.append("futures_live_enabled_refused_in_v1")
        if halted:
            block.append("halt_file")
        if not can_trade:
            block.append("trading_disabled")
        if killed:
            block.append("daily_loss_kill")
        if (
            daily_loss_breached(equity, start_eq, settings.futures_daily_loss_kill_usd)
            and not killed
        ):
            block.append("daily_loss_kill_pending")

        return {
            "enabled": True,
            "ts": now.isoformat(),
            "last_tick_at": state.futures_last_tick_at.isoformat()
            if state.futures_last_tick_at
            else None,
            "mode": "paper",
            "futures_mode": settings.futures_mode,
            "futures_live_enabled": settings.futures_live_enabled,
            "label": "Future Trader — Coinbase perps paper; isolated book; never live futures",
            "mark_source": state.futures_mark_source,
            "status": {
                "halt_active": halted,
                "trading_enabled": can_trade,
                "daily_killed": killed,
                "utc_date": utc_date,
                "block_reasons": block,
                "strategies": settings.futures_strategy_list,
            },
            "risk": {
                "bankroll_usd": settings.futures_bankroll_usd,
                "cash_usd": cash,
                "equity_usd": equity,
                "daily_pnl_usd": daily_pnl,
                "daily_loss_kill_usd": settings.futures_daily_loss_kill_usd,
                "start_of_day_equity": start_eq,
                "unrealized_pnl_usd": ledger.unrealized_pnl(marks),
                "max_positions_per_pair": settings.futures_max_positions,
                "max_position_notional_usd": settings.futures_max_notional_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.futures_max_positions * max(1, len(products)),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "never_sell_red": settings.never_sell_red,
            },
            "products": products,
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "scorecard": ledger.scorecard(),
            "pair_pauses": [p for p in pause_rows.values() if p.get("active")],
        }
