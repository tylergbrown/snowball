from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowball.allocation import leg_notional_usd, open_notional_usd
from snowball.config import LiveTradingRefused, Settings
from snowball.halt import halt_active, trading_enabled
from snowball.live import make_broker
from snowball.logging_setup import setup_logging
from snowball.market import CcxtMarket, MarketData, fill_price
from snowball.models import PairSnapshot, Position, Signal, utcnow
from snowball.paper import PaperLedger
from snowball.risk import allow_entry, allow_exit, context_from_settings, daily_loss_breached
from snowball.state import AppState
from snowball.gates import (
    indicator_filters_allow,
    indicator_snapshot_for_strategy,
    is_emergency_flatten_reason,
    lot_unrealized_pnl_pct,
    momentum_fading,
    scale_in_allowed,
    sma_fast_for_strategy,
    sma_slow_for_strategy,
    strategy_exit_allowed,
    trend_filter_allows,
)
from snowball.strategy import (
    DONCHIAN_1D,
    EMA_15M,
    MEAN_REVERSION_STRATEGY_IDS,
    SMA_15M,
    SMA_1D,
    SMA_5M,
    crossover_signal,
    donchian_breakout_signal,
    donchian_channels,
    ema,
    ema_crossover_signal,
    enabled_timeframes,
    populate_rsi_bb,
    signal_for_strategy,
    sma,
)

log = logging.getLogger("snowball.engine")


def _write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(utcnow().isoformat(), encoding="utf-8")


