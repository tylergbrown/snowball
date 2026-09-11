"""Dashboard / API snapshot for the isolated Future Trader book."""

from __future__ import annotations

from typing import Any

from snowball.futures.market import DEFAULT_FUTURES_PRODUCTS, normalize_futures_product
from snowball.futures.session import session_times_from_settings
from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState


def _fills_by_strategy(fills: list[dict[str, Any]], ledger: Any) -> dict[str, Any]:
    """Split FT fills/PnL for dashboard + PDF (session_day vs momentum_15m)."""
    out: dict[str, Any] = {}
    for f in fills:
        sid = str(f.get("strategy") or "unknown")
        bucket = out.setdefault(sid, {"fills": 0, "buy": 0, "sell": 0})
        bucket["fills"] += 1
        side = str(f.get("side") or "").lower()
        if side == "buy":
            bucket["buy"] += 1
        elif side == "sell":
            bucket["sell"] += 1
    try:
        sc = ledger.scorecard() or {}
        for row in sc.get("by_strategy") or []:
            sid = str(row.get("strategy") or "unknown")
            bucket = out.setdefault(sid, {"fills": 0, "buy": 0, "sell": 0})
            bucket["closed_trades"] = row.get("trades") or row.get("n") or row.get("count")
            bucket["realized_pnl"] = row.get("realized_pnl") or row.get("pnl")
    except Exception:  # noqa: BLE001
        pass
    return out


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
        live = settings.futures_live_orders_permitted()
        can_trade = trading_enabled(settings) and (
            settings.futures_mode == "paper" or live
        )

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
                    "session_state": (state.futures_session_states or {}).get(
                        pos.product, "open_today"
                    ),
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
            sess = (state.futures_session_states or {}).get(product)
            if sess is None:
                from snowball.futures.session import classify_session_state

                sess = classify_session_state(
                    open_lots=ledger.open_positions(product), now=now
                )
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
                    "session_state": sess,
                    "allotment_usd": state.futures_per_index_allotment_usd,
                }
            )

        block: list[str] = []
        if settings.futures_mode == "live" and not settings.futures_live_enabled:
            block.append("futures_live_gate_incomplete")
        if settings.futures_mode != "live" and settings.futures_live_enabled:
            block.append("futures_live_gate_incomplete")
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

        times = session_times_from_settings(settings)
        mode_label = "live" if live else "paper"
        label = (
            "Future Trader — session+momentum LIVE (dual-gated); CFM US500/TECH ~30%; never-sell-red"
            if live
            else "Future Trader — session+momentum paper; isolated book (~30% budget)"
        )

        return {
            "enabled": True,
            "ts": now.isoformat(),
            "last_tick_at": state.futures_last_tick_at.isoformat()
            if state.futures_last_tick_at
            else None,
            "mode": mode_label,
            "futures_mode": settings.futures_mode,
            "futures_live_enabled": settings.futures_live_enabled,
            "label": label,
            "mark_source": state.futures_mark_source,
            "session": {
                "timezone": "America/New_York",
                "entry_window_et": f"{times['entry_start'].strftime('%H:%M')}–{times['entry_end'].strftime('%H:%M')} (late catch-up until exit unless momentum shares lane)",
                "exit_window_et": f"{times['exit_start'].strftime('%H:%M')}–{times['exit_end'].strftime('%H:%M')}",
                "states": dict(state.futures_session_states or {}),
                "weekday_only": True,
                "holiday_calendar": False,
                "overnight_hold_when_red": True,
            },
            "momentum": {
                "enabled": settings.futures_uses_momentum(),
                "strategy": "momentum_15m",
                "timeframe": getattr(settings, "futures_momentum_timeframe", "15m"),
                "lookback_bars": getattr(settings, "futures_momentum_lookback_bars", 8),
                "min_momentum_pct": getattr(settings, "futures_momentum_min_pct", 0.003),
                "take_profit_pct": getattr(settings, "futures_momentum_take_profit_pct", 0.008),
                "stall_exit_enabled": getattr(settings, "futures_momentum_stall_exit_enabled", True),
                "stall_lookback_bars": getattr(settings, "futures_momentum_stall_lookback_bars", 4),
                "stall_exit_pct": getattr(settings, "futures_momentum_stall_exit_pct", 0.004),
                "cash_hours_et": "09:30–15:45",
                "shared_budget_with_session": True,
            },
            "daily_pnl_target_usd": getattr(settings, "futures_daily_pnl_target_usd", 100.0),
            "status": {
                "halt_active": halted,
                "trading_enabled": can_trade,
                "daily_killed": killed,
                "utc_date": utc_date,
                "block_reasons": block,
                "strategies": settings.futures_strategy_list,
                "live_orders_permitted": live,
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
                "max_position_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.futures_account_value_usd or settings.futures_bankroll_usd)
                ),
                "effective_per_leg_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.futures_account_value_usd or settings.futures_bankroll_usd)
                ),
                "per_leg_base_usd": settings.per_leg_base_usd,
                "per_leg_autoscale": settings.per_leg_autoscale,
                "per_leg_scale_per_100_usd_pct": settings.per_leg_scale_per_100_usd_pct,
                "account_value_usd": state.futures_account_value_usd,
                "budget_pct": settings.futures_account_budget_pct,
                "budget_usd": state.futures_budget_usd,
                "per_index_allotment_usd": state.futures_per_index_allotment_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.futures_max_positions * max(1, len(products)),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "fee_buffer_pct": getattr(settings, "fee_buffer_pct", 0.0),
                "effective_take_profit_floor": settings.effective_min_take_profit_pct(),
                "sma_min_take_profit_pct": getattr(settings, "sma_min_take_profit_pct", 0.08),
                "effective_sma_take_profit_floor": settings.effective_min_take_profit_pct_for("sma_15m"),
                "never_sell_red": settings.never_sell_red,
                "cfm_max_contracts": settings.cfm_max_contracts,
                "cfm_leverage": settings.cfm_leverage,
                "cfm_margin_rate": settings.cfm_margin_rate,
                "venue": "CFM_CDE",
            },
            "products": products,
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "fills_by_strategy": _fills_by_strategy(fills, ledger),
            "scorecard": ledger.scorecard(),
            "pair_pauses": [p for p in pause_rows.values() if p.get("active")],
        }
