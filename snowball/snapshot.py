from __future__ import annotations

from snowball.halt import halt_active, trading_enabled
from snowball.models import utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState
from snowball.watcher.store import event_to_dict
from snowball.yolo_demon.store import idea_to_dict
from snowball.stocks.snapshot import build_stocks_snapshot


def build_snapshot(state: AppState) -> dict:
    settings = state.settings
    now = utcnow()
    with state.lock:
        marks = state.marks()
        for pos in state.ledger.open_positions():
            marks.setdefault(pos.product, pos.entry_price)
        cash = state.ledger.cash_usd()
        equity = state.ledger.equity_usd(marks)
        utc_date, start_eq, killed = state.ledger.ensure_utc_day(now, equity)
        daily_pnl = equity - start_eq
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)
        positions = []
        for pos in state.ledger.open_positions():
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
        for fill in state.ledger.recent_fills(40):
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
        pause_rows = {p["product"]: p for p in state.ledger.list_pair_pauses(now)}
        for product in settings.product_list:
            snap = state.pairs.get(product)
            open_count = state.ledger.open_count(product)
            paused_until = state.ledger.pair_paused_until(product, now)
            pause_info = pause_rows.get(product)
            pairs.append(
                {
                    "product": product,
                    "last": snap.last if snap else None,
                    "bid": snap.bid if snap else None,
                    "ask": snap.ask if snap else None,
                    "sma_fast": snap.sma_fast if snap else None,
                    "sma_slow": snap.sma_slow if snap else None,
                    "signal": snap.signal if snap else "hold",
                    "candle_ts": snap.candle_ts.isoformat() if snap and snap.candle_ts else None,
                    "sma_fast_15m": snap.sma_fast if snap else None,
                    "sma_slow_15m": snap.sma_slow if snap else None,
                    "signal_15m": snap.signal if snap else "hold",
                    "candle_ts_15m": snap.candle_ts.isoformat() if snap and snap.candle_ts else None,
                    "sma_fast_5m": snap.sma_fast_5m if snap else None,
                    "sma_slow_5m": snap.sma_slow_5m if snap else None,
                    "signal_5m": snap.signal_5m if snap else "hold",
                    "candle_ts_5m": snap.candle_ts_5m.isoformat() if snap and snap.candle_ts_5m else None,
                    "sma_fast_1d": getattr(snap, "sma_fast_1d", None) if snap else None,
                    "sma_slow_1d": getattr(snap, "sma_slow_1d", None) if snap else None,
                    "signal_1d": getattr(snap, "signal_1d", "hold") if snap else "hold",
                    "ema_fast_15m": getattr(snap, "ema_fast_15m", None) if snap else None,
                    "ema_slow_15m": getattr(snap, "ema_slow_15m", None) if snap else None,
                    "signal_ema_15m": getattr(snap, "signal_ema_15m", "hold") if snap else "hold",
                    "donchian_high_1d": getattr(snap, "donchian_high_1d", None) if snap else None,
                    "donchian_low_1d": getattr(snap, "donchian_low_1d", None) if snap else None,
                    "signal_donchian_1d": getattr(snap, "signal_donchian_1d", "hold") if snap else "hold",
                    "open_count": open_count,
                    "max_open": settings.max_positions_per_pair,
                    "last_error": snap.last_error if snap else None,
                    "paused": paused_until is not None,
                    "paused_until": paused_until.isoformat() if paused_until else None,
                    "pause_reason": pause_info.get("reason") if pause_info else None,
                    "consecutive_losses": pause_info.get("consecutive_losses", 0) if pause_info else 0,
                }
            )
        block: list[str] = []
        if halted:
            block.append("halt_file")
        if not can_trade:
            block.append("trading_disabled")
        if killed:
            block.append("daily_loss_kill")
        if daily_loss_breached(equity, start_eq, settings.daily_loss_kill_usd) and not killed:
            block.append("daily_loss_kill_pending")

        scorecard = state.ledger.scorecard()
        pair_pauses = [p for p in pause_rows.values() if p.get("active")]

        return {
            "ts": now.isoformat(),
            "started_at": state.started_at.isoformat(),
            "last_tick_at": state.last_tick_at.isoformat() if state.last_tick_at else None,
            "last_error": state.last_error,
            "status": {
                "mode": settings.mode,
                "live_enabled": settings.live_enabled,
                "live_orders_permitted": settings.live_orders_permitted(),
                "paper": settings.mode == "paper" and not settings.live_enabled,
                "trading_enabled": can_trade,
                "halt_active": halted,
                "halt_file": str(settings.halt_file),
                "daily_killed": killed,
                "utc_date": utc_date,
                "running": state.running,
                "block_reasons": block,
                "strategies": settings.strategy_list,
            },
            "risk": {
                "bankroll_usd": settings.bankroll_usd,
                "cash_usd": cash,
                "equity_usd": equity,
                "daily_pnl_usd": daily_pnl,
                "daily_loss_kill_usd": settings.daily_loss_kill_usd,
                "start_of_day_equity": start_eq,
                "unrealized_pnl_usd": state.ledger.unrealized_pnl(marks),
                "max_positions_per_pair": settings.max_positions_per_pair,
                "max_position_notional_usd": settings.max_position_notional_usd,
                "open_positions": len(positions),
                "max_book_positions": settings.max_positions_per_pair * len(settings.product_list),
            },
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "scorecard": scorecard,
            "pair_pauses": pair_pauses,
            "watcher": _watcher_payload(state),
            "yolo_demon": _yolo_payload(state),
            "clerk": _clerk_payload(state),
            "stocks": build_stocks_snapshot(state),
        }


