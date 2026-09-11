"""Dashboard / API snapshot for Crash Guard."""

from __future__ import annotations

from typing import Any

from snowball.crash.market import DEFAULT_CRASH_PRODUCTS, normalize_futures_product
from snowball.crash.triggers import short_unrealized_pnl_pct
from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState


def build_crash_snapshot(state: AppState) -> dict[str, Any]:
    settings = state.settings
    now = utcnow()
    store = getattr(state, "crash_ledger", None)
    if store is None or not getattr(settings, "crash_enabled", False):
        return {
            "enabled": False,
            "mode": "paper",
            "label": "Crash Guard (disabled)",
            "pairs": [],
            "positions": [],
            "fills": [],
            "risk": {},
            "products": [],
            "triggers": {},
        }

    with state.lock:
        marks: dict[str, float] = {}
        for product, snap in (state.crash_pairs or {}).items():
            if snap.last is not None:
                marks[product] = snap.last
        for pos in store.open_positions():
            marks.setdefault(pos.product, pos.entry_price)

        cash = store.cash_usd()
        equity = store.equity_usd(marks)
        utc_date, start_eq, killed = store.ensure_utc_day(now, equity)
        daily_pnl = equity - start_eq
        halted = halt_active(settings.halt_file)
        live = settings.crash_live_orders_permitted()
        can_trade = trading_enabled(settings) and (
            settings.crash_mode == "paper" or live
        )

        positions = []
        for pos in store.open_positions():
            mark = marks.get(pos.product, pos.entry_price)
            u_pnl = (pos.entry_price - mark) * pos.qty  # short PnL
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
                    "short_pnl_pct": short_unrealized_pnl_pct(pos.entry_price, mark),
                    "opened_at": pos.opened_at.isoformat(),
                    "strategy": pos.strategy,
                }
            )

        fills = []
        for fill in store.recent_fills(40):
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
            for p in (settings.crash_product_list or list(DEFAULT_CRASH_PRODUCTS))
        ]
        pairs = []
        max_open = settings.crash_max_positions
        triggers = dict(getattr(state, "crash_last_triggers", None) or {})
        for product in products:
            snap = (state.crash_pairs or {}).get(product)
            tr = triggers.get(product) or {}
            pairs.append(
                {
                    "product": product,
                    "last": snap.last if snap else None,
                    "rsi_1d": getattr(snap, "rsi_1d", None) if snap else None,
                    "bb_lower_1d": getattr(snap, "bb_lower_1d", None) if snap else None,
                    "bb_mid_1d": getattr(snap, "bb_mid_1d", None) if snap else None,
                    "bb_upper_1d": getattr(snap, "bb_upper_1d", None) if snap else None,
                    "candle_ts_1d": (
                        snap.candle_ts_1d.isoformat()
                        if snap and getattr(snap, "candle_ts_1d", None)
                        else None
                    ),
                    "open_count": store.open_count(product),
                    "max_open": max_open,
                    "last_error": snap.last_error if snap else None,
                    "mark_source": state.crash_mark_source,
                    "allotment_usd": state.crash_per_index_allotment_usd,
                    "trigger_fire": bool(tr.get("fire")),
                    "trigger_reasons": list(tr.get("reasons") or []),
                    "daily_return": tr.get("daily_return"),
                }
            )

        block: list[str] = []
        if settings.crash_mode == "live" and not settings.crash_live_enabled:
            block.append("crash_live_gate_incomplete")
        if settings.crash_mode != "live" and settings.crash_live_enabled:
            block.append("crash_live_gate_incomplete")
        if halted:
            block.append("halt_file")
        if not can_trade:
            block.append("trading_disabled")
        if killed:
            block.append("daily_loss_kill")
        if (
            daily_loss_breached(equity, start_eq, settings.crash_daily_loss_kill_usd)
            and not killed
        ):
            block.append("daily_loss_kill_pending")

        mode_label = "live" if live else "paper"
        label = (
            "Crash Guard — LIVE shorts (dual-gated); 10% budget 50/50 SPY/QQQ; never cover red"
            if live
            else "Crash Guard — paper shorts; isolated book; never cover red"
        )

        return {
            "enabled": True,
            "ts": now.isoformat(),
            "last_tick_at": state.crash_last_tick_at.isoformat()
            if state.crash_last_tick_at
            else None,
            "mode": mode_label,
            "crash_mode": settings.crash_mode,
            "crash_live_enabled": settings.crash_live_enabled,
            "label": label,
            "mark_source": state.crash_mark_source,
            "status": {
                "halt_active": halted,
                "trading_enabled": can_trade,
                "daily_killed": killed,
                "utc_date": utc_date,
                "block_reasons": block,
                "live_orders_permitted": live,
                "strategy": "crash_guard",
            },
            "risk": {
                "bankroll_usd": settings.crash_bankroll_usd,
                "cash_usd": cash,
                "equity_usd": equity,
                "daily_pnl_usd": daily_pnl,
                "daily_loss_kill_usd": settings.crash_daily_loss_kill_usd,
                "start_of_day_equity": start_eq,
                "unrealized_pnl_usd": store.unrealized_pnl(marks),
                "max_positions_per_pair": settings.crash_max_positions,
                "max_position_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.crash_account_value_usd or settings.crash_bankroll_usd)
                ),
                "effective_per_leg_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.crash_account_value_usd or settings.crash_bankroll_usd)
                ),
                "per_leg_base_usd": settings.per_leg_base_usd,
                "per_leg_autoscale": settings.per_leg_autoscale,
                "per_leg_scale_per_100_usd_pct": settings.per_leg_scale_per_100_usd_pct,
                "account_value_usd": state.crash_account_value_usd,
                "budget_pct": settings.crash_account_budget_pct,
                "budget_usd": state.crash_budget_usd,
                "per_index_allotment_usd": state.crash_per_index_allotment_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.crash_max_positions * max(1, len(products)),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "fee_buffer_pct": getattr(settings, "fee_buffer_pct", 0.0),
                "effective_take_profit_floor": settings.effective_min_take_profit_pct(),
                "never_cover_red": True,
                "leverage": 1.0,
            },
            "products": products,
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "triggers": triggers,
            "scorecard": store.scorecard(),
        }
