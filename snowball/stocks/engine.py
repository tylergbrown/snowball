"""STOCK engine — isolated ledger; paper (Yahoo) or dual-gated live CFM indexes.

Live stock trading uses Coinbase CFM CDE ``US5-19DEC30-CDE`` + ``TEK-19DEC30-CDE``
(same integer-contract path as FT/Crash/Fed). Does **not** place single-name
``*-PERP-INTX`` orders. Requires STOCK_MODE=live AND STOCK_LIVE_ENABLED=true.
Long-only. Never sell red.
Budget: when CRYPTO_STOCK_SHARED_BUDGET=true (default), stock+crypto share
AV*(crypto_pct+stock_pct) (~65%); open notional counts live-backed stock lots
plus crypto ledger open. When sharing is off, budget = stock_account_budget_pct
alone (default 25%). Max 1 CFM lot per index; skip if Crash is short the same product.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from snowball.allocation import leg_notional_usd, open_notional_usd, spot_open_notional_usd, spot_shared_budget_usd
from snowball.config import LiveTradingRefused, Settings
from snowball.gates import (
    indicator_filters_allow,
    indicator_snapshot_for_strategy,
    is_emergency_flatten_reason,
    is_stall_exit_reason,
    lot_unrealized_pnl_pct,
    momentum_fading,
    price_stalled,
    scale_in_allowed,
    stall_exit_allowed,
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
)
from snowball.stocks.universe import build_stock_universe, normalize_symbol
from snowball.futures.market import (
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
)
from snowball.futures.session import (
    DEFAULT_CASH_END,
    DEFAULT_CASH_START,
    in_us_cash_session,
    _parse_hhmm,
)
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
    """Long-only stock lane. Paper=Yahoo; live=Coinbase CFM US5/TEK (dual-gated)."""

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
        if settings.stock_live_orders_permitted():
            live_prods = list(settings.stock_product_list)
            self.state.stock_universe_all = list(live_prods)
            self.state.stock_universe_active = list(live_prods)
            self.state.stock_universe_dynamic = []
            self.state.stock_universe_sources = {p: "cfm_live" for p in live_prods}
        else:
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
        """Map live stock products → CFM CDE ids (identity). No INTX single-names."""
        settings = self.state.settings
        mapping: dict[str, str] = {}
        if settings.stock_live_orders_permitted():
            for raw in settings.stock_product_list:
                pid = normalize_futures_product(raw)
                if not is_cfm_product(pid):
                    log.error(
                        "stock live product refused: not CFM CDE",
                        extra={"data": {"product": raw, "normalized": pid}},
                    )
                    continue
                mapping[pid] = pid
                # Also key common aliases for lookups
                mapping[normalize_symbol(pid)] = pid
        self._perp_map = mapping
        self.state.stock_coinbase_ids = {k: v for k, v in mapping.items() if is_cfm_product(v)}
        if isinstance(self.market, StockMarkRouter):
            self.market.perp_map = dict(mapping)
            self.market.prefer_coinbase = settings.stock_live_orders_permitted()
            self.market.coinbase = self._cb_market
        log.info(
            "stock CFM product map",
            extra={
                "data": {
                    "mapped": len({v for v in mapping.values()}),
                    "products": sorted({v for v in mapping.values()}),
                    "venue": "CFM_CDE",
                    "intx_live": False,
                }
            },
        )
        return mapping

    def _order_product_id(self, product: str) -> str | None:
        """Resolve CFM product id for live orders."""
        if product in self._perp_map:
            return self._perp_map[product]
        try:
            norm = normalize_futures_product(product)
        except Exception:
            norm = product.strip().upper()
        if norm in self._perp_map:
            return self._perp_map[norm]
        sym = normalize_symbol(product)
        return self._perp_map.get(sym)

    def _crash_has_opposing_short(self, product: str) -> bool:
        """True when Crash Guard holds a short on the same CFM index."""
        store = getattr(self.state, "crash_ledger", None)
        if store is None:
            return False
        try:
            pid = normalize_futures_product(product)
        except Exception:
            pid = product
        try:
            lots = store.open_positions(pid)
        except Exception:  # noqa: BLE001
            return False
        return any(str(getattr(lot, "side", "")).lower() == "short" for lot in lots)

    def _tradeable_products(self) -> list[str]:
        settings = self.state.settings
        active = list(self.state.stock_universe_active or [])
        if not settings.stock_live_orders_permitted():
            return active
        # Live: only mapped CFM CDE products
        out: list[str] = []
        seen: set[str] = set()
        for p in active:
            pid = self._order_product_id(p)
            if pid and is_cfm_product(pid) and pid not in seen:
                out.append(pid)
                seen.add(pid)
        return out

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
        shared = bool(getattr(settings, "crypto_stock_shared_budget", True))
        # Live budget tracks exchange-backed lots only; legacy paper lots are
        # ledger accounting and must not block CFM deployment.
        if settings.stock_live_orders_permitted():
            stock_lots = [
                lot
                for lot in ledger.open_positions()
                if self._lot_is_live_backed(lot)
            ]
        else:
            stock_lots = list(ledger.open_positions())
        if shared:
            budget = spot_shared_budget_usd(
                account_value,
                settings.crypto_account_budget_pct,
                settings.stock_account_budget_pct,
            )
            crypto_ledger = getattr(self.state, "ledger", None)
            crypto_positions = (
                list(crypto_ledger.open_positions()) if crypto_ledger is not None else []
            )
            open_n = spot_open_notional_usd(crypto_positions, stock_lots)
        else:
            pct = float(settings.stock_account_budget_pct)
            budget = max(0.0, account_value * pct)
            open_n = open_notional_usd(stock_lots)
        self._last_budget = {
            "account_value_usd": account_value,
            "budget_usd": budget,
            "open_notional_usd": open_n,
            "crypto_stock_shared_budget": 1.0 if shared else 0.0,
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

        # Always refresh/manage products with open lots (e.g. CFM TEK/US5), even if
        # paper universe refresh temporarily omits them from the active watchlist.
        manage_products = list(self.state.stock_universe_active)
        for lot in ledger.open_positions():
            if lot.product not in manage_products:
                manage_products.append(lot.product)
                if lot.product not in self.state.stock_pairs:
                    self.state.stock_pairs[lot.product] = PairSnapshot(
                        product=lot.product, max_open=settings.stock_max_positions
                    )

        marks: dict[str, float] = {}
        for product in manage_products:
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
        for product in manage_products:
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
        # Stall detector needs 15m OHLCV even if only 5m/1d strategies are enabled.
        if (
            bool(getattr(settings, "stock_cfm_stall_exit_enabled", True))
            and "15m" not in frames
            and is_cfm_product(product)
        ):
            frames = list(frames) + ["15m"]
        snap.stalled_15m = False
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
                if timeframe == "15m" and bool(
                    getattr(settings, "stock_cfm_stall_exit_enabled", True)
                ):
                    mark_for_stall = snap.last if snap.last is not None else (
                        closes[-1] if closes else None
                    )
                    snap.stalled_15m = price_stalled(
                        highs,
                        lows,
                        closes,
                        mark_for_stall,
                        lookback=int(
                            getattr(settings, "stock_cfm_stall_lookback_bars", 5) or 5
                        ),
                        new_high_tol=float(
                            getattr(settings, "stock_cfm_stall_new_high_tol", 0.002)
                            or 0.002
                        ),
                        range_compress_pct=float(
                            getattr(
                                settings, "stock_cfm_stall_range_compress_pct", 0.006
                            )
                            or 0.006
                        ),
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

        all_lots = ledger.open_positions(product)
        strategy_lots = [lot for lot in all_lots if lot.strategy == strategy_id]
        mark = marks.get(product)
        if mark is None and snap.last is not None:
            mark = snap.last

        lots_to_close: list[Position] = []
        exit_reason = f"{strategy_id}:exit"
        fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)

        # CFM stall early-bank is indicator-independent (runs even while EMA/SMA warm).
        if (
            strategy_lots
            and bool(getattr(settings, "stock_cfm_stall_exit_enabled", True))
            and is_cfm_product(product)
            and bool(getattr(snap, "stalled_15m", False))
        ):
            cash_start = _parse_hhmm(
                getattr(settings, "stock_cfm_stall_start_et", "09:30"),
                DEFAULT_CASH_START,
            )
            cash_end = _parse_hhmm(
                getattr(settings, "stock_cfm_stall_end_et", "15:45"),
                DEFAULT_CASH_END,
            )
            if in_us_cash_session(now, start=cash_start, end=cash_end):
                stall_pct = float(
                    getattr(settings, "stock_cfm_stall_exit_pct", 0.05) or 0.05
                )
                for lot in strategy_lots:
                    ok_st, reason_st = stall_exit_allowed(
                        lot,
                        mark,
                        stall_exit_pct=stall_pct,
                        never_sell_red=settings.never_sell_red,
                    )
                    if ok_st:
                        lots_to_close.append(lot)
                    else:
                        log.info(
                            "stock stall hold",
                            extra={
                                "data": {
                                    "product": product,
                                    "strategy": strategy_id,
                                    "position_id": lot.id,
                                    "reason": reason_st,
                                    "stalled_15m": True,
                                }
                            },
                        )
                if lots_to_close:
                    exit_reason = "stall_take_profit"
                    log.info(
                        "stock stall_take_profit",
                        extra={
                            "data": {
                                "product": product,
                                "strategy": strategy_id,
                                "lots": [lot.id for lot in lots_to_close],
                                "stall_exit_pct": stall_pct,
                                "mark": mark,
                            }
                        },
                    )
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
                            extra={
                                "data": {
                                    "product": product,
                                    "strategy": strategy_id,
                                    "reason": reason,
                                }
                            },
                        )
                        return
                    self._close_lots(
                        product, lots_to_close, marks, reason=exit_reason, now=now
                    )
                    return

        if strategy_id not in MEAN_REVERSION_STRATEGY_IDS:
            if _sma_fast(snap, strategy_id) is None or _sma_slow(snap, strategy_id) is None:
                return

        signal, uptrend = _signal_for(snap, strategy_id)
        max_pos = settings.stock_max_positions
        if settings.stock_live_orders_permitted() and is_cfm_product(product):
            max_pos = min(max_pos, settings.cfm_max_contracts_per_index())

        want_entry = signal is Signal.ENTER or (
            signal is Signal.HOLD and uptrend and 0 < len(strategy_lots) < max_pos
        )
        want_signal_exit = signal is Signal.EXIT and len(strategy_lots) > 0

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
            shared = bool(getattr(settings, "crypto_stock_shared_budget", True))
            log.info(
                "stock entry skipped: spot_budget_full"
                if shared
                else "stock entry skipped: budget full",
                extra={
                    "data": {
                        "product": product,
                        "reason": "spot_budget_full" if shared else "stock_budget_full",
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
        pid = self._order_product_id(product) or normalize_futures_product(product)
        if not pid or not is_cfm_product(pid):
            log.info(
                "stock live open skipped: not a CFM CDE product",
                extra={"data": {"product": product, "pid": pid}},
            )
            return
        if self._crash_has_opposing_short(pid):
            log.warning(
                "stock live open skipped: crash short opposing on same CFM product",
                extra={"data": {"product": pid}},
            )
            return
        ledger = self.state.stock_ledger
        assert ledger is not None
        # Enforce 1 lot per index (CFM_MAX_CONTRACTS)
        max_c = settings.cfm_max_contracts_per_index()
        if ledger.open_count(pid) >= max_c or ledger.open_count(product) >= max_c:
            log.info(
                "stock live open skipped: max CFM contracts for index",
                extra={"data": {"product": pid, "max": max_c}},
            )
            return
        snap = self.state.stock_pairs.get(product) or self.state.stock_pairs.get(pid)
        if snap is None:
            snap = PairSnapshot(product=pid, max_open=max_c)
            self.state.stock_pairs[pid] = snap
        ref = snap.last
        if ref is None or ref <= 0:
            log.warning(
                "stock live buy skipped: no mark",
                extra={"data": {"product": pid}},
            )
            return
        from snowball.futures.market import parse_futures_order_fill

        lev = settings.cfm_order_leverage()
        margin_rate = float(getattr(settings, "cfm_margin_rate", 0.10))
        lane_max = float(settings.stock_max_notional_usd)
        # Prefer lane budget remaining; floor with STOCK_MAX_NOTIONAL inside order_size
        budget = float(self._last_budget.get("budget_usd") or notional_usd)
        open_n = float(self._last_budget.get("open_notional_usd") or 0.0)
        sizing_notional = max(budget - open_n, notional_usd, lane_max)
        avail = max(float(ledger.cash_usd()), sizing_notional, lane_max)
        amount = order_size_for_product(
            pid,
            notional_usd=sizing_notional,
            price=float(ref),
            available_margin_usd=avail,
            max_contracts=max_c,
            leverage=lev,
            margin_rate=margin_rate,
            lane_max_notional_usd=lane_max,
        )
        if amount < 1.0:
            log.info(
                "stock live buy skipped: cannot fund 1 CFM contract",
                extra={
                    "data": {
                        "product": pid,
                        "notional": sizing_notional,
                        "ref": ref,
                        "amount": amount,
                        "lane_max": lane_max,
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
                bid2, ask2 = self._cb_market.fetch_bba(pid)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_buy_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "stock live buy skipped: no book bid for maker",
                    extra={"data": {"product": pid, "bid": bid, "ask": ask}},
                )
                return
            amount = order_size_for_product(
                pid,
                notional_usd=sizing_notional,
                price=float(limit_px),
                available_margin_usd=avail,
                max_contracts=max_c,
                leverage=lev,
                margin_rate=margin_rate,
                lane_max_notional_usd=lane_max,
            )
            if amount < 1.0:
                log.info(
                    "stock live buy skipped: amount below min",
                    extra={"data": {"product": pid, "limit_px": limit_px}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self._cb_market.create_swap_maker_limit_order(
                pid,
                "buy",
                amount,
                price=limit_px,
                bid=bid,
                ask=ask,
                leverage=lev,
                reduce_only=False,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "stock live buy failed",
                extra={"data": {"product": pid, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "stock live buy unfilled/canceled maker limit",
                extra={"data": {"product": pid, "order_id": order.get("id")}},
            )
            return
        cost = fill_qty * fill_px + fee_usd
        if ledger.cash_usd() + 1e-9 < cost:
            try:
                ledger.set_cash_usd(cost * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("stock live ledger cash sync failed")
        # Book under canonical CFM product id
        book_product = pid
        if book_product not in self.state.stock_pairs:
            self.state.stock_pairs[book_product] = PairSnapshot(
                product=book_product, max_open=max_c
            )
        pos, fill = ledger.open_buy(
            product=book_product,
            fill_px=fill_px,
            notional_usd=fill_qty * fill_px,
            slippage_bps=0.0,
            fee_usd=fee_usd,
            reason=live_reason,
            ts=now,
            strategy=strategy,
        )
        self._live_position_ids.add(int(pos.id))
        self.state.stock_pairs[book_product].open_count = ledger.open_count(book_product)
        self._last_budget["open_notional_usd"] = open_notional_usd(
            [lot for lot in ledger.open_positions() if self._lot_is_live_backed(lot)]
        )
        log.warning(
            "stock LIVE buy (CFM CDE)",
            extra={
                "data": {
                    "product": book_product,
                    "strategy": strategy,
                    "qty": fill_qty,
                    "price": fill_px,
                    "notional": fill.notional_usd,
                    "fee": fee_usd,
                    "reason": live_reason,
                    "position_id": pos.id,
                    "order_id": order.get("id"),
                    "mark_source": "coinbase_cfm_cde",
                    "venue": "CFM_CDE",
                    "leverage": lev,
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
        pid = self._order_product_id(product) or normalize_futures_product(product)
        if not pid or not is_cfm_product(pid):
            log.error(
                "stock live close refused: not a CFM CDE product",
                extra={"data": {"product": product, "pid": pid}},
            )
            return
        ledger = self.state.stock_ledger
        assert ledger is not None
        snap = self.state.stock_pairs.get(product) or self.state.stock_pairs.get(pid)
        from snowball.futures.market import parse_futures_order_fill

        fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)
        lev = settings.cfm_order_leverage()
        for lot in lots:
            ref = snap.last if snap is not None else None
            if not is_emergency_flatten_reason(reason):
                if is_stall_exit_reason(reason):
                    # Gross stall floor (no fee buffer); do not apply swing ~7/9% floors.
                    ok_sw, reason_sw = stall_exit_allowed(
                        lot,
                        ref,
                        stall_exit_pct=float(
                            getattr(settings, "stock_cfm_stall_exit_pct", 0.05) or 0.05
                        ),
                        never_sell_red=settings.never_sell_red,
                    )
                else:
                    ok_sw, reason_sw = strategy_exit_allowed(
                        lot,
                        ref,
                        min_take_profit_pct=settings.min_take_profit_pct_for(
                            lot.strategy
                        ),
                        never_sell_red=settings.never_sell_red,
                        fee_buffer_pct=fee_buf,
                    )
                if not ok_sw:
                    log.info(
                        "stock live close skipped",
                        extra={
                            "data": {
                                "product": pid,
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
                        extra={"data": {"product": pid, "position_id": lot.id}},
                    )
                    continue
            amount = float(max(1, int(round(float(lot.qty)))))
            if amount < 1.0:
                continue
            try:
                from snowball.maker import maker_sell_price

                if is_emergency_flatten_reason(reason):
                    order = self._cb_market.create_swap_market_order(
                        pid, "sell", amount, leverage=lev, reduce_only=True
                    )
                else:
                    bid = getattr(snap, "bid", None) if snap else None
                    ask = getattr(snap, "ask", None) if snap else None
                    last = getattr(snap, "last", None) if snap else None
                    sell_px = maker_sell_price(bid, ask, last)
                    if sell_px is None:
                        bid2, ask2 = self._cb_market.fetch_bba(pid)
                        bid = bid if bid is not None else bid2
                        ask = ask if ask is not None else ask2
                        sell_px = maker_sell_price(bid, ask, last)
                    if sell_px is None:
                        log.info(
                            "stock live sell skipped: no ask for maker",
                            extra={
                                "data": {
                                    "product": pid,
                                    "position_id": lot.id,
                                }
                            },
                        )
                        continue
                    timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
                    order = self._cb_market.create_swap_maker_limit_order(
                        pid,
                        "sell",
                        amount,
                        price=sell_px,
                        bid=bid,
                        ask=ask,
                        leverage=lev,
                        reduce_only=True,
                        timeout_sec=timeout,
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "stock live sell failed",
                    extra={"data": {"product": pid, "error": str(exc)}},
                )
                continue
            fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
            if fill_px <= 0 or fill_qty <= 0:
                log.error(
                    "stock live sell unfilled/canceled maker limit",
                    extra={"data": {"product": pid, "order_id": order.get("id")}},
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
                "stock LIVE sell (CFM CDE)",
                extra={
                    "data": {
                        "product": pid,
                        "strategy": lot.strategy,
                        "qty": fill_qty,
                        "price": fill_px,
                        "fee": fee_usd,
                        "reason": reason,
                        "position_id": lot.id,
                        "realized": realized,
                        "order_id": order.get("id"),
                        "venue": "CFM_CDE",
                    }
                },
            )
        book = pid if pid in self.state.stock_pairs else product
        if book in self.state.stock_pairs:
            self.state.stock_pairs[book].open_count = ledger.open_count(book)
        self._last_budget["open_notional_usd"] = open_notional_usd(
            [lot for lot in ledger.open_positions() if self._lot_is_live_backed(lot)]
        )

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
        log.info(
            "stock live venue CFM CDE (no INTX single-name map)",
            extra={
                "data": {
                    "products": sorted({v for v in self._perp_map.values()}),
                    "live_orders": live,
                }
            },
        )

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
        seed_map = {p: p for p in settings.stock_product_list}
        market: Any = StockMarkRouter(
            yahoo=YahooPaperMarket(),
            coinbase=cb_market,
            perp_map=seed_map,
            prefer_coinbase=True,
        )
        state.stock_mark_source = "coinbase_cfm_cde"
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
                "budget_pct": (
                    float(settings.crypto_account_budget_pct)
                    + float(settings.stock_account_budget_pct)
                    if getattr(settings, "crypto_stock_shared_budget", True)
                    else float(settings.stock_account_budget_pct)
                ),
                "crypto_stock_shared_budget": bool(
                    getattr(settings, "crypto_stock_shared_budget", True)
                ),
                "stock_account_budget_pct": settings.stock_account_budget_pct,
                "cfm_products": list(settings.stock_product_list) if live else [],
                "perp_mapped": len({v for v in engine._perp_map.values()}),
                "venue": "CFM_CDE" if live else "yahoo_paper",
            }
        },
    )
    return engine