def _watcher_payload(state: AppState) -> dict:
    settings = state.settings
    sidecar = state.watcher_sidecar
    last_poll = getattr(sidecar, "last_poll_at", None) if sidecar is not None else None
    last_error = getattr(sidecar, "last_error", None) if sidecar is not None else None
    upcoming = []
    press = []
    rates = []
    if state.watcher is not None:
        upcoming = [event_to_dict(e) for e in state.watcher.upcoming_calendar(hours=48)]
        press = [event_to_dict(e) for e in state.watcher.latest_press(20)]
        rates = state.watcher.rates()
        last_poll = last_poll or state.watcher.get_meta("last_poll_at")
        last_error = last_error or state.watcher.get_meta("last_error")
    return {
        "id": "watcher",
        "name": "The Watcher",
        "enabled": settings.watcher_enabled,
        "te_configured": bool(settings.tradingeconomics_api_key.strip()),
        "fred_configured": bool(settings.fred_api_key.strip()),
        "halt_around_fomc": settings.watcher_halt_around_fomc,
        "last_poll_at": last_poll.isoformat() if hasattr(last_poll, "isoformat") else last_poll,
        "last_error": last_error,
        "upcoming_calendar": upcoming,
        "latest_press": press,
        "rates": rates,
    }


def _yolo_payload(state: AppState) -> dict:
    settings = state.settings
    sidecar = state.yolo_sidecar
    sources = {
        "youtube": "configured" if settings.youtube_api_key.strip() else "missing_key",
        "x": (
            "disabled_by_default"
            if not settings.x_enabled
            else ("configured" if settings.x_bearer_token.strip() else "missing_key")
        ),
    }
    if sidecar is not None and getattr(sidecar, "source_status", None):
        sources = {**sources, **dict(sidecar.source_status)}
    configured = any(
        [
            bool(settings.youtube_api_key.strip()),
            bool(settings.x_bearer_token.strip() and settings.x_enabled),
        ]
    )
    last_poll = getattr(sidecar, "last_poll_at", None) if sidecar is not None else None
    last_error = getattr(sidecar, "last_error", None) if sidecar is not None else None
    disabled_reason = getattr(sidecar, "disabled_reason", None) if sidecar is not None else None
    ideas = []
    priority_videos = []
    source_breakdown: dict[str, int] = {}
    x_reads_today = 0
    backfill_done = None
    backfill_counts = None
    if state.yolo is not None:
        ideas = [idea_to_dict(i) for i in state.yolo.top_ideas(20)]
        priority_videos = state.yolo.recent_videos(40)
        source_breakdown = state.yolo.source_counts()
        last_poll = last_poll or state.yolo.get_meta("last_poll_at")
        disabled_reason = disabled_reason or state.yolo.get_meta("disabled_reason")
        x_reads_today = state.yolo.x_reads_today()
        backfill_done = state.yolo.get_meta("yolo_youtube_backfill_done")
        backfill_counts = state.yolo.get_meta("yolo_youtube_backfill_counts")
    return {
        "id": "yolo_demon",
        "name": "Yolo Demon",
        "enabled": settings.yolo_demon_enabled,
        "configured": configured,
        "disabled_reason": disabled_reason,
        "label": "HIGH RISK / RESEARCH ONLY / DOES NOT TRADE",
        "last_poll_at": last_poll.isoformat() if hasattr(last_poll, "isoformat") else last_poll,
        "last_error": last_error,
        "ideas": ideas,
        "priority_videos": priority_videos,
        "youtube_backfill_done": backfill_done,
        "youtube_backfill_counts": backfill_counts,
        "sources": sources,
        "source_breakdown": source_breakdown,
        "youtube_channel_handles": settings.youtube_channel_handles,
        "x_enabled": settings.x_enabled,
        "x_daily_max_reads": settings.x_daily_max_reads,
        "x_reads_today": x_reads_today,
        "x_cost_note": "X is pay-per-use (~$0.005/post read); X_ENABLED defaults false",
    }


