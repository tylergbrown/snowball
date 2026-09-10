"""Future Trader paper engine — isolated sqlite; never places live futures/crypto orders."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from snowball.config import LiveTradingRefused, Settings
from snowball.gates import (
    is_emergency_flatten_reason,
    lot_unrealized_pnl_pct,
    momentum_fading,
    scale_in_allowed,
    sma_fast_for_strategy,
    sma_slow_for_strategy,
    strategy_exit_allowed,
    trend_filter_allows,
)
from snowball.halt import halt_active, trading_enabled
from snowball.market import fill_price
from snowball.models import PairSnapshot, Position, Signal, Ticker, utcnow
from snowball.paper import PaperLedger
from snowball.risk import RiskContext, allow_entry, allow_exit, daily_loss_breached
from snowball.state import AppState
from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    CoinbaseFuturesMarket,
    normalize_futures_product,
)
from snowball.strategy import (
    DONCHIAN_1D,
    SMA_1D,
    crossover_signal,
    donchian_breakout_signal,
    donchian_channels,
    enabled_timeframes,
    signal_for_strategy,
    sma,
)

log = logging.getLogger("snowball.futures.engine")


def _futures_risk_ctx(
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
    """Always paper + live_enabled=False so crypto live flags cannot leak into futures."""
    return RiskContext(
        now=now,
        trading_enabled=can_trade,
        halt_active=halted,
        daily_killed=daily_killed,
        open_count_for_pair=open_count_for_pair,
        last_entry_at=last_entry_at,
        cash_usd=cash_usd,
        requested_notional=requested_notional,
        max_positions_per_pair=settings.futures_max_positions,
        max_position_notional_usd=settings.futures_max_notional_usd,
        entry_cooldown=timedelta(seconds=cooldown_seconds),
        mode="paper",
        live_enabled=False,
    )


def _refuse_live_futures(settings: Settings) -> None:
    """Hard gate: v1 is paper-only. Dual live gate for any future path."""
    if settings.futures_mode != "paper":
        raise LiveTradingRefused(
            f"Futures trading refused: FUTURES_MODE={settings.futures_mode!r} "
            "(Future Trader v1 is paper-only)."
        )
    if settings.futures_live_enabled:
        raise LiveTradingRefused(
            "Futures trading refused: FUTURES_LIVE_ENABLED=true but FUTURES_MODE "
            "is not live (both required for any future live path)."
        )


class FuturesPaperEngine:
    """Long-only daily SMA/Donchian paper lane on Coinbase equity perps."""

    def __init__(
        self, state: AppState, market: CoinbaseFuturesMarket | None = None
    ) -> None:
        self.state = state
        if market is not None:
            self.market = market
        else:
            s = state.settings
            self.market = CoinbaseFuturesMarket(
                api_key=s.coinbase_api_key,
                api_secret=s.coinbase_api_secret,
                api_passphrase=s.coinbase_api_passphrase,
            )

    def product_list(self) -> list[str]:
        settings = self.state.settings
        items = [
            normalize_futures_product(p)
            for p in settings.futures_product_list
        ]
        return items or list(DEFAULT_FUTURES_PRODUCTS)

    def tick(self) -> None:
        settings = self.state.settings
        if not settings.futures_enabled:
            return
        try:
            _refuse_live_futures(settings)
        except LiveTradingRefused as exc:
            log.error(str(exc))
            return
        ledger = self.state.futures_ledger
        if ledger is None:
            return

        products = self.product_list()
        for product in products:
            if product not in self.state.futures_pairs:
                self.state.futures_pairs[product] = PairSnapshot(
                    product=product, max_open=settings.futures_max_positions
                )

        now = utcnow()
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)

        # Market I/O outside AppState.lock — same pattern as stocks.
        marks: dict[str, float] = {}
        for product in products:
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        equity = ledger.equity_usd(marks)
        utc_date, start_eq, killed = ledger.ensure_utc_day(now, equity)
        if not killed and daily_loss_breached(
            equity, start_eq, settings.futures_daily_loss_kill_usd
        ):
            log.warning(
                "futures daily loss kill",
                extra={
                    "data": {
                        "equity": equity,
                        "start": start_eq,
                        "kill": settings.futures_daily_loss_kill_usd,
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
                for product in products
                for lot in ledger.open_positions(product)
            ]
            if open_lots:
                log.warning(
                    "futures halt emergency flatten",
                    extra={"data": {"open_lots": len(open_lots)}},
                )
                self._flatten_all(marks, reason="halt_flatten", now=now)

        for product in products:
            self._act_on_pair(
                product=product,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=killed,
                marks=marks,
            )

        self.state.futures_last_tick_at = now
        self.state.futures_mark_source = getattr(
            self.market, "mark_source", "coinbase_perp"
        )

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.futures_pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.futures_max_positions
        )
        limit = settings.ohlcv_fetch_limit
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("futures ticker failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)

        wanted = set(settings.futures_strategy_list)
        frames = enabled_timeframes(settings.futures_strategy_list)
        if not frames:
            frames = ["1d"]
        for timeframe in frames:
            try:
                rows = self.market.fetch_ohlcv(product, timeframe, limit)
                closes = [float(r[4]) for r in rows]
                highs = [float(r[2]) for r in rows]
                lows = [float(r[3]) for r in rows]
                candle_ts = None
                if rows:
                    candle_ts = datetime.fromtimestamp(
                        float(rows[-1][0]) / 1000.0, tz=timezone.utc
                    )
                sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
                fast_v = sma(closes, settings.sma_fast)
                slow_v = sma(closes, settings.sma_slow)
                if timeframe == "1d" and SMA_1D in wanted:
                    snap.sma_fast_1d = fast_v
                    snap.sma_slow_1d = slow_v
                    snap.signal_1d = sig.value
                    snap.candle_ts_1d = candle_ts
                    # Mirror into primary slots for display / trend gates
                    if fast_v is not None:
                        snap.sma_fast = fast_v
                        snap.sma_slow = slow_v
                        snap.signal = sig.value
                        snap.candle_ts = candle_ts
                if timeframe == "1d" and DONCHIAN_1D in wanted:
                    high, low = donchian_channels(highs, lows)
                    snap.donchian_high_1d = high
                    snap.donchian_low_1d = low
                    snap.signal_donchian_1d = donchian_breakout_signal(
                        closes, highs, lows
                    ).value
                    if snap.candle_ts_1d is None:
                        snap.candle_ts_1d = candle_ts
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "futures ohlcv failed",
                    extra={"data": {"product": product, "timeframe": timeframe}},
                )
                snap.last_error = str(exc)

        ledger = self.state.futures_ledger
        assert ledger is not None
        snap.open_count = ledger.open_count(product)
        self.state.futures_pairs[product] = snap
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
        for strategy_id in settings.futures_strategy_list:
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
        ledger = self.state.futures_ledger
        assert ledger is not None
        snap = self.state.futures_pairs[product]

        # Skip strategies that lack indicator data
        if (
            sma_fast_for_strategy(snap, strategy_id) is None
            or sma_slow_for_strategy(snap, strategy_id) is None
        ):
            return

        signal, uptrend = signal_for_strategy(snap, strategy_id)
        all_lots = ledger.open_positions(product)
        strategy_lots = [lot for lot in all_lots if lot.strategy == strategy_id]
        max_pos = settings.futures_max_positions

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
            sma_fast=sma_fast_for_strategy(snap, strategy_id),
            sma_slow=sma_slow_for_strategy(snap, strategy_id),
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
                        "futures strategy swing hold",
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
            ctx = _futures_risk_ctx(
                settings,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=daily_killed,
                open_count_for_pair=len(all_lots),
                last_entry_at=ledger.last_entry_at(product, strategy_id),
                cash_usd=ledger.cash_usd(),
                requested_notional=settings.futures_max_notional_usd,
                cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            )
            ok, reason = allow_exit(ctx)
            if not ok:
                log.info(
                    "futures exit blocked",
                    extra={
                        "data": {
                            "product": product,
                            "strategy": strategy_id,
                            "reason": reason,
                        }
                    },
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
            sma_slow_for_strategy(snap, strategy_id),
            enabled=settings.trend_filter_enabled,
        )
        if not ok_tf:
            return

        ctx = _futures_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(all_lots),
            last_entry_at=ledger.last_entry_at(product, strategy_id),
            cash_usd=ledger.cash_usd(),
            requested_notional=settings.futures_max_notional_usd,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            return
        entry_reason = (
            f"{strategy_id}:scale_in" if is_scale_in else f"{strategy_id}:enter"
        )
        self._open_lot(
            product, marks, reason=entry_reason, now=now, strategy=strategy_id
        )

    def _open_lot(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        strategy: str,
    ) -> None:
        settings = self.state.settings
        _refuse_live_futures(settings)
        ledger = self.state.futures_ledger
        assert ledger is not None
        snap = self.state.futures_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        px = fill_price(ticker, "buy", settings.slippage_bps)
        notional = min(settings.futures_max_notional_usd, ledger.cash_usd())
        if notional <= 0:
            return
        pos, fill = ledger.open_buy(
            product=product,
            fill_px=px,
            notional_usd=settings.futures_max_notional_usd,
            slippage_bps=settings.slippage_bps,
            fee_usd=0.0,
            reason=reason,
            ts=now,
            strategy=strategy,
        )
        self.state.futures_pairs[product].open_count = ledger.open_count(product)
        log.info(
            "futures paper buy",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "qty": pos.qty,
                    "price": fill.price,
                    "notional": fill.notional_usd,
                    "reason": reason,
                    "position_id": pos.id,
                    "mark_source": self.state.futures_mark_source,
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
        _refuse_live_futures(settings)
        ledger = self.state.futures_ledger
        assert ledger is not None
        snap = self.state.futures_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        paper_px = fill_price(ticker, "sell", settings.slippage_bps)
        for lot in lots:
            # Strategy exits already gated; emergency may sell red.
            if not is_emergency_flatten_reason(reason):
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot,
                    paper_px,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    never_sell_red=settings.never_sell_red,
                )
                if not ok_sw:
                    log.info(
                        "futures close skipped never_sell_red",
                        extra={
                            "data": {
                                "product": product,
                                "position_id": lot.id,
                                "reason": reason_sw,
                            }
                        },
                    )
                    continue
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
                log.warning("futures pair paused", extra={"data": paused})
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
                log.warning(
                    "futures emergency flatten sells red", extra={"data": sell_data}
                )
            log.info("futures paper sell", extra={"data": sell_data})
        self.state.futures_pairs[product].open_count = ledger.open_count(product)

    def _flatten_all(
        self, marks: dict[str, float], reason: str, now: datetime
    ) -> None:
        ledger = self.state.futures_ledger
        assert ledger is not None
        for product in self.product_list():
            lots = ledger.open_positions(product)
            if lots:
                self._close_lots(product, lots, marks, reason=reason, now=now)

    def run_forever(self) -> None:
        interval = max(15.0, float(self.state.settings.futures_poll_seconds))
        log.info(
            "Future Trader paper loop start",
            extra={
                "data": {
                    "poll_seconds": interval,
                    "products": self.product_list(),
                    "strategies": self.state.settings.futures_strategy_list,
                }
            },
        )
        while self.state.running:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("futures tick failed")
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(0.05, remaining)
            while self.state.running and time.monotonic() < deadline:
                time.sleep(0.2)


def attach_futures_lane(state: AppState) -> FuturesPaperEngine | None:
    """Create isolated futures ledger + engine if FUTURES_ENABLED."""
    settings = state.settings
    if not settings.futures_enabled:
        log.info("Future Trader lane disabled")
        return None
    settings.assert_futures_paper_only()
    ledger = PaperLedger(settings.futures_sqlite_path, settings.futures_bankroll_usd)
    state.futures_ledger = ledger
    state.futures_mark_source = "coinbase_perp"
    for product in settings.futures_product_list or list(DEFAULT_FUTURES_PRODUCTS):
        pid = normalize_futures_product(product)
        state.futures_pairs[pid] = PairSnapshot(
            product=pid, max_open=settings.futures_max_positions
        )
    engine = FuturesPaperEngine(state)
    state.futures_engine = engine
    log.warning(
        "Future Trader paper lane attached",
        extra={
            "data": {
                "sqlite": str(settings.futures_sqlite_path),
                "bankroll": settings.futures_bankroll_usd,
                "products": settings.futures_product_list,
                "strategies": settings.futures_strategy_list,
                "mark_source": "coinbase_perp",
                "futures_mode": settings.futures_mode,
                "futures_live_enabled": settings.futures_live_enabled,
            }
        },
    )
    return engine
