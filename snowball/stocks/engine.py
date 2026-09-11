"""STOCK engine — isolated ledger; paper (Yahoo) or dual-gated live INTX equity perps.

Live stock trading uses Coinbase `{SYM}-PERP-INTX` only (no US equity spot).
Requires STOCK_MODE=live AND STOCK_LIVE_ENABLED=true. Long-only. Never sell red.
Budget = stock_account_budget_pct (default 35%) of Coinbase account value.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from snowball.allocation import leg_notional_usd, open_notional_usd
from snowball.config import LiveTradingRefused, Settings
from snowball.gates import (
    indicator_filters_allow,
    indicator_snapshot_for_strategy,
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
from snowball.stocks.market import (
    StockMarkRouter,
    YahooPaperMarket,
    resolve_coinbase_equity_ids,
    resolve_coinbase_equity_perps,
)
from snowball.stocks.universe import build_stock_universe, normalize_symbol
from snowball.gates import sma_fast_for_strategy, sma_slow_for_strategy
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

log = logging.getLogger("snowball.stocks.engine")

LIVE_REASON_TAG = ":live"


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
    max_notional: float | None = None,
) -> RiskContext:
    """Stock risk context. Live dual-gate is separate from crypto MODE/LIVE_ENABLED."""
    live = settings.stock_live_orders_permitted()
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
        max_position_notional_usd=(
            float(max_notional)
            if max_notional is not None
            else settings.stock_max_notional_usd
        ),
        entry_cooldown=timedelta(seconds=cooldown_seconds),
        mode="live" if live else "paper",
        live_enabled=live,
    )


def _sma_slow(snap: PairSnapshot, strategy_id: str) -> float | None:
    return sma_slow_for_strategy(snap, strategy_id)


def _sma_fast(snap: PairSnapshot, strategy_id: str) -> float | None:
    return sma_fast_for_strategy(snap, strategy_id)


def _signal_for(snap: PairSnapshot, strategy_id: str) -> tuple[Signal, bool]:
    return signal_for_strategy(snap, strategy_id)


class StockPaperEngine:
    """Long-only stock lane. Paper=Yahoo; live=Coinbase INTX equity perps (dual-gated)."""

    def __init__(self, state: AppState, market: Any | None = None) -> None:
        self.state = state
        self.market = market or YahooPaperMarket()
        self._cb_market: Any | None = None  # CoinbaseFuturesMarket when live
        self._last_universe_refresh = 0.0
        self._universe_refresh_seconds = 300.0
        self._live_position_ids: set[int] = set()
        self._last_budget: dict[str, float] = {
            "account_value_usd": 0.0,
            "budget_usd": 0.0,
            "open_notional_usd": 0.0,
        }
        self._perp_map: dict[str, str] = {}

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

    def refresh_perp_map(self) -> dict[str, str]:
        settings = self.state.settings
        # Do not double-trade Future Trader's SPY/QQQ INTX products in the stock lane.
        exclude: set[str] = set()
        for p in settings.futures_product_list:
            base = p.split("-")[0].upper()
            if base:
                exclude.add(base)
        wanted = list(self.state.stock_universe_active or [])
        try:
            mapping = resolve_coinbase_equity_perps(wanted, exclude_bases=exclude)
        except Exception as exc:  # noqa: BLE001
            log.exception("stock INTX perp probe failed", extra={"data": {"error": str(exc)}})
            mapping = {}
        self._perp_map = mapping
        self.state.stock_coinbase_ids = dict(mapping)
        # Keep router in sync when using StockMarkRouter
        if isinstance(self.market, StockMarkRouter):
            self.market.perp_map = dict(mapping)
            self.market.prefer_coinbase = settings.stock_live_orders_permitted()
            self.market.coinbase = self._cb_market
        log.info(
            "stock INTX perp map",
            extra={
                "data": {
                    "mapped": len(mapping),
                    "sample": dict(list(mapping.items())[:12]),
                    "excluded_ft": sorted(exclude),
                }
            },
        )
        return mapping

    def _tradeable_products(self) -> list[str]:
        settings = self.state.settings
        active = list(self.state.stock_universe_active or [])
        if not settings.stock_live_orders_permitted():
            return active
        # Live: only symbols with an INTX perp mapping
        return [p for p in active if normalize_symbol(p) in self._perp_map]

    def _refresh_budget(self, marks: dict[str, float]) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        account_value = 0.0
        if settings.stock_live_orders_permitted() and self._cb_market is not None:
            try:
                crypto_marks = {}
                try:
                    crypto_marks = self.state.marks()
                except Exception:  # noqa: BLE001
                    crypto_marks = {}
                account_value = float(
                    self._cb_market.fetch_account_value_usd(crypto_marks=crypto_marks)
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "stock account value fetch failed; falling back to ledger equity",
                    extra={"data": {"error": str(exc)}},
                )
                account_value = float(ledger.equity_usd(marks))
        else:
            account_value = float(ledger.equity_usd(marks))
            if account_value <= 0:
                account_value = float(settings.stock_bankroll_usd)
        pct = float(settings.stock_account_budget_pct)
        budget = max(0.0, account_value * pct)
        # Live budget tracks exchange-backed lots only; legacy paper lots are
        # ledger accounting and must not block INTX deployment.
        if settings.stock_live_orders_permitted():
            live_lots = [
                lot
                for lot in ledger.open_positions()
                if self._lot_is_live_backed(lot)
            ]
            open_n = open_notional_usd(live_lots)
        else:
            open_n = open_notional_usd(ledger.open_positions())
        self._last_budget = {
            "account_value_usd": account_value,
            "budget_usd": budget,
            "open_notional_usd": open_n,
        }
        self.state.stock_account_value_usd = account_value  # type: ignore[attr-defined]
        self.state.stock_budget_usd = budget  # type: ignore[attr-defined]

    def tick(self) -> None:
        settings = self.state.settings
        if not settings.stock_enabled:
            return
        # Dual-gate consistency: refuse tick on half-configured live
        try:
            settings.assert_stock_config()
        except LiveTradingRefused as exc:
            log.error("stock config refused; skipping tick", extra={"data": {"error": str(exc)}})
            return
        ledger = self.state.stock_ledger
        if ledger is None:
            return

        self.refresh_universe()
        now = utcnow()
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)

        marks: dict[str, float] = {}
        for product in list(self.state.stock_universe_active):
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        self._refresh_budget(marks)

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

        tradeable = set(self._tradeable_products())
        for product in list(self.state.stock_universe_active):
            # Always manage exits for open lots; entries only on tradeable
            self._act_on_pair(
                product=product,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=killed,
                marks=marks,
                entries_allowed=product in tradeable,
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

        wanted = set(settings.stock_strategy_list)
        frames = enabled_timeframes(settings.stock_strategy_list)
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
                if timeframe == "15m" and SMA_15M in wanted and len(closes) >= settings.sma_slow:
                    snap.sma_fast = fast_v
                    snap.sma_slow = slow_v
                    snap.signal = sig.value
                    snap.candle_ts = candle_ts
                elif timeframe == "5m" and SMA_5M in wanted and len(closes) >= settings.sma_slow:
                    snap.sma_fast_5m = fast_v
                    snap.sma_slow_5m = slow_v
                    snap.signal_5m = sig.value
                    snap.candle_ts_5m = candle_ts
                elif timeframe == "1d" and SMA_1D in wanted:
                    snap.sma_fast_1d = fast_v
                    snap.sma_slow_1d = slow_v
                    snap.signal_1d = sig.value
                    snap.candle_ts_1d = candle_ts
                    if snap.sma_fast is None and fast_v is not None:
                        snap.sma_fast = fast_v
                        snap.sma_slow = slow_v
                        snap.signal = sig.value
                        snap.candle_ts = candle_ts
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
        entries_allowed: bool = True,
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
                entries_allowed=entries_allowed,
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
        entries_allowed: bool = True,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs[product]

        if strategy_id not in MEAN_REVERSION_STRATEGY_IDS:
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
        fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)

        if strategy_lots and want_signal_exit:
            for lot in strategy_lots:
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=fee_buf,
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
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=fee_buf,
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

        if want_signal_exit or not want_entry or not entries_allowed:
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

        use_trend = settings.trend_filter_enabled and strategy_id not in MEAN_REVERSION_STRATEGY_IDS
        ok_tf, reason_tf = trend_filter_allows(
            snap.last,
            _sma_slow(snap, strategy_id),
            enabled=use_trend,
        )
        if not ok_tf:
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
                "stock entry blocked",
                extra={
                    "data": {
                        "product": product,
                        "strategy": strategy_id,
                        "reason": reason_ind,
                    }
                },
            )
            return

        notional = self._target_leg_notional()
        if notional <= 1e-6:
            log.info(
                "stock entry skipped: budget full",
                extra={
                    "data": {
                        "product": product,
                        "budget": self._last_budget,
                    }
                },
            )
            return

        ctx = _stock_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(all_lots),
            last_entry_at=ledger.last_entry_at(product, strategy_id),
            cash_usd=max(ledger.cash_usd(), notional * 2),
            requested_notional=notional,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            max_notional=notional,
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            return
        entry_reason = f"{strategy_id}:scale_in" if is_scale_in else f"{strategy_id}:enter"
        self._open_lot(
            product, marks, reason=entry_reason, now=now, strategy=strategy_id, notional_usd=notional
        )

    def _target_leg_notional(self) -> float:
        settings = self.state.settings
        budget = float(self._last_budget.get("budget_usd") or 0.0)
        open_n = float(self._last_budget.get("open_notional_usd") or 0.0)
        av = float(self._last_budget.get("account_value_usd") or 0.0)
        per_leg = settings.effective_per_leg_notional_usd(av)
        return leg_notional_usd(
            budget_usd=budget,
            open_notional_usd=open_n,
            max_notional_usd=per_leg,
            target_legs=8,
        )

    def _open_lot(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        strategy: str,
        notional_usd: float | None = None,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.stock_ledger
        assert ledger is not None
        target = float(
            notional_usd if notional_usd is not None else settings.per_leg_base_usd
        )
        if settings.stock_live_orders_permitted():
            self._open_lot_live(
                product=product,
                reason=reason,
                now=now,
                strategy=strategy,
                notional_usd=target,
            )
            return
        snap = self.state.stock_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        px = fill_price(ticker, "buy", settings.slippage_bps)
        notional = min(target, ledger.cash_usd())
        if notional <= 0:
            return
        pos, fill = ledger.open_buy(
            product=product,
            fill_px=px,
            notional_usd=notional,
            slippage_bps=settings.slippage_bps,
            fee_usd=0.0,
            reason=reason,
            ts=now,
            strategy=strategy,
        )
        self.state.stock_pairs[product].open_count = ledger.open_count(product)
        self._last_budget["open_notional_usd"] = open_notional_usd(ledger.open_positions())
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

    def _open_lot_live(
        self,
        product: str,
        reason: str,
        now: datetime,
        strategy: str,
        notional_usd: float,
    ) -> None:
        settings = self.state.settings
        if not settings.stock_live_orders_permitted():
            raise LiveTradingRefused("stock live open refused: dual gate not set")
        if self._cb_market is None:
            log.error("stock live open refused: no coinbase market")
            return
        perp = self._perp_map.get(normalize_symbol(product))
        if not perp:
            log.info(
                "stock live open skipped: no INTX perp",
                extra={"data": {"product": product}},
            )
            return
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs[product]
        ref = snap.last
        if ref is None or ref <= 0:
            log.warning(
                "stock live buy skipped: no mark",
                extra={"data": {"product": product, "perp": perp}},
            )
            return
        from snowball.futures.market import parse_futures_order_fill, round_amount_down

        amount = round_amount_down(notional_usd / float(ref), 0.01)
        if amount < 0.01:
            log.info(
                "stock live buy skipped: amount below min",
                extra={
                    "data": {
                        "product": product,
                        "perp": perp,
                        "notional": notional_usd,
                        "ref": ref,
                        "amount": amount,
                    }
                },
            )
            return
        live_reason = reason if reason.endswith(LIVE_REASON_TAG) else f"{reason}{LIVE_REASON_TAG}"
        bid = getattr(snap, "bid", None)
        ask = getattr(snap, "ask", None)
        try:
            from snowball.maker import maker_buy_price

            limit_px = maker_buy_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                bid2, ask2 = self._cb_market.fetch_bba(perp)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_buy_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "stock live buy skipped: no book bid for maker",
                    extra={"data": {"product": product, "perp": perp, "bid": bid, "ask": ask}},
                )
                return
            amount = round_amount_down(notional_usd / float(limit_px), 0.01)
            if amount < 0.01:
                log.info(
                    "stock live buy skipped: amount below min",
                    extra={"data": {"product": product, "perp": perp, "limit_px": limit_px}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self._cb_market.create_swap_maker_limit_order(
                perp,
                "buy",
                amount,
                price=limit_px,
                bid=bid,
                ask=ask,
                leverage=1.0,
                reduce_only=False,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "stock live buy failed",
                extra={"data": {"product": product, "perp": perp, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "stock live buy unfilled/canceled maker limit",
                extra={"data": {"product": product, "perp": perp, "order_id": order.get("id")}},
            )
            return
        cost = fill_qty * fill_px + fee_usd
        if ledger.cash_usd() + 1e-9 < cost:
            try:
                ledger.set_cash_usd(cost * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("stock live ledger cash sync failed")
        pos, fill = ledger.open_buy(
            product=product,
            fill_px=fill_px,
            notional_usd=fill_qty * fill_px,
            slippage_bps=0.0,
            fee_usd=fee_usd,
            reason=live_reason,
            ts=now,
            strategy=strategy,
        )
        self._live_position_ids.add(int(pos.id))
        self.state.stock_pairs[product].open_count = ledger.open_count(product)
        self._last_budget["open_notional_usd"] = open_notional_usd(ledger.open_positions())
        log.warning(
            "stock LIVE buy (INTX perp)",
            extra={
                "data": {
                    "product": product,
                    "perp": perp,
                    "strategy": strategy,
                    "qty": fill_qty,
                    "price": fill_px,
                    "notional": fill.notional_usd,
                    "fee": fee_usd,
                    "reason": live_reason,
                    "position_id": pos.id,
                    "order_id": order.get("id"),
                    "mark_source": "coinbase_intx_perp",
                }
            },
        )

    def _lot_is_live_backed(self, lot: Position) -> bool:
        if int(lot.id) in self._live_position_ids:
            return True
        # Recover across restarts: inspect buy fill reason for :live tag
        ledger = self.state.stock_ledger
        if ledger is None:
            return False
        try:
            for fill in ledger.recent_fills(200):
                if (
                    fill.position_id == lot.id
                    and fill.side == "buy"
                    and LIVE_REASON_TAG in (fill.reason or "")
                ):
                    self._live_position_ids.add(int(lot.id))
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _close_lots(
        self,
        product: str,
        lots: list[Position],
        marks: dict[str, float],
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        live_lots = [lot for lot in lots if self._lot_is_live_backed(lot)]
        paper_lots = [lot for lot in lots if not self._lot_is_live_backed(lot)]
        if live_lots and settings.stock_live_orders_permitted():
            self._close_lots_live(product, live_lots, reason=reason, now=now)
        elif live_lots and not settings.stock_live_orders_permitted():
            # Dual gate dropped — refuse exchange sells; hold live-backed lots
            log.error(
                "stock live close refused: dual gate off; holding live-backed lots",
                extra={"data": {"product": product, "ids": [l.id for l in live_lots]}},
            )
        if paper_lots:
            self._close_lots_paper(product, paper_lots, marks, reason=reason, now=now)

    def _close_lots_paper(
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
        refuse_red = bool(settings.never_sell_red) or bool(
            getattr(settings, "never_sell_red_emergency", True)
        )
        for lot in lots:
            if refuse_red and paper_px < lot.entry_price:
                log.info(
                    "stock paper sell skipped never_sell_red",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "mark": paper_px,
                            "entry": lot.entry_price,
                            "reason": reason,
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
                "venue": "paper",
            }
            log.info("stock paper sell", extra={"data": sell_data})
        self.state.stock_pairs[product].open_count = ledger.open_count(product)
        self._last_budget["open_notional_usd"] = open_notional_usd(ledger.open_positions())

    def _close_lots_live(
        self,
        product: str,
        lots: list[Position],
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        if not settings.stock_live_orders_permitted():
            raise LiveTradingRefused("stock live close refused: dual gate not set")
        if self._cb_market is None:
            log.error("stock live close refused: no coinbase market")
            return
        perp = self._perp_map.get(normalize_symbol(product))
        if not perp:
            log.error(
                "stock live close refused: no INTX perp map",
                extra={"data": {"product": product}},
            )
            return
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs[product]
        from snowball.futures.market import parse_futures_order_fill, round_amount_down

        fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)
        for lot in lots:
            ref = snap.last
            if not is_emergency_flatten_reason(reason):
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot,
                    ref,
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=fee_buf,
                )
                if not ok_sw:
                    log.info(
                        "stock live close skipped",
                        extra={
                            "data": {
                                "product": product,
                                "position_id": lot.id,
                                "reason": reason_sw,
                            }
                        },
                    )
                    continue
            elif settings.never_sell_red or getattr(settings, "never_sell_red_emergency", True):
                if ref is not None and ref < lot.entry_price:
                    log.warning(
                        "stock live emergency close skipped never_sell_red",
                        extra={"data": {"product": product, "position_id": lot.id}},
                    )
                    continue
            amount = round_amount_down(float(lot.qty), 0.01)
            if amount < 0.01:
                continue
            try:
                from snowball.gates import is_emergency_flatten_reason
                from snowball.maker import maker_sell_price

                if is_emergency_flatten_reason(reason):
                    order = self._cb_market.create_swap_market_order(
                        perp, "sell", amount, leverage=1.0, reduce_only=True
                    )
                else:
                    snap_s = self.state.stock_pairs.get(product)
                    bid = getattr(snap_s, "bid", None) if snap_s else None
                    ask = getattr(snap_s, "ask", None) if snap_s else None
                    last = getattr(snap_s, "last", None) if snap_s else None
                    sell_px = maker_sell_price(bid, ask, last)
                    if sell_px is None:
                        bid2, ask2 = self._cb_market.fetch_bba(perp)
                        bid = bid if bid is not None else bid2
                        ask = ask if ask is not None else ask2
                        sell_px = maker_sell_price(bid, ask, last)
                    if sell_px is None:
                        log.info(
                            "stock live sell skipped: no ask for maker",
                            extra={
                                "data": {
                                    "product": product,
                                    "perp": perp,
                                    "position_id": lot.id,
                                }
                            },
                        )
                        continue
                    timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
                    order = self._cb_market.create_swap_maker_limit_order(
                        perp,
                        "sell",
                        amount,
                        price=sell_px,
                        bid=bid,
                        ask=ask,
                        leverage=1.0,
                        reduce_only=True,
                        timeout_sec=timeout,
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "stock live sell failed",
                    extra={"data": {"product": product, "perp": perp, "error": str(exc)}},
                )
                continue
            fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
            if fill_px <= 0 or fill_qty <= 0:
                log.error(
                    "stock live sell unfilled/canceled maker limit",
                    extra={"data": {"product": product, "order_id": order.get("id")}},
                )
                continue
            fill = ledger.close_position(
                position_id=lot.id,
                fill_px=fill_px,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason if reason.endswith(LIVE_REASON_TAG) else f"{reason}{LIVE_REASON_TAG}",
                ts=now,
            )
            self._live_position_ids.discard(int(lot.id))
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
            log.warning(
                "stock LIVE sell (INTX perp)",
                extra={
                    "data": {
                        "product": product,
                        "perp": perp,
                        "strategy": lot.strategy,
                        "qty": fill_qty,
                        "price": fill_px,
                        "fee": fee_usd,
                        "reason": reason,
                        "position_id": lot.id,
                        "realized": realized,
                        "order_id": order.get("id"),
                    }
                },
            )
        self.state.stock_pairs[product].open_count = ledger.open_count(product)
        self._last_budget["open_notional_usd"] = open_notional_usd(ledger.open_positions())

    def _flatten_all(self, marks: dict[str, float], reason: str, now: datetime) -> None:
        ledger = self.state.stock_ledger
        assert ledger is not None
        for product in list(self.state.stock_universe_active):
            lots = ledger.open_positions(product)
            if lots:
                self._close_lots(product, lots, marks, reason=reason, now=now)

    def run_forever(self) -> None:
        interval = max(5.0, float(self.state.settings.stock_poll_seconds))
        live = self.state.settings.stock_live_orders_permitted()
        log.info(
            "stock loop start",
            extra={
                "data": {
                    "poll_seconds": interval,
                    "live": live,
                    "mark_source": getattr(self.market, "mark_source", None),
                }
            },
        )
        self.refresh_perp_map()
        # Informational spot probe (expected empty)
        try:
            ids = resolve_coinbase_equity_ids(list(self.state.stock_universe_active or [])[:40])
            if ids:
                log.info("coinbase spot equity ids (unexpected)", extra={"data": ids})
        except Exception:  # noqa: BLE001
            log.exception("coinbase spot equity probe failed")

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
        log.info("stock lane disabled")
        return None
    settings.assert_stock_config()
    ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.stock_ledger = ledger
    live = settings.stock_live_orders_permitted()

    cb_market = None
    if live:
        from snowball.futures.market import CoinbaseFuturesMarket

        cb_market = CoinbaseFuturesMarket(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret,
            api_passphrase=settings.coinbase_api_passphrase,
            allow_orders=True,
        )
        market: Any = StockMarkRouter(
            yahoo=YahooPaperMarket(),
            coinbase=cb_market,
            perp_map={},
            prefer_coinbase=True,
        )
        state.stock_mark_source = "coinbase_intx_perp"
    else:
        market = YahooPaperMarket()
        state.stock_mark_source = "yahoo_paper"

    engine = StockPaperEngine(state, market=market)
    engine._cb_market = cb_market
    engine.refresh_universe(force=True)
    if live:
        engine.refresh_perp_map()
    state.stock_engine = engine
    log.warning(
        "stock lane attached",
        extra={
            "data": {
                "sqlite": str(settings.stock_sqlite_path),
                "bankroll": settings.stock_bankroll_usd,
                "active": len(state.stock_universe_active or []),
                "strategies": settings.stock_strategy_list,
                "mark_source": state.stock_mark_source,
                "stock_mode": settings.stock_mode,
                "stock_live_enabled": settings.stock_live_enabled,
                "live_orders": live,
                "budget_pct": settings.stock_account_budget_pct,
                "perp_mapped": len(engine._perp_map),
            }
        },
    )
    return engine