def _clerk_watchlist_labels() -> list:
    """Clerk sidecar is optional; strategy snapshot must not require it."""
    try:
        from snowball.clerk.watchlist import watchlist_labels
    except Exception:
        return []
    try:
        return watchlist_labels()
    except Exception:
        return []


def _clerk_payload(state: AppState) -> dict:
    settings = state.settings
    sidecar = getattr(state, "clerk_sidecar", None)
    last_poll = getattr(sidecar, "last_poll_at", None) if sidecar is not None else None
    last_error = getattr(sidecar, "last_error", None) if sidecar is not None else None
    counts = {
        "filings": 0,
        "transactions": 0,
        "watchlist_filings": 0,
        "watchlist_transactions": 0,
        "scanned_skip": 0,
    }
    recent: list = []
    store = getattr(state, "clerk", None)
    if store is not None:
        counts = store.counts()
        recent = store.recent_watchlist(20)
        last_poll = last_poll or store.get_meta("last_poll_at")
        stored_err = store.get_meta("last_error")
        last_error = last_error or stored_err or None
    poll_counts = getattr(sidecar, "last_counts", None) if sidecar is not None else None
    if isinstance(poll_counts, dict):
        counts = {
            **counts,
            "last_poll_pdfs": int(poll_counts.get("pdfs") or 0),
            "last_poll_transactions": int(poll_counts.get("transactions") or 0),
        }
    return {
        "id": "clerk",
        "name": "The Clerk",
        "enabled": bool(getattr(settings, "clerk_enabled", True)),
        "label": "RESEARCH ONLY / DOES NOT TRADE / NO ORDERS",
        "places_orders": False,
        "source": "https://disclosures-clerk.house.gov",
        "last_poll_at": last_poll.isoformat() if hasattr(last_poll, "isoformat") else last_poll,
        "last_error": last_error or None,
        "counts": counts,
        "recent_watchlist": recent,
        "watchlist": _clerk_watchlist_labels(),
        "poll_seconds": max(21600.0, float(getattr(settings, "clerk_poll_seconds", 21600.0))),
        "pdf_cap": int(getattr(settings, "clerk_pdf_cap", 25)),
    }