class Engine:
    def __init__(self, state: AppState, market: MarketData) -> None:
        self.state = state
        self.market = market
        self._last_candle: dict[str, float] = {}

    def tick(self) -> None:
        settings = self.state.settings
        now = utcnow()
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)
        self._apply_pair_pause_clear_file()

        if self.state.broker is not None and settings.live_orders_permitted():
            try:
                free_usd = self.state.broker.fetch_free_usd()
                self.state.ledger.set_cash_usd(free_usd, ts=now)
            except Exception:  # noqa: BLE001 — keep the loop alive
                log.exception("live cash sync failed")

        marks: dict[str, float] = {}
        with self.state.lock:
            gap = max(0.0, float(getattr(settings, "pair_fetch_gap_sec", 0.15) or 0.0))
            for i, product in enumerate(settings.product_list):
                if i and gap:
                    time.sleep(gap)
                snap = self._update_pair(product)
                if snap.last is not None:
                    marks[product] = snap.last

            equity = self.state.ledger.equity_usd(marks)
            utc_date, start_eq, killed = self.state.ledger.ensure_utc_day(now, equity)
            if not killed and daily_loss_breached(equity, start_eq, settings.daily_loss_kill_usd):
                log.warning(
                    "daily loss kill",
                    extra={"data": {"equity": equity, "start": start_eq, "kill": settings.daily_loss_kill_usd}},
                )
                if not halted and can_trade:
                    self._flatten_all(marks, reason="daily_loss_kill", now=now)
                self.state.ledger.set_daily_killed(utc_date)
                killed = True

            # Emergency: HALT flattens open lots (may sell red). Blocks new entries via allow_entry.
            if halted and can_trade:
                open_lots = [
                    lot
                    for product in settings.product_list
                    for lot in self.state.ledger.open_positions(product)
                ]
                if open_lots:
                    log.warning(
                        "halt emergency flatten",
                        extra={"data": {"open_lots": len(open_lots)}},
                    )
                    self._flatten_all(marks, reason="halt_flatten", now=now)

            for product in settings.product_list:
                self._act_on_pair(
                    product=product,
                    now=now,
                    halted=halted,
                    can_trade=can_trade,
                    daily_killed=killed,
                    marks=marks,
                )

            self.state.last_tick_at = now
            self.state.last_error = None
            _write_heartbeat(settings.heartbeat_path)

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.max_positions_per_pair
        )
        limit = settings.ohlcv_fetch_limit
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.exception("ticker update failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)
            self.state.last_error = str(exc)

        wanted = set(settings.strategy_list)
        for timeframe in enabled_timeframes(settings.strategy_list):
            try:
                rows = self.market.fetch_ohlcv(product, timeframe, limit)
                closes = [float(r[4]) for r in rows]
                highs = [float(r[2]) for r in rows]
                lows = [float(r[3]) for r in rows]
                candle_ts = None
                if rows:
                    candle_ts = datetime.fromtimestamp(float(rows[-1][0]) / 1000.0, tz=timezone.utc)
                    self._last_candle[f"{product}:{timeframe}"] = float(rows[-1][0])
                if timeframe == "15m" and SMA_15M in wanted:
                    sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
                    fast_v = sma(closes, settings.sma_fast)
                    slow_v = sma(closes, settings.sma_slow)
                    snap.sma_fast = fast_v
                    snap.sma_slow = slow_v
                    snap.signal = sig.value
                    snap.candle_ts = candle_ts
                elif timeframe == "5m" and SMA_5M in wanted:
                    sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
                    fast_v = sma(closes, settings.sma_fast)
                    slow_v = sma(closes, settings.sma_slow)
                    snap.sma_fast_5m = fast_v
                    snap.sma_slow_5m = slow_v
                    snap.signal_5m = sig.value
                    snap.candle_ts_5m = candle_ts
                elif timeframe == "1d" and SMA_1D in wanted:
                    sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
                    fast_v = sma(closes, settings.sma_fast)
                    slow_v = sma(closes, settings.sma_slow)
                    snap.sma_fast_1d = fast_v
                    snap.sma_slow_1d = slow_v
                    snap.signal_1d = sig.value
                    snap.candle_ts_1d = candle_ts
                if timeframe == "15m" and EMA_15M in wanted:
                    snap.ema_fast_15m = ema(closes, 12)
                    snap.ema_slow_15m = ema(closes, 26)
                    snap.signal_ema_15m = ema_crossover_signal(closes).value
                    if snap.candle_ts is None:
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
                populate_rsi_bb(
                    snap,
                    closes,
                    timeframe,
                    wanted=wanted,
                    filters_enabled=bool(
                        getattr(settings, "indicator_filters_enabled", True)
                    ),
                    rsi_period=int(getattr(settings, "rsi_period", 14) or 14),
                    bb_period=int(getattr(settings, "bb_period", 20) or 20),
                    bb_std_mult=float(getattr(settings, "bb_std_mult", 2.0) or 2.0),
                )
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                log.exception(
                    "ohlcv update failed",
                    extra={"data": {"product": product, "timeframe": timeframe}},
                )
                snap.last_error = str(exc)
                self.state.last_error = str(exc)

        snap.open_count = self.state.ledger.open_count(product)
        self.state.pairs[product] = snap
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
        snap = self.state.pairs[product]
        for strategy_id in settings.strategy_list:
            self._act_on_strategy(
                product=product,
                strategy_id=strategy_id,
                snap=snap,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=daily_killed,
                marks=marks,
            )
        snap.open_count = self.state.ledger.open_count(product)

    def _act_on_strategy(
        self,
        product: str,
        strategy_id: str,
        snap: PairSnapshot,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        settings = self.state.settings
        # ema_15m / donchian_1d stay stock-only. rsi_*/bb_* are live on crypto.
        if strategy_id in (EMA_15M, DONCHIAN_1D):
            return
        signal, uptrend = signal_for_strategy(snap, strategy_id)
        all_lots = self.state.ledger.open_positions(product)
        strategy_lots = [lot for lot in all_lots if lot.strategy == strategy_id]

        want_entry = signal is Signal.ENTER or (
            signal is Signal.HOLD
            and uptrend
            and 0 < len(strategy_lots) < settings.max_positions_per_pair
        )
        want_signal_exit = signal is Signal.EXIT and len(strategy_lots) > 0

        mark = marks.get(product)
        if mark is None and snap.last is not None:
            mark = snap.last

        # Exits (never red; +min_tp is a FLOOR — no auto-sell at 5% alone):
        # 1) Full death cross (Signal.EXIT) after >= floor → close all eligible lots.
        # 2) Momentum fade (last < SMA fast, still > SMA slow) after >= floor →
        #    scale out ONE best-green lot with reason {strategy}:fade.
        # HALT / daily-loss kill flatten separately and may sell red.
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
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=settings.fee_buffer_pct,
                )
                if ok_sw:
                    lots_to_close.append(lot)
                else:
                    log.info(
                        "strategy swing hold",
                        extra={
                            "data": {
                                "product": product,
                                "strategy": strategy_id,
                                "position_id": lot.id,
                                "reason": reason_sw,
                                "mark": mark,
                                "entry": lot.entry_price,
                                "signal": signal.value,
                            }
                        },
                    )
            exit_reason = f"{strategy_id}:exit"
        elif strategy_lots and fade:
            # Scale out a single lot on fade — prefer highest unrealized %.
            eligible: list[Position] = []
            for lot in strategy_lots:
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=settings.fee_buffer_pct,
                )
                if ok_sw:
                    eligible.append(lot)
                else:
                    log.info(
                        "strategy fade hold",
                        extra={
                            "data": {
                                "product": product,
                                "strategy": strategy_id,
                                "position_id": lot.id,
                                "reason": reason_sw,
                                "mark": mark,
                                "entry": lot.entry_price,
                            }
                        },
                    )
            if eligible:
                def _pnl_key(lot: Position) -> float:
                    pct = lot_unrealized_pnl_pct(lot, mark)
                    return pct if pct is not None else float("-inf")

                best = max(eligible, key=_pnl_key)
                lots_to_close = [best]
                exit_reason = f"{strategy_id}:fade"

        if lots_to_close:
            ctx = context_from_settings(
                settings,
                now=now,
                halt_active=halted,
                trading_enabled=can_trade,
                daily_killed=daily_killed,
                open_count_for_pair=len(all_lots),
                last_entry_at=self.state.ledger.last_entry_at(product, strategy_id),
                cash_usd=self.state.ledger.cash_usd(),
                requested_notional=settings.max_position_notional_usd,
                cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            )
            ok, reason = allow_exit(ctx)
            if not ok:
                log.info(
                    "exit blocked",
                    extra={"data": {"product": product, "strategy": strategy_id, "reason": reason}},
                )
                return
            self._close_lots(product, lots_to_close, marks, reason=exit_reason, now=now)
            return

        if want_signal_exit:
            # Strategy held all lots (below TP / red); do not fall through to entry.
            return

        if not want_entry:
            return

        if settings.pair_pause_enabled and self.state.ledger.is_pair_paused(product, now):
            log.info(
                "entry blocked",
                extra={"data": {"product": product, "strategy": strategy_id, "reason": "pair_paused"}},
            )
            return

        is_scale_in = len(strategy_lots) > 0
        if is_scale_in:
            ok_si, reason_si = scale_in_allowed(
                strategy_lots, mark, settings.scale_in_min_profit_pct
            )
            if not ok_si:
                log.info(
                    "entry blocked",
                    extra={
                        "data": {
                            "product": product,
                            "strategy": strategy_id,
                            "reason": reason_si,
                        }
                    },
                )
                return

        use_trend = settings.trend_filter_enabled and strategy_id not in MEAN_REVERSION_STRATEGY_IDS
        ok_tf, reason_tf = trend_filter_allows(
            snap.last,
            sma_slow_for_strategy(snap, strategy_id),
            enabled=use_trend,
        )
        if not ok_tf:
            log.info(
                "entry blocked",
                extra={"data": {"product": product, "strategy": strategy_id, "reason": reason_tf}},
            )
            return

        rsi_v, bb_up, bb_mid, _bb_lo = indicator_snapshot_for_strategy(snap, strategy_id)
        ok_ind, reason_ind = indicator_filters_allow(
            last=snap.last if snap.last is not None else mark,
            rsi=rsi_v,
            bb_upper=bb_up,
            bb_mid=bb_mid,
            enabled=bool(getattr(settings, "indicator_filters_enabled", True)),
        )
        if not ok_ind:
            log.info(
                "entry blocked",
                extra={"data": {"product": product, "strategy": strategy_id, "reason": reason_ind}},
            )
            return

        # Cap new lots so crypto open notional stays within CRYPTO_ACCOUNT_BUDGET_PCT.
        crypto_notional = self._crypto_leg_notional(marks)
        if crypto_notional <= 1e-6:
            log.info(
                "entry blocked",
                extra={
                    "data": {
                        "product": product,
                        "strategy": strategy_id,
                        "reason": "crypto_budget_full",
                        "budget_pct": settings.crypto_account_budget_pct,
                    }
                },
            )
            return

        ctx = context_from_settings(
            settings,
            now=now,
            halt_active=halted,
            trading_enabled=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(all_lots),
            last_entry_at=self.state.ledger.last_entry_at(product, strategy_id),
            cash_usd=self.state.ledger.cash_usd(),
            requested_notional=crypto_notional,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            max_position_notional_usd=crypto_notional,
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            log.info(
                "entry blocked",
                extra={"data": {"product": product, "strategy": strategy_id, "reason": reason}},
            )
            return
        entry_reason = (
            f"{strategy_id}:scale_in" if is_scale_in else f"{strategy_id}:enter"
        )
        self._open_lot(
            product,
            marks,
            reason=entry_reason,
            now=now,
            strategy=strategy_id,
            notional_usd=crypto_notional,
        )


    def _crypto_leg_notional(self, marks: dict[str, float] | None = None) -> float:
        """Cap new crypto lot size so open notional stays within crypto_account_budget_pct."""
        settings = self.state.settings
        ledger = self.state.ledger
        open_n = open_notional_usd(ledger.open_positions())
        account_value = 0.0
        # Prefer live free+positions equity; fall back to ledger equity / bankroll
        try:
            if self.state.broker is not None and settings.live_orders_permitted():
                # Use ledger equity marked to market as account proxy when broker
                # balance sync already updated cash; futures lane owns full AV fetch.
                account_value = float(ledger.equity_usd(marks or self.state.marks()))
            else:
                account_value = float(ledger.equity_usd(marks or self.state.marks()))
        except Exception:
            account_value = float(ledger.cash_usd() + open_n)
        if account_value <= 0:
            account_value = float(settings.bankroll_usd)
        # If futures engine cached a fresher Coinbase AV, prefer it for allocation.
        ft_av = getattr(self.state, "futures_account_value_usd", None)
        if ft_av is not None and float(ft_av) > 0:
            account_value = float(ft_av)
        budget = account_value * float(settings.crypto_account_budget_pct)
        per_leg = settings.effective_per_leg_notional_usd(account_value)
        return leg_notional_usd(
            budget_usd=budget,
            open_notional_usd=open_n,
            max_notional_usd=per_leg,
            target_legs=max(4, settings.max_positions_per_pair * 2),
        )

    def _open_lot(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        strategy: str = "sma_15m",
        notional_usd: float | None = None,
    ) -> None:
        settings = self.state.settings
        snap = self.state.pairs[product]
        from snowball.models import Ticker

        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        target = float(
            notional_usd
            if notional_usd is not None
            else settings.per_leg_base_usd
        )
        broker = self.state.broker
        if broker is not None and settings.live_orders_permitted():
            self._open_lot_live(
                product=product,
                ticker=ticker,
                reason=reason,
                now=now,
                strategy=strategy,
                notional_usd=target,
            )
            return
        px = fill_price(ticker, "buy", settings.slippage_bps)
        notional = min(target, self.state.ledger.cash_usd())
        if notional <= 0:
            return
        pos, fill = self.state.ledger.open_buy(
            product=product,
            fill_px=px,
            notional_usd=notional,
            slippage_bps=settings.slippage_bps,
            fee_usd=0.0,
            reason=reason,
            ts=now,
            strategy=strategy,
        )
        self.state.pairs[product].open_count = self.state.ledger.open_count(product)
        log.info(
            "paper buy",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "qty": pos.qty,
                    "price": fill.price,
                    "notional": fill.notional_usd,
                    "reason": reason,
                    "position_id": pos.id,
                }
            },
        )

    def _open_lot_live(
        self,
        product: str,
        ticker: object,
        reason: str,
        now: datetime,
        strategy: str,
        notional_usd: float | None = None,
    ) -> None:
        """Place a Coinbase maker-limit buy and record the exchange fill into the ledger."""
        from snowball.live import parse_order_fill

        settings = self.state.settings
        broker = self.state.broker
        assert broker is not None
        try:
            free_usd = broker.fetch_free_usd()
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.exception(
                "live balance fetch failed; skip buy",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        # Keep ledger cash in sync with exchange free USD for risk / dashboard.
        try:
            self.state.ledger.set_cash_usd(free_usd, ts=now)
        except Exception:  # noqa: BLE001
            log.exception("live cash sync failed")
        target = float(
            notional_usd
            if notional_usd is not None
            else settings.per_leg_base_usd
        )
        notional = min(target, free_usd, self.state.ledger.cash_usd())
        if notional <= 1e-6:
            log.info(
                "live buy skipped",
                extra={
                    "data": {
                        "product": product,
                        "reason": "insufficient_funds_or_budget",
                        "free_usd": free_usd,
                        "target": target,
                    }
                },
            )
            return
        try:
            est_px = fill_price(ticker, "buy", 0.0)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            log.info(
                "live buy skipped",
                extra={"data": {"product": product, "reason": "no_price", "error": str(exc)}},
            )
            return
        if est_px <= 0:
            return
        amount = notional / est_px
        # Maker-limit entry: rest at/near best bid (post-only). No market buys.
        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        try:
            from snowball.maker import maker_buy_price

            limit_px = maker_buy_price(bid, ask, est_px)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "live buy skipped",
                    extra={
                        "data": {
                            "product": product,
                            "reason": "no_book_bid_for_maker",
                            "bid": bid,
                            "ask": ask,
                        }
                    },
                )
                return
            amount = notional / limit_px
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = broker.create_maker_limit_order(
                product,
                "buy",
                amount,
                price=limit_px,
                bid=bid,
                ask=ask,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — insufficient funds / API errors
            log.exception(
                "live buy failed; skip",
                extra={"data": {"product": product, "strategy": strategy, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_order_fill(order if isinstance(order, dict) else {})
        if fill_qty <= 0 or fill_px <= 0:
            log.warning(
                "live buy unfilled/canceled maker limit; skip ledger",
                extra={"data": {"product": product, "order": order}},
            )
            return
        notional_filled = fill_qty * fill_px
        try:
            pos, fill = self.state.ledger.open_buy(
                product=product,
                fill_px=fill_px,
                notional_usd=notional_filled,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
                strategy=strategy,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "live buy filled on exchange but ledger record failed",
                extra={"data": {"product": product, "error": str(exc), "fill_px": fill_px, "qty": fill_qty}},
            )
            return
        self.state.pairs[product].open_count = self.state.ledger.open_count(product)
        log.info(
            "live buy",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "qty": pos.qty,
                    "price": fill.price,
                    "notional": fill.notional_usd,
                    "fee_usd": fee_usd,
                    "reason": reason,
                    "position_id": pos.id,
                }
            },
        )

    def _close_lots(
        self, product: str, lots: list[Position], marks: dict[str, float], reason: str, now: datetime
    ) -> None:
        settings = self.state.settings
        from snowball.models import Ticker
        from snowball.live import parse_order_fill

        snap = self.state.pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        broker = self.state.broker
        live = broker is not None and settings.live_orders_permitted()
        paper_px = None if live else fill_price(ticker, "sell", settings.slippage_bps)
        for lot in lots:
            # Absolute never-sell-red: skip any lot that is underwater vs entry
            # (strategy, fade, HALT, and daily-loss alike when enabled).
            mark = marks.get(product)
            if mark is None and snap.last is not None:
                mark = snap.last
            refuse_red = bool(settings.never_sell_red) or bool(
                getattr(settings, "never_sell_red_emergency", True)
            )
            if refuse_red and mark is not None and lot.entry_price > 0 and mark < lot.entry_price:
                log.info(
                    "never_sell_red hold",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "strategy": lot.strategy,
                            "reason": reason,
                            "mark": mark,
                            "entry": lot.entry_price,
                            "emergency": is_emergency_flatten_reason(reason),
                        }
                    },
                )
                continue
            fill_px = paper_px
            fee_usd = 0.0
            slip = settings.slippage_bps
            if live:
                assert broker is not None
                try:
                    # Emergency flatten may use market; normal exits prefer maker at/near ask.
                    if is_emergency_flatten_reason(reason):
                        order = broker.create_market_order(product, "sell", float(lot.qty))
                    else:
                        from snowball.maker import maker_sell_price

                        sell_px = maker_sell_price(ticker.bid, ticker.ask, ticker.last)
                        if sell_px is None:
                            log.info(
                                "live sell skipped no ask for maker",
                                extra={
                                    "data": {
                                        "product": product,
                                        "position_id": lot.id,
                                        "bid": ticker.bid,
                                        "ask": ticker.ask,
                                    }
                                },
                            )
                            continue
                        timeout = float(
                            getattr(settings, "maker_timeout_seconds", 90.0) or 90.0
                        )
                        order = broker.create_maker_limit_order(
                            product,
                            "sell",
                            float(lot.qty),
                            price=sell_px,
                            bid=ticker.bid,
                            ask=ticker.ask,
                            timeout_sec=timeout,
                        )
                except Exception as exc:  # noqa: BLE001 — keep loop alive
                    log.exception(
                        "live sell failed; skip lot",
                        extra={
                            "data": {
                                "product": product,
                                "position_id": lot.id,
                                "error": str(exc),
                            }
                        },
                    )
                    continue
                fill_px, fill_qty, fee_usd = parse_order_fill(
                    order if isinstance(order, dict) else {}
                )
                if fill_px <= 0 or fill_qty <= 0:
                    log.warning(
                        "live sell unfilled/canceled maker limit; skip ledger",
                        extra={"data": {"product": product, "position_id": lot.id, "order": order}},
                    )
                    continue
                slip = 0.0
                # Prefer exchange qty if present; else lot qty already used on the order.
                _ = fill_qty
            assert fill_px is not None
            fill = self.state.ledger.close_position(
                position_id=lot.id,
                fill_px=fill_px,
                slippage_bps=slip,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
            )
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
            paused = self.state.ledger.record_closed_trade_for_pause(
                product,
                realized,
                now=now,
                enabled=settings.pair_pause_enabled,
                loss_threshold=settings.pair_pause_losses,
                pause_hours=settings.pair_pause_hours,
            )
            if paused:
                log.warning(
                    "pair paused",
                    extra={"data": paused},
                )
            sell_data = {
                "product": product,
                "strategy": lot.strategy,
                "qty": fill.qty,
                "price": fill.price,
                "fee_usd": fee_usd,
                "reason": reason,
                "position_id": lot.id,
                "entry_price": lot.entry_price,
                "realized": realized,
            }
            if fill.price < lot.entry_price:
                # Should be unreachable when refuse_red is on; belt-and-suspenders.
                log.error(
                    "refusing to record red sell",
                    extra={"data": sell_data},
                )
                continue
            log.info(
                "live sell" if live else "paper sell",
                extra={"data": sell_data},
            )
        self.state.pairs[product].open_count = self.state.ledger.open_count(product)

    def _flatten_all(self, marks: dict[str, float], reason: str, now: datetime) -> None:
        for product in self.state.settings.product_list:
            lots = self.state.ledger.open_positions(product)
            if lots:
                self._close_lots(product, lots, marks, reason=reason, now=now)

    def run_forever(self) -> None:
        interval = max(1.0, float(self.state.settings.poll_seconds))
        log.info("engine loop start", extra={"data": {"poll_seconds": interval}})
        while self.state.running:
            started = time.monotonic()
            try:
                self.tick()
            except LiveTradingRefused:
                log.exception("live trading refused; engine stopping")
                self.state.running = False
                raise
            except Exception:
                log.exception("tick failed")
                self.state.last_error = "tick failed"
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(0.05, remaining)
            while self.state.running and time.monotonic() < deadline:
                time.sleep(0.2)

    def _apply_pair_pause_clear_file(self) -> None:
        """Manual clear via optional file: products listed comma/newline, then file deleted."""
        path = self.state.settings.pair_pause_clear_file
        if path is None:
            return
        p = Path(path)
        if not p.is_file():
            return
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            log.exception("pair pause clear file read failed")
            return
        products: list[str] = []
        for part in raw.replace(",", "\n").splitlines():
            name = part.strip().upper()
            if name:
                products.append(name)
        for product in products:
            if self.state.ledger.clear_pair_pause(product):
                log.info(
                    "pair pause cleared",
                    extra={"data": {"product": product, "via": "clear_file"}},
                )
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.exception("pair pause clear file delete failed")


