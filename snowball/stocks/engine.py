"""STOCK PAPER engine — separate sqlite book; never places live equity orders."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from snowball.config import Settings
from snowball.gates import (
    is_emergency_flatten_reason,
    lot_unrealized_pnl_pct,
    momentum_fading,
    scale_in_allowed,
    strategy_exit_allowed,
    trend_filter_allows,
)
from snowball.halt import halt_active, trading_enabled
from snowball.market import fill_price
from snowball.models import PairSnapshot, Position, Signal, Ticker, utcnow
from snowball.paper import PaperLedger
from snowball.risk import RiskContext, allow_entry, allow_exit, daily_loss_breached
from snowball.state import AppState
from snowball.stocks.market import YahooPaperMarket, resolve_coinbase_equity_ids
from snowball.stocks.universe import build_stock_universe
from snowball.strategy import crossover_signal, sma

log = logging.getLogger("snowball.stocks.engine")


def _stock_risk_ctx(
    settings: Settings,
    *,
    now: datetime,
    halted: bool,
    can_trade: bool,
    daily_killed: bool,
    open_count_for_pair: int,
    last_entry_at: datetime | None,
    cash_usd: float,
    requested_notional: float,
    cooldown_seconds: int,
) -> RiskContext:
    """Always paper + live_enabled=False so crypto live flags cannot leak into stocks."""
    return RiskContext(
        now=now,
        trading_enabled=can_trade,
        halt_active=halted,
        daily_killed=daily_killed,
        open_count_for_pair=open_count_for_pair,
        last_entry_at=last_entry_at,
        cash_usd=cash_usd,
        requested_notional=requested_notional,
        max_positions_per_pair=settings.stock_max_positions,
        max_position_notional_usd=settings.stock_max_notional_usd,
        entry_cooldown=timedelta(seconds=cooldown_seconds),
        mode="paper",
        live_enabled=False,
    )


def _sma_slow(snap: PairSnapshot, strategy_id: str) -> float | None:
    if strategy_id == "sma_5m":
        return snap.sma_slow_5m
    if strategy_id == "sma_1d":
        return getattr(snap, "sma_slow_1d", None) or snap.sma_slow
    return snap.sma_slow


def _sma_fast(snap: PairSnapshot, strategy_id: str) -> float | None:
    if strategy_id == "sma_5m":
        return snap.sma_fast_5m
    if strategy_id == "sma_1d":
        return getattr(snap, "sma_fast_1d", None) or snap.sma_fast
    return snap.sma_fast


def _signal_for(snap: PairSnapshot, strategy_id: str) -> tuple[Signal, bool]:
    if strategy_id == "sma_5m":
        up = (
            snap.sma_fast_5m is not None
            and snap.sma_slow_5m is not None
            and snap.sma_fast_5m > snap.sma_slow_5m
        )
        return Signal(snap.signal_5m), up
    if strategy_id == "sma_1d":
        fast = getattr(snap, "sma_fast_1d", None)
        slow = getattr(snap, "sma_slow_1d", None)
        sig = getattr(snap, "signal_1d", Signal.HOLD.value)
        up = fast is not None and slow is not None and fast > slow
        return Signal(sig), up
    up = snap.sma_fast is not None and snap.sma_slow is not None and snap.sma_fast > snap.sma_slow
    return Signal(snap.signal), up


class StockPaperEngine:
    """Long-only SMA paper lane on equities. Isolated ledger. No live orders."""

    def __init__(self, state: AppState, market: YahooPaperMarket | None = None) -> None:
        self.state = state
        self.market = market or YahooPaperMarket()
        self._last_universe_refresh = 0.0
        self._universe_refresh_seconds = 300.0

    def refresh_universe(self, *, force: bool = False) -> None:
        settings = self.state.settings
        now_m = time.monotonic()
        if (
            not force
            and self._last_universe_refresh
            and now_m - self._last_universe_refresh < self._universe_refresh_seconds
        ):
            return
        meta = build_stock_universe(
            self.state.yolo,
            max_dynamic=settings.stock_dynamic_max,
            max_active=settings.stock_max_active,
        )
        self.state.stock_universe_all = list(meta["symbols"])
        self.state.stock_universe_active = list(meta["active"])
        self.state.stock_universe_dynamic = list(meta["dynamic"])
        self.state.stock_universe_sources = dict(meta["sources"])
        for product in self.state.stock_universe_active:
            if product not in self.state.stock_pairs:
                self.state.stock_pairs[product] = PairSnapshot(
                    product=product, max_open=settings.stock_max_positions
                )
        self._last_universe_refresh = now_m
        log.info(
            "stock universe refreshed",
            extra={
                "data": {
                    "active": len(self.state.stock_universe_active),
                    "all": len(self.state.stock_universe_all),
                    "dynamic": len(self.state.stock_universe_dynamic),
                }
            },
        )

    def tick(self) -> None:
        settings = self.state.settings
        if not settings.stock_enabled:
            return
        if settings.stock_mode != "paper":
            log.error(
                "STOCK_MODE must be paper; refusing stock tick",
                extra={"data": {"stock_mode": settings.stock_mode}},
            )
            return
        ledger = self.state.stock_ledger
        if ledger is None:
            return

        self.refresh_universe()
        now = utcnow()
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)

        # Market I/O intentionally OUTSIDE AppState.lock — Yahoo fetches for ~45
        # symbols would otherwise block /api/snapshot and the crypto dashboard.
        marks: dict[str, float] = {}
        for product in list(self.state.stock_universe_active):
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        equity = ledger.equity_usd(marks)
        utc_date, start_eq, killed = ledger.ensure_utc_day(now, equity)
        if not killed and daily_loss_breached(
            equity, start_eq, settings.stock_daily_loss_kill_usd
        ):
            log.warning(
                "stock daily loss kill",
                extra={
                    "data": {
                        "equity": equity,
                        "start": start_eq,
                        "kill": settings.stock_daily_loss_kill_usd,
                    }
                },
            )
            if not halted and can_trade:
                self._flatten_all(marks, reason="daily_loss_kill", now=now)
            ledger.set_daily_killed(utc_date)
            killed = True

        if halted and can_trade:
            open_lots = [
                lot
                for product in self.state.stock_universe_active
                for lot in ledger.open_positions(product)
            ]
            if open_lots:
                log.warning(
                    "stock halt emergency flatten",
                    extra={"data": {"open_lots": len(open_lots)}},
                )
                self._flatten_all(marks, reason="halt_flatten", now=now)

        for product in list(self.state.stock_universe_active):
            self._act_on_pair(
                product=product,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=killed,
                marks=marks,
            )

        self.state.stock_last_tick_at = now
        self.state.stock_mark_source = getattr(self.market, "mark_source", "yahoo_paper")

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.stock_pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.stock_max_positions
        )
        limit = settings.ohlcv_fetch_limit
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("stock ticker failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)

        # Only fetch timeframes required by enabled stock strategies.
        wanted = set(settings.stock_strategy_list)
        frames: list[tuple[str, str]] = []
        if "sma_15m" in wanted:
            frames.append(("15m", "15m"))
        if "sma_5m" in wanted:
            frames.append(("5m", "5m"))
        if "sma_1d" in wanted or not frames:
            frames.append(("1d", "1d"))
        for timeframe, dest in frames:
            try:
                rows = self.market.fetch_ohlcv(product, timeframe, limit)
                closes = [float(r[4]) for r in rows]
                candle_ts = None
                if rows:
                    candle_ts = datetime.fromtimestamp(
                        float(rows[-1][0]) / 1000.0, tz=timezone.utc
                    )
                sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
                fast_v = sma(closes, settings.sma_fast)
                slow_v = sma(closes, settings.sma_slow)
                if dest == "15m" and len(closes) >= settings.sma_slow:
                    snap.sma_fast = fast_v
                    snap.sma_slow = slow_v
                    snap.signal = sig.value
                    snap.candle_ts = candle_ts
                elif dest == "5m" and len(closes) >= settings.sma_slow:
                    snap.sma_fast_5m = fast_v
                    snap.sma_slow_5m = slow_v
                    snap.signal_5m = sig.value
                    snap.candle_ts_5m = candle_ts
                elif dest == "1d":
                    snap.sma_fast_1d = fast_v
                    snap.sma_slow_1d = slow_v
                    snap.signal_1d = sig.value
                    snap.candle_ts_1d = candle_ts
                    # If 15m missing (weekend), mirror daily into 15m slots for display
                    if snap.sma_fast is None and fast_v is not None:
                        snap.sma_fast = fast_v
                        snap.sma_slow = slow_v
                        snap.signal = sig.value
                        snap.candle_ts = candle_ts
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "stock ohlcv failed",
                    extra={"data": {"product": product, "timeframe": timeframe}},
                )
                snap.last_error = str(exc)

        ledger = self.state.stock_ledger
        assert ledger is not None
        snap.open_count = ledger.open_count(product)
        self.state.stock_pairs[product] = snap
        return snap

    def _act_on_pair(
        self,
        product: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        settings = self.state.settings
        for strategy_id in settings.stock_strategy_list:
            self._act_on_strategy(
                product=product,
                strategy_id=strategy_id,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=daily_killed,
                marks=marks,
            )

    def _act_on_strategy(
        self,
        product: str,
        strategy_id: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs[product]

        # Skip strategies that lack SMA data
        if _sma_fast(snap, strategy_id) is None or _sma_slow(snap, strategy_id) is None:
            return

        signal, uptrend = _signal_for(snap, strategy_id)
        all_lots = ledger.open_positions(product)
        strategy_lots = [lot for lot in all_lots if lot.strategy == strategy_id]
        max_pos = settings.stock_max_positions

        want_entry = signal is Signal.ENTER or (
            signal is Signal.HOLD and uptrend and 0 < len(strategy_lots) < max_pos
        )
        want_signal_exit = signal is Signal.EXIT and len(strategy_lots) > 0

        mark = marks.get(product)
        if mark is None and snap.last is not None:
            mark = snap.last

        lots_to_close: list[Position] = []
        exit_reason = f"{strategy_id}:exit"
        fade = momentum_fading(
            last=mark,
            sma_fast=_sma_fast(snap, strategy_id),
            sma_slow=_sma_slow(snap, strategy_id),
        )

        if strategy_lots and want_signal_exit:
            for lot in strategy_lots:
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    never_sell_red=settings.never_sell_red,
                )
                if ok_sw:
                    lots_to_close.append(lot)
                else:
                    log.info(
                        "stock strategy swing hold",
                        extra={
                            "data": {
                                "product": product,
                                "strategy": strategy_id,
                                "position_id": lot.id,
                                "reason": reason_sw,
                            }
                        },
                    )
        elif strategy_lots and fade:
            eligible: list[Position] = []
            for lot in strategy_lots:
                ok_sw, _reason = strategy_exit_allowed(
                    lot,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    never_sell_red=settings.never_sell_red,
                )
                if ok_sw:
                    eligible.append(lot)
            if eligible:

                def _pnl_key(lot: Position) -> float:
                    pct = lot_unrealized_pnl_pct(lot, mark)
                    return pct if pct is not None else float("-inf")

                best = max(eligible, key=_pnl_key)
                lots_to_close = [best]
                exit_reason = f"{strategy_id}:fade"

        if lots_to_close:
            ctx = _stock_risk_ctx(
                settings,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=daily_killed,
                open_count_for_pair=len(all_lots),
                last_entry_at=ledger.last_entry_at(product, strategy_id),
                cash_usd=ledger.cash_usd(),
                requested_notional=settings.stock_max_notional_usd,
                cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            )
            ok, reason = allow_exit(ctx)
            if not ok:
                log.info(
                    "stock exit blocked",
                    extra={"data": {"product": product, "strategy": strategy_id, "reason": reason}},
                )
                return
            self._close_lots(product, lots_to_close, marks, reason=exit_reason, now=now)
            return

        if want_signal_exit or not want_entry:
            return

        if settings.pair_pause_enabled and ledger.is_pair_paused(product, now):
            return

        is_scale_in = len(strategy_lots) > 0
        if is_scale_in:
            ok_si, reason_si = scale_in_allowed(
                strategy_lots, mark, settings.scale_in_min_profit_pct
            )
            if not ok_si:
                return

        ok_tf, reason_tf = trend_filter_allows(
            snap.last,
            _sma_slow(snap, strategy_id),
            enabled=settings.trend_filter_enabled,
        )
        if not ok_tf:
            return

        ctx = _stock_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(all_lots),
            last_entry_at=ledger.last_entry_at(product, strategy_id),
            cash_usd=ledger.cash_usd(),
            requested_notional=settings.stock_max_notional_usd,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            return
        entry_reason = f"{strategy_id}:scale_in" if is_scale_in else f"{strategy_id}:enter"
        self._open_lot(product, marks, reason=entry_reason, now=now, strategy=strategy_id)

    def _open_lot(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        strategy: str,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        # HARD: never live stock orders
        snap = self.state.stock_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        px = fill_price(ticker, "buy", settings.slippage_bps)
        notional = min(settings.stock_max_notional_usd, ledger.cash_usd())
        if notional <= 0:
            return
        pos, fill = ledger.open_buy(
            product=product,
            fill_px=px,
            notional_usd=settings.stock_max_notional_usd,
            slippage_bps=settings.slippage_bps,
            fee_usd=0.0,
            reason=reason,
            ts=now,
            strategy=strategy,
        )
        self.state.stock_pairs[product].open_count = ledger.open_count(product)
        log.info(
            "stock paper buy",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "qty": pos.qty,
                    "price": fill.price,
                    "notional": fill.notional_usd,
                    "reason": reason,
                    "position_id": pos.id,
                    "mark_source": self.state.stock_mark_source,
                }
            },
        )

    def _close_lots(
        self,
        product: str,
        lots: list[Position],
        marks: dict[str, float],
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        paper_px = fill_price(ticker, "sell", settings.slippage_bps)
        for lot in lots:
            fill = ledger.close_position(
                position_id=lot.id,
                fill_px=paper_px,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
            )
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
            paused = ledger.record_closed_trade_for_pause(
                product,
                realized,
                now=now,
                enabled=settings.pair_pause_enabled,
                loss_threshold=settings.pair_pause_losses,
                pause_hours=settings.pair_pause_hours,
            )
            if paused:
                log.warning("stock pair paused", extra={"data": paused})
            sell_data = {
                "product": product,
                "strategy": lot.strategy,
                "qty": fill.qty,
                "price": fill.price,
                "reason": reason,
                "position_id": lot.id,
                "entry_price": lot.entry_price,
                "realized": realized,
            }
            if is_emergency_flatten_reason(reason) and fill.price < lot.entry_price:
                log.warning("stock emergency flatten sells red", extra={"data": sell_data})
            log.info("stock paper sell", extra={"data": sell_data})
        self.state.stock_pairs[product].open_count = ledger.open_count(product)

    def _flatten_all(self, marks: dict[str, float], reason: str, now: datetime) -> None:
        ledger = self.state.stock_ledger
        assert ledger is not None
        for product in list(self.state.stock_universe_active):
            lots = ledger.open_positions(product)
            if lots:
                self._close_lots(product, lots, marks, reason=reason, now=now)

    def run_forever(self) -> None:
        interval = max(5.0, float(self.state.settings.stock_poll_seconds))
        log.info("stock paper loop start", extra={"data": {"poll_seconds": interval}})
        # One-shot Coinbase equity id probe (informational)
        try:
            ids = resolve_coinbase_equity_ids(list(self.state.stock_universe_active or [])[:40])
            self.state.stock_coinbase_ids = ids
            if ids:
                log.info("coinbase spot equity ids", extra={"data": ids})
            else:
                log.info(
                    "no Coinbase spot equity product_ids; using Yahoo paper marks",
                    extra={"data": {"mark_source": "yahoo_paper"}},
                )
        except Exception:  # noqa: BLE001
            log.exception("coinbase equity probe failed")

        while self.state.running:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("stock tick failed")
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(0.05, remaining)
            while self.state.running and time.monotonic() < deadline:
                time.sleep(0.2)


def attach_stock_lane(state: AppState) -> StockPaperEngine | None:
    """Create isolated stock ledger + engine if STOCK_ENABLED."""
    settings = state.settings
    if not settings.stock_enabled:
        log.info("stock paper lane disabled")
        return None
    if settings.stock_mode != "paper":
        raise RuntimeError(
            f"STOCK_MODE={settings.stock_mode!r} refused — stock lane is paper-only this week"
        )
    ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.stock_ledger = ledger
    state.stock_mark_source = "yahoo_paper"
    engine = StockPaperEngine(state)
    engine.refresh_universe(force=True)
    state.stock_engine = engine
    log.warning(
        "stock paper lane attached",
        extra={
            "data": {
                "sqlite": str(settings.stock_sqlite_path),
                "bankroll": settings.stock_bankroll_usd,
                "active": len(state.stock_universe_active or []),
                "strategies": settings.stock_strategy_list,
                "mark_source": "yahoo_paper",
            }
        },
    )
    return engine
