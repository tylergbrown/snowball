"""Dashboard / API snapshot for Fed Desk."""

from __future__ import annotations

from typing import Any

from snowball.crash.triggers import short_unrealized_pnl_pct
from snowball.fed.market import DEFAULT_FED_PRODUCTS, normalize_futures_product
from snowball.fed.probs import BET_SKEW_THRESHOLD
from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState


def build_fed_snapshot(state: AppState) -> dict[str, Any]:
    settings = state.settings
    now = utcnow()
    store = getattr(state, "fed_ledger", None)
    if store is None or not getattr(settings, "fed_enabled", False):
        return {
            "enabled": False,
            "mode": "paper",
            "label": "Fed Desk (disabled)",
            "pairs": [],
            "positions": [],
            "fills": [],
            "risk": {},
            "products": [],
            "research": {},
            "bet_eligible": False,
        }

    with state.lock:
        marks: dict[str, float] = {}
        for product, snap in (state.fed_pairs or {}).items():
            if snap.last is not None:
                marks[product] = snap.last
        for pos in store.open_positions():
            marks.setdefault(pos.product, pos.entry_price)

        cash = store.cash_usd()
        equity = store.equity_usd(marks)
        utc_date, start_eq, killed = store.ensure_utc_day(now, equity)
        daily_pnl = equity - start_eq
        halted = halt_active(settings.halt_file)
        live = settings.fed_live_orders_permitted()
        can_trade = trading_enabled(settings) and (
            settings.fed_mode == "paper" or live
        )

        positions = []
        for pos in store.open_positions():
            mark = marks.get(pos.product, pos.entry_price)
            if pos.side == "short":
                u_pnl = (pos.entry_price - mark) * pos.qty
                pnl_pct = short_unrealized_pnl_pct(pos.entry_price, mark)
            else:
                u_pnl = (mark - pos.entry_price) * pos.qty
                pnl_pct = (
                    (mark - pos.entry_price) / pos.entry_price
                    if pos.entry_price
                    else None
                )
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
                    "pnl_pct": pnl_pct,
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
            for p in (settings.fed_product_list or list(DEFAULT_FED_PRODUCTS))
        ]
        pairs = []
        for product in products:
            snap = (state.fed_pairs or {}).get(product)
            pairs.append(
                {
                    "product": product,
                    "last": snap.last if snap else None,
                    "open_count": store.open_count(product),
                    "max_open": settings.fed_max_positions,
                    "last_error": snap.last_error if snap else None,
                    "mark_source": state.fed_mark_source,
                    "allotment_usd": state.fed_per_index_allotment_usd,
                }
            )

        research = dict(getattr(state, "fed_last_research", None) or {})
        if not research:
            research = store.last_research() or {}

        block: list[str] = []
        if settings.fed_mode == "live" and not settings.fed_live_enabled:
            block.append("fed_live_gate_incomplete")
        if settings.fed_mode != "live" and settings.fed_live_enabled:
            block.append("fed_live_gate_incomplete")
        if halted:
            block.append("halt_file")
        if not can_trade:
            block.append("trading_disabled")
        if killed:
            block.append("daily_loss_kill")
        if (
            daily_loss_breached(equity, start_eq, settings.fed_daily_loss_kill_usd)
            and not killed
        ):
            block.append("daily_loss_kill_pending")

        bet_eligible = bool(research.get("bet_eligible"))
        # Recompute eligibility against live open lots: if already in a bet, note it
        bet_status = getattr(state, "fed_bet_status", None) or research.get("direction") or "idle"
        if positions:
            bet_status = f"open_{positions[0].get('side')}"

        mode_label = "live" if live else "paper"
        label = (
            "Fed Desk — LIVE dual-gated; CFM US500/TECH; FOMC skew bets"
            if live
            else "Fed Desk — paper; research always on; dual gate off"
        )

        return {
            "enabled": True,
            "ts": now.isoformat(),
            "last_tick_at": state.fed_last_tick_at.isoformat()
            if state.fed_last_tick_at
            else None,
            "mode": mode_label,
            "fed_mode": settings.fed_mode,
            "fed_live_enabled": settings.fed_live_enabled,
            "label": label,
            "mark_source": state.fed_mark_source,
            "next_meeting_date": research.get("next_meeting_date"),
            "days_left": research.get("days_left"),
            "in_window": research.get("in_window"),
            "p_hold": research.get("p_hold"),
            "p_hike": research.get("p_hike"),
            "p_cut": research.get("p_cut"),
            "skew": research.get("skew"),
            "max_prob": research.get("max_prob"),
            "bet_eligible": bet_eligible,
            "direction": research.get("direction"),
            "bet_status": bet_status,
            "threshold": BET_SKEW_THRESHOLD,
            "effr": research.get("effr"),
            "current_target": research.get("current_target"),
            "watcher_halt_around_fomc": settings.watcher_halt_around_fomc,
            "status": {
                "halt_active": halted,
                "trading_enabled": can_trade,
                "daily_killed": killed,
                "utc_date": utc_date,
                "block_reasons": block,
                "live_orders_permitted": live,
                "strategy": "fed_desk",
                "bet_status": bet_status,
            },
            "risk": {
                "bankroll_usd": settings.fed_bankroll_usd,
                "cash_usd": cash,
                "equity_usd": equity,
                "daily_pnl_usd": daily_pnl,
                "daily_loss_kill_usd": settings.fed_daily_loss_kill_usd,
                "start_of_day_equity": start_eq,
                "unrealized_pnl_usd": store.unrealized_pnl(marks),
                "max_positions_per_pair": settings.fed_max_positions,
                "max_position_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.fed_account_value_usd or settings.fed_bankroll_usd)
                ),
                "effective_per_leg_notional_usd": settings.effective_per_leg_notional_usd(
                    float(state.fed_account_value_usd or settings.fed_bankroll_usd)
                ),
                "per_leg_base_usd": settings.per_leg_base_usd,
                "per_leg_autoscale": settings.per_leg_autoscale,
                "per_leg_scale_per_100_usd_pct": settings.per_leg_scale_per_100_usd_pct,
                "account_value_usd": state.fed_account_value_usd,
                "budget_pct": settings.fed_account_budget_pct,
                "budget_usd": state.fed_budget_usd,
                "per_index_allotment_usd": state.fed_per_index_allotment_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.fed_max_positions * max(1, len(products)),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "fee_buffer_pct": getattr(settings, "fee_buffer_pct", 0.0),
                "effective_take_profit_floor": settings.effective_min_take_profit_pct(),
                "never_sell_red": True,
                "never_cover_red": True,
                "cfm_max_contracts": settings.cfm_max_contracts,
                "cfm_leverage": settings.cfm_leverage,
                "cfm_margin_rate": settings.cfm_margin_rate,
                "venue": "CFM_CDE",
                "leverage": settings.cfm_leverage,
            },
            "products": products,
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "research": research,
            "scorecard": store.scorecard(),
        }