def build_state(
    settings: Settings | None = None,
    market: MarketData | None = None,
    exchange: object | None = None,
) -> tuple[AppState, Engine]:
    settings = settings or Settings(_env_file=".env")
    # MODE=live without LIVE_ENABLED still refuses. Dual-gated live is allowed.
    settings.assert_not_accidentally_live()
    broker = make_broker(settings, exchange=exchange)
    ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    if broker is not None:
        try:
            free_usd = broker.fetch_free_usd()
            ledger.set_cash_usd(free_usd)
            log.warning(
                "live cash synced from exchange",
                extra={"data": {"free_usd": free_usd}},
            )
        except Exception as exc:  # noqa: BLE001 — start anyway; buys will re-fetch
            log.exception(
                "live cash sync at startup failed; continuing",
                extra={"data": {"error": str(exc)}},
            )
    state = AppState(settings=settings, ledger=ledger, broker=broker)
    for product in settings.product_list:
        state.pairs[product] = PairSnapshot(
            product=product, max_open=settings.max_positions_per_pair
        )
    if settings.watcher_enabled:
        from snowball.watcher.store import WatcherStore

        state.watcher = WatcherStore(settings.sqlite_path)
    if settings.yolo_demon_enabled:
        from snowball.yolo_demon.store import YoloStore

        state.yolo = YoloStore(settings.sqlite_path)
    if settings.clerk_enabled:
        from snowball.clerk.poller import clerk_db_path
        from snowball.clerk.store import ClerkStore

        state.clerk = ClerkStore(clerk_db_path(settings))
    if getattr(settings, "earnings_enabled", True):
        try:
            from snowball.earnings.poller import earnings_db_path
            from snowball.earnings.store import EarningsStore

            state.earnings = EarningsStore(earnings_db_path(settings))
        except ImportError:
            log.warning("Earnings Scout package not present; skipping attach")
    if settings.stock_enabled:
        from snowball.stocks.engine import attach_stock_lane

        settings.assert_stock_config()
        attach_stock_lane(state)
    if settings.futures_enabled:
        from snowball.futures.engine import attach_futures_lane

        settings.assert_futures_config()
        attach_futures_lane(state)
    if getattr(settings, "crash_enabled", False):
        from snowball.crash.engine import attach_crash_lane

        settings.assert_crash_config()
        attach_crash_lane(state)
    if getattr(settings, "fed_enabled", False):
        from snowball.fed.engine import attach_fed_lane

        settings.assert_fed_config()
        attach_fed_lane(state)
    market = market or CcxtMarket(settings)
    return state, Engine(state, market)


def main() -> None:
    settings = Settings(_env_file=".env")
    setup_logging(settings.log_level)
    log.info(
        "snowball starting",
        extra={
            "data": {
                "mode": settings.mode,
                "live_enabled": settings.live_enabled,
                "products": settings.product_list,
                "strategies": settings.strategy_list,
                "paper": settings.mode == "paper" and not settings.live_enabled,
                "stock_enabled": settings.stock_enabled,
                "stock_mode": settings.stock_mode,
                "stock_live_enabled": settings.stock_live_enabled,
                "lane_budgets": settings.lane_budget_pcts(),
                "min_take_profit_pct": settings.min_take_profit_pct,
                "sma_min_take_profit_pct": settings.sma_min_take_profit_pct,
                "fee_buffer_pct": settings.fee_buffer_pct,
                "effective_take_profit_floor": settings.effective_min_take_profit_pct(),
                "effective_sma_take_profit_floor": settings.effective_min_take_profit_pct_for("sma_15m"),
                "futures_enabled": settings.futures_enabled,
                "futures_mode": settings.futures_mode,
            }
        },
    )
    try:
        state, engine = build_state(settings)
    except LiveTradingRefused as exc:
        log.error(str(exc))
        raise SystemExit(2) from exc

    def _stop(*_args: Any) -> None:
        log.info("shutdown signal")
        state.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    worker = threading.Thread(target=engine.run_forever, name="snowball-engine", daemon=True)
    worker.start()

    extra_threads: list[threading.Thread] = []
    if settings.watcher_enabled and state.watcher is not None:
        from snowball.watcher.poller import WatcherSidecar

        sidecar = WatcherSidecar(settings, state.watcher, running=state)
        state.watcher_sidecar = sidecar
        t_w = threading.Thread(target=sidecar.run_forever, name="snowball-watcher", daemon=True)
        t_w.start()
        extra_threads.append(t_w)
        log.info("The Watcher thread started")
    if settings.yolo_demon_enabled and state.yolo is not None:
        from snowball.yolo_demon.poller import YoloDemonSidecar

        yolo = YoloDemonSidecar(settings, state.yolo, running=state)
        state.yolo_sidecar = yolo
        t_y = threading.Thread(target=yolo.run_forever, name="snowball-yolo-demon", daemon=True)
        t_y.start()
        extra_threads.append(t_y)
        log.info("Yolo Demon thread started")

    if settings.clerk_enabled and state.clerk is not None:
        from snowball.clerk.poller import ClerkSidecar

        clerk = ClerkSidecar(settings, state.clerk, running=state)
        state.clerk_sidecar = clerk
        t_c = threading.Thread(target=clerk.run_forever, name="snowball-clerk", daemon=True)
        t_c.start()
        extra_threads.append(t_c)
        log.info("The Clerk thread started")

    if getattr(settings, "earnings_enabled", True) and getattr(state, "earnings", None) is not None:
        from snowball.earnings.poller import EarningsScout

        earnings = EarningsScout(settings, state.earnings, running=state)
        state.earnings_sidecar = earnings
        t_e = threading.Thread(target=earnings.run_forever, name="snowball-earnings-scout", daemon=True)
        t_e.start()
        extra_threads.append(t_e)
        log.info("Earnings Scout thread started")

    if settings.stock_enabled and state.stock_engine is not None:
        stock_engine = state.stock_engine
        t_s = threading.Thread(
            target=stock_engine.run_forever, name="snowball-stocks-paper", daemon=True
        )
        t_s.start()
        extra_threads.append(t_s)
        log.info(
            "STOCK PAPER thread started",
            extra={
                "data": {
                    "active": len(state.stock_universe_active or []),
                    "sqlite": str(settings.stock_sqlite_path),
                    "mark_source": state.stock_mark_source,
                }
            },
        )

    if settings.futures_enabled and state.futures_engine is not None:
        futures_engine = state.futures_engine
        t_f = threading.Thread(
            target=futures_engine.run_forever, name="snowball-futures", daemon=True
        )
        t_f.start()
        extra_threads.append(t_f)
        log.info(
            "Future Trader thread started",
            extra={
                "data": {
                    "products": settings.futures_product_list,
                    "sqlite": str(settings.futures_sqlite_path),
                    "mark_source": state.futures_mark_source,
                    "strategies": settings.futures_strategy_list,
                    "futures_mode": settings.futures_mode,
                    "futures_live_enabled": settings.futures_live_enabled,
                    "budget_pct": settings.futures_account_budget_pct,
                }
            },
        )

    if getattr(settings, "crash_enabled", False) and getattr(state, "crash_engine", None) is not None:
        crash_engine = state.crash_engine
        t_cg = threading.Thread(
            target=crash_engine.run_forever, name="snowball-crash", daemon=True
        )
        t_cg.start()
        extra_threads.append(t_cg)
        log.info(
            "Crash Guard thread started",
            extra={
                "data": {
                    "products": settings.crash_product_list,
                    "sqlite": str(settings.crash_sqlite_path),
                    "mark_source": state.crash_mark_source,
                    "crash_mode": settings.crash_mode,
                    "crash_live_enabled": settings.crash_live_enabled,
                    "budget_pct": settings.crash_account_budget_pct,
                    "live_orders": settings.crash_live_orders_permitted(),
                }
            },
        )

    if getattr(settings, "fed_enabled", False) and getattr(state, "fed_engine", None) is not None:
        fed_engine = state.fed_engine
        t_fd = threading.Thread(
            target=fed_engine.run_forever, name="snowball-fed", daemon=True
        )
        t_fd.start()
        extra_threads.append(t_fd)
        log.info(
            "Fed Desk thread started",
            extra={
                "data": {
                    "products": settings.fed_product_list,
                    "sqlite": str(settings.fed_sqlite_path),
                    "mark_source": state.fed_mark_source,
                    "fed_mode": settings.fed_mode,
                    "fed_live_enabled": settings.fed_live_enabled,
                    "budget_pct": settings.fed_account_budget_pct,
                    "live_orders": settings.fed_live_orders_permitted(),
                }
            },
        )

    if settings.dashboard_enabled:
        import uvicorn

        from snowball.dashboard import create_app

        app = create_app(state)
        log.info(
            "dashboard bind",
            extra={"data": {"host": settings.dashboard_host, "port": settings.dashboard_port}},
        )
        uvicorn.run(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
        state.running = False
    else:
        while state.running:
            time.sleep(0.5)

    worker.join(timeout=5)
    for th in extra_threads:
        th.join(timeout=5)
    log.info("snowball stopped")
