"""Future Trader — session day-trade engine (paper + dual-gated live CFM CDE).

Primary path is America/New_York session longs on SPY/QQQ perps:
  • Budget = futures_account_budget_pct (default 20%) of Coinbase account value
  • 50/50 notional split across products; max 1 open lot per index
  • Entry ~09:25–09:30 ET (late catch-up until exit window if bot was down)
  • Exit ~15:55–16:00 ET only if green (mark >= entry); else hold overnight
  • Overnight losers: no new entry while lot remains open

Legacy sma_1d/donchian_1d paper path remains when session_day is not configured.
"""

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
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
    parse_futures_order_fill,
    round_amount_down,
)
from snowball.futures.session import (
    classify_session_state,
    entry_allowed,
    et_date_str,
    in_exit_window,
    session_times_from_settings,
    to_et,
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

SESSION_STRATEGY = "session_day"


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
    max_notional: float | None = None,
) -> RiskContext:
    live = settings.futures_live_orders_permitted()
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
        max_position_notional_usd=(
            float(max_notional)
            if max_notional is not None
            else float(settings.per_leg_base_usd)
        ),
        entry_cooldown=timedelta(seconds=cooldown_seconds),
        mode="live" if live else "paper",
        live_enabled=True if live else False,
    )


def _refuse_unless_paper_or_dual_live(settings: Settings) -> None:
    """Paper always OK; live only when both futures flags true."""
    if settings.futures_mode == "paper":
        if settings.futures_live_enabled:
            raise LiveTradingRefused(
                "Futures trading refused: FUTURES_LIVE_ENABLED=true while "
                "FUTURES_MODE is not live (dual gate required)."
            )
        return
    if not settings.futures_live_orders_permitted():
        raise LiveTradingRefused(
            f"Futures trading refused: FUTURES_MODE={settings.futures_mode!r} "
            f"FUTURES_LIVE_ENABLED={settings.futures_live_enabled!r} "
            "(both must be true for live futures orders)."
        )


class FuturesEngine:
    """Long-only session day-trade (primary) + optional legacy daily SMA paper lane."""

    def __init__(
        self, state: AppState, market: CoinbaseFuturesMarket | None = None
    ) -> None:
        self.state = state
        self._entry_dates_et: dict[str, str] = {}
        self._last_budget: dict[str, float] = {
            "account_value_usd": 0.0,
            "budget_usd": 0.0,
            "per_index_usd": 0.0,
        }
        if market is not None:
            self.market = market
        else:
            s = state.settings
            allow = s.futures_live_orders_permitted()
            self.market = CoinbaseFuturesMarket(
                api_key=s.coinbase_api_key,
                api_secret=s.coinbase_api_secret,
                api_passphrase=s.coinbase_api_passphrase,
                allow_orders=allow,
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
            _refuse_unless_paper_or_dual_live(settings)
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

        marks: dict[str, float] = {}
        for product in products:
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        # Recompute live account budget each session tick
        self._refresh_budget(marks)

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

        if settings.futures_uses_session_engine():
            for product in products:
                self._act_session(
                    product=product,
                    now=now,
                    halted=halted,
                    can_trade=can_trade,
                    daily_killed=killed,
                    marks=marks,
                )
        else:
            for product in products:
                self._act_on_pair_legacy(
                    product=product,
                    now=now,
                    halted=halted,
                    can_trade=can_trade,
                    daily_killed=killed,
                    marks=marks,
                )

        # Dashboard session state
        states: dict[str, str] = {}
        for product in products:
            lots = ledger.open_positions(product)
            states[product] = classify_session_state(open_lots=lots, now=now)
        self.state.futures_session_states = states
        self.state.futures_account_value_usd = self._last_budget["account_value_usd"]
        self.state.futures_budget_usd = self._last_budget["budget_usd"]
        self.state.futures_per_index_allotment_usd = self._last_budget["per_index_usd"]
        self.state.futures_last_tick_at = now
        self.state.futures_mark_source = getattr(
            self.market, "mark_source", "coinbase_perp"
        )

    def _refresh_budget(self, marks: dict[str, float]) -> None:
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        n = max(1, len(self.product_list()))
        account_value = 0.0
        if settings.futures_live_orders_permitted():
            try:
                crypto_marks = {}
                try:
                    crypto_marks = self.state.marks()
                except Exception:  # noqa: BLE001
                    crypto_marks = {}
                account_value = float(
                    self.market.fetch_account_value_usd(crypto_marks=crypto_marks)
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "futures account value fetch failed; falling back to ledger equity",
                    extra={"data": {"error": str(exc)}},
                )
                account_value = float(ledger.equity_usd(marks))
        else:
            # Paper: use ledger equity (bankroll ± open MTM)
            account_value = float(ledger.equity_usd(marks))
            if account_value <= 0:
                account_value = float(settings.futures_bankroll_usd)

        pct = float(settings.futures_account_budget_pct)
        budget = max(0.0, account_value * pct)
        per_index = budget / float(n)
        # Shared per-leg autoscale ceiling (PER_LEG_*); INTX-era soft cap only.
        per_leg = settings.effective_per_leg_notional_usd(account_value)
        prods = [normalize_futures_product(p) for p in self.product_list()]
        if not (prods and all(is_cfm_product(p) for p in prods)):
            per_index = min(per_index, per_leg)
        self._last_budget = {
            "account_value_usd": account_value,
            "budget_usd": budget,
            "per_index_usd": per_index,
            "per_leg_notional_usd": per_leg,
        }
        # Sync paper cash display toward bankroll when paper; live keeps ledger as fill book
        if not settings.futures_live_orders_permitted():
            # Ensure paper cash can fund allotment (ledger already has bankroll)
            pass

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.futures_pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.futures_max_positions
        )
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("futures ticker failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)

        # Session engine skips SMA/Donchian as primary; still refresh if legacy strategies listed
        wanted = set(settings.futures_strategy_list)
        if wanted - {SESSION_STRATEGY}:
            limit = settings.ohlcv_fetch_limit
            frames = enabled_timeframes(
                [s for s in settings.futures_strategy_list if s != SESSION_STRATEGY]
            )
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
        elif SESSION_STRATEGY in wanted:
            # Session: surface state in signal field for dashboard
            ledger = self.state.futures_ledger
            assert ledger is not None
            lots = ledger.open_positions(product)
            snap.signal = classify_session_state(open_lots=lots, now=utcnow())
            snap.signal_1d = snap.signal

        ledger = self.state.futures_ledger
        assert ledger is not None
        snap.open_count = ledger.open_count(product)
        snap.max_open = settings.futures_max_positions
        self.state.futures_pairs[product] = snap
        return snap

    # --- Session day-trade path -------------------------------------------------

    def _act_session(
        self,
        product: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        times = session_times_from_settings(settings)
        lots = ledger.open_positions(product)
        mark = marks.get(product)
        snap = self.state.futures_pairs[product]
        if mark is None and snap.last is not None:
            mark = snap.last

        # Exit window first: close green only
        if lots and in_exit_window(
            now, start=times["exit_start"], end=times["exit_end"]
        ):
            green_lots: list[Position] = []
            for lot in lots:
                if mark is None or mark < lot.entry_price:
                    log.info(
                        "futures session hold overnight (red/flat)",
                        extra={
                            "data": {
                                "product": product,
                                "position_id": lot.id,
                                "mark": mark,
                                "entry": lot.entry_price,
                            }
                        },
                    )
                    continue
                # Session floor: mark >= entry. Honor never_sell_red (already green).
                # Do NOT require min_take_profit_pct here — day-trade would never exit.
                if settings.never_sell_red and mark < lot.entry_price:
                    continue
                green_lots.append(lot)
            if green_lots:
                ctx = _futures_risk_ctx(
                    settings,
                    now=now,
                    halted=halted,
                    can_trade=can_trade,
                    daily_killed=daily_killed,
                    open_count_for_pair=len(lots),
                    last_entry_at=ledger.last_entry_at(product, SESSION_STRATEGY),
                    cash_usd=ledger.cash_usd(),
                    requested_notional=self._last_budget["per_index_usd"],
                    cooldown_seconds=0,
                    max_notional=max(
                        self._last_budget["per_index_usd"],
                        float(
                            self._last_budget.get("per_leg_notional_usd")
                            or settings.effective_per_leg_notional_usd(
                                float(self._last_budget.get("account_value_usd") or 0.0)
                            )
                        ),
                    ),
                )
                ok, reason = allow_exit(ctx)
                if ok:
                    self._close_lots(
                        product,
                        green_lots,
                        marks,
                        reason=f"{SESSION_STRATEGY}:session_close",
                        now=now,
                    )
                else:
                    log.info(
                        "futures session exit blocked",
                        extra={"data": {"product": product, "reason": reason}},
                    )
            return

        # Entry: only when flat (max 1 lot); overnight open lot blocks new entry
        if lots:
            return
        if not entry_allowed(
            now, entry_start=times["entry_start"], exit_start=times["exit_start"]
        ):
            return
        today_et = et_date_str(now)
        if self._entry_dates_et.get(product) == today_et:
            return  # already entered (or attempted) this ET day

        per_index = float(self._last_budget["per_index_usd"])
        # Remaining lane budget after open exposure. CFM consumes margin estimate,
        # not full contract notional (~$3k), so a second index can still open.
        from snowball.sizing import cfm_required_margin_usd

        margin_rate = float(getattr(settings, "cfm_margin_rate", 0.10))
        lev = settings.cfm_order_leverage()
        open_used = 0.0
        for prod in self.product_list():
            for p in ledger.open_positions(prod):
                if is_cfm_product(prod):
                    qty = max(1, int(round(float(p.qty))))
                    open_used += cfm_required_margin_usd(
                        float(p.entry_price),
                        contracts=qty,
                        leverage=lev,
                        margin_rate=margin_rate,
                    )
                else:
                    open_used += float(p.notional_usd)
        budget_left = max(0.0, float(self._last_budget["budget_usd"]) - open_used)
        per_leg = float(
            self._last_budget.get("per_leg_notional_usd")
            or settings.effective_per_leg_notional_usd(
                float(self._last_budget.get("account_value_usd") or 0.0)
            )
        )
        if is_cfm_product(product):
            # Lane budget remaining is the affordability budget for 1 CFM contract.
            notional = min(per_index, budget_left)
        else:
            notional = min(per_index, budget_left, per_leg)
        if notional <= 1.0:
            log.info(
                "futures session entry skipped small notional",
                extra={"data": {"product": product, "notional": notional}},
            )
            return

        ctx = _futures_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=0,
            last_entry_at=None,
            cash_usd=max(ledger.cash_usd(), notional),
            requested_notional=notional,
            cooldown_seconds=0,
            max_notional=max(notional, per_leg),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            log.info(
                "futures session entry blocked",
                extra={"data": {"product": product, "reason": reason}},
            )
            return
        self._entry_dates_et[product] = today_et
        self._open_lot(
            product,
            marks,
            reason=f"{SESSION_STRATEGY}:session_enter",
            now=now,
            strategy=SESSION_STRATEGY,
            notional_usd=notional,
        )

    # --- Legacy SMA/Donchian paper path ----------------------------------------

    def _act_on_pair_legacy(
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
            if strategy_id == SESSION_STRATEGY:
                continue
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
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=getattr(settings, "fee_buffer_pct", 0.0),
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
                    min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=getattr(settings, "fee_buffer_pct", 0.0),
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

        allot = float(
            self._last_budget.get("per_index_usd")
            or self._last_budget.get("per_leg_notional_usd")
            or settings.effective_per_leg_notional_usd(
                float(self._last_budget.get("account_value_usd") or 0.0)
            )
        )
        ctx = _futures_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(all_lots),
            last_entry_at=ledger.last_entry_at(product, strategy_id),
            cash_usd=ledger.cash_usd(),
            requested_notional=allot,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            max_notional=allot,
        )
        ok, reason = allow_entry(ctx)
        if not ok:
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
            notional_usd=allot,
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
        _refuse_unless_paper_or_dual_live(settings)
        ledger = self.state.futures_ledger
        assert ledger is not None
        snap = self.state.futures_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        target = float(
            notional_usd
            if notional_usd is not None
            else (
                self._last_budget.get("per_leg_notional_usd")
                or settings.per_leg_base_usd
            )
        )
        if settings.futures_live_orders_permitted():
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
        if is_cfm_product(product):
            contracts = order_size_for_product(
                product,
                notional_usd=target,
                price=float(px),
                available_margin_usd=max(float(ledger.cash_usd()), float(target)),
                max_contracts=settings.cfm_max_contracts_per_index(),
                leverage=settings.cfm_order_leverage(),
                margin_rate=float(settings.cfm_margin_rate),
            )
            if contracts < 1:
                log.info(
                    "futures paper buy skipped: cannot fund 1 CFM contract",
                    extra={"data": {"product": product, "target": target, "px": px}},
                )
                return
            notional = float(contracts) * float(px)
        else:
            notional = min(target, ledger.cash_usd())
            if notional <= 0:
                return
        # Top-up paper cash if budget-based notional exceeds cash (session paper)
        if ledger.cash_usd() + 1e-9 < notional:
            try:
                ledger.set_cash_usd(max(ledger.cash_usd(), notional * 2), ts=now)
            except Exception:  # noqa: BLE001
                log.exception("futures paper cash top-up failed")
            if not is_cfm_product(product):
                notional = min(target, ledger.cash_usd())
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
                    "et": to_et(now).isoformat(),
                }
            },
        )

    def _open_lot_live(
        self,
        product: str,
        ticker: Ticker,
        reason: str,
        now: datetime,
        strategy: str,
        notional_usd: float,
    ) -> None:
        settings = self.state.settings
        if not settings.futures_live_orders_permitted():
            raise LiveTradingRefused("futures live open refused: dual gate not set")
        ledger = self.state.futures_ledger
        assert ledger is not None
        ref = ticker.reference or ticker.last
        if ref is None or ref <= 0:
            log.warning(
                "futures live buy skipped: no mark",
                extra={"data": {"product": product}},
            )
            return
        lev = settings.cfm_order_leverage() if is_cfm_product(product) else 1.0
        max_c = settings.cfm_max_contracts_per_index()
        margin_rate = float(getattr(settings, "cfm_margin_rate", 0.10))
        avail = max(float(ledger.cash_usd()), float(notional_usd))
        amount = order_size_for_product(
            product,
            notional_usd=notional_usd,
            price=float(ref),
            available_margin_usd=avail,
            max_contracts=max_c,
            leverage=lev,
            margin_rate=margin_rate,
        )
        min_amt = 1.0 if is_cfm_product(product) else 0.01
        if amount < min_amt:
            log.info(
                "futures live buy skipped: amount below min",
                extra={
                    "data": {
                        "product": product,
                        "notional": notional_usd,
                        "ref": ref,
                        "amount": amount,
                        "cfm": is_cfm_product(product),
                    }
                },
            )
            return
        bid = ticker.bid
        ask = ticker.ask
        try:
            from snowball.maker import maker_buy_price

            limit_px = maker_buy_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                bid2, ask2 = self.market.fetch_bba(product)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_buy_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "futures live buy skipped: no book bid for maker",
                    extra={"data": {"product": product, "bid": bid, "ask": ask}},
                )
                return
            amount = order_size_for_product(
                product,
                notional_usd=notional_usd,
                price=float(limit_px),
                available_margin_usd=avail,
                max_contracts=max_c,
                leverage=lev,
                margin_rate=margin_rate,
            )
            if amount < min_amt:
                log.info(
                    "futures live buy skipped: amount below min",
                    extra={"data": {"product": product, "limit_px": limit_px}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
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
                "futures live buy failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "futures live buy unfilled/canceled maker limit",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        # Ensure ledger can record (futures book is separate; top-up cash for accounting)
        cost = fill_qty * fill_px + fee_usd
        if ledger.cash_usd() + 1e-9 < cost:
            try:
                ledger.set_cash_usd(cost * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("futures live ledger cash sync failed")
        pos, fill = ledger.open_buy(
            product=product,
            fill_px=fill_px,
            notional_usd=fill_qty * fill_px,
            slippage_bps=0.0,
            fee_usd=fee_usd,
            reason=reason,
            ts=now,
            strategy=strategy,
        )
        self.state.futures_pairs[product].open_count = ledger.open_count(product)
        log.warning(
            "futures LIVE buy",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "qty": fill_qty,
                    "price": fill_px,
                    "notional": fill.notional_usd,
                    "fee": fee_usd,
                    "reason": reason,
                    "position_id": pos.id,
                    "order_id": order.get("id"),
                    "et": to_et(now).isoformat(),
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
        _refuse_unless_paper_or_dual_live(settings)
        ledger = self.state.futures_ledger
        assert ledger is not None
        snap = self.state.futures_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        if settings.futures_live_orders_permitted():
            self._close_lots_live(product, lots, ticker, reason=reason, now=now)
            return
        paper_px = fill_price(ticker, "sell", settings.slippage_bps)
        for lot in lots:
            if not is_emergency_flatten_reason(reason):
                # Session closes already gated to green; legacy uses strategy_exit_allowed
                if SESSION_STRATEGY not in reason:
                    ok_sw, reason_sw = strategy_exit_allowed(
                        lot,
                        paper_px,
                        min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                        never_sell_red=settings.never_sell_red,
                    fee_buffer_pct=getattr(settings, "fee_buffer_pct", 0.0),
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
                elif settings.never_sell_red and paper_px < lot.entry_price:
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

    def _close_lots_live(
        self,
        product: str,
        lots: list[Position],
        ticker: Ticker,
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        for lot in lots:
            if not is_emergency_flatten_reason(reason):
                ref = ticker.reference or ticker.last
                if settings.never_sell_red and ref is not None and ref < lot.entry_price:
                    log.info(
                        "futures live close skipped never_sell_red",
                        extra={"data": {"product": product, "position_id": lot.id}},
                    )
                    continue
            if is_cfm_product(product):
                amount = float(max(1, int(round(float(lot.qty)))))
            else:
                amount = round_amount_down(lot.qty, 0.01)
                if amount < 0.01:
                    continue
            try:
                from snowball.maker import (
                    URGENT_MAKER_TIMEOUT_SEC,
                    maker_sell_price,
                    session_close_urgent,
                )
                from snowball.futures.session import session_times_from_settings

                emergency = is_emergency_flatten_reason(reason)
                times = session_times_from_settings(settings)
                urgent = (not emergency) and (
                    ":session_close" in reason
                    and session_close_urgent(
                        now,
                        exit_start=times["exit_start"],
                        exit_end=times["exit_end"],
                    )
                )
                if emergency:
                    order = self.market.create_swap_market_order(
                        product, "sell", amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
                    )
                else:
                    bid, ask = ticker.bid, ticker.ask
                    sell_px = maker_sell_price(bid, ask, ticker.last)
                    if sell_px is None:
                        bid2, ask2 = self.market.fetch_bba(product)
                        bid = bid if bid is not None else bid2
                        ask = ask if ask is not None else ask2
                        sell_px = maker_sell_price(bid, ask, ticker.last)
                    if sell_px is None:
                        if urgent:
                            # Session would miss close — careful market fallback.
                            log.warning(
                                "futures session close maker skipped; market fallback",
                                extra={"data": {"product": product, "position_id": lot.id}},
                            )
                            order = self.market.create_swap_market_order(
                                product, "sell", amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
                            )
                        else:
                            log.info(
                                "futures live sell skipped: no ask for maker",
                                extra={
                                    "data": {
                                        "product": product,
                                        "position_id": lot.id,
                                    }
                                },
                            )
                            continue
                    else:
                        timeout = (
                            URGENT_MAKER_TIMEOUT_SEC
                            if urgent
                            else float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
                        )
                        order = self.market.create_swap_maker_limit_order(
                            product,
                            "sell",
                            amount,
                            price=sell_px,
                            bid=bid,
                            ask=ask,
                            leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0),
                            reduce_only=True,
                            timeout_sec=timeout,
                        )
                        fill_px_chk, fill_qty_chk, _ = parse_futures_order_fill(order)
                        if (fill_px_chk <= 0 or fill_qty_chk <= 0) and urgent:
                            # One cancel/replace already done inside settle; market fallback.
                            log.warning(
                                "futures session close maker unfilled; market fallback",
                                extra={
                                    "data": {
                                        "product": product,
                                        "position_id": lot.id,
                                        "order_id": order.get("id"),
                                    }
                                },
                            )
                            order = self.market.create_swap_market_order(
                                product, "sell", amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
                            )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "futures live sell failed",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "error": str(exc),
                        }
                    },
                )
                continue
            fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
            if fill_px <= 0 or fill_qty <= 0:
                log.error(
                    "futures live sell unfilled/canceled maker limit",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "order_id": order.get("id"),
                        }
                    },
                )
                continue
            fill = ledger.close_position(
                position_id=lot.id,
                fill_px=fill_px,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
            )
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
            log.warning(
                "futures LIVE sell",
                extra={
                    "data": {
                        "product": product,
                        "strategy": lot.strategy,
                        "qty": fill.qty,
                        "price": fill.price,
                        "reason": reason,
                        "position_id": lot.id,
                        "realized": realized,
                        "order_id": order.get("id"),
                        "et": to_et(now).isoformat(),
                    }
                },
            )
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
            "Future Trader loop start",
            extra={
                "data": {
                    "poll_seconds": interval,
                    "products": self.product_list(),
                    "strategies": self.state.settings.futures_strategy_list,
                    "futures_mode": self.state.settings.futures_mode,
                    "futures_live_enabled": self.state.settings.futures_live_enabled,
                    "session_engine": self.state.settings.futures_uses_session_engine(),
                    "budget_pct": self.state.settings.futures_account_budget_pct,
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


# Back-compat name used by tests / imports
FuturesPaperEngine = FuturesEngine


def attach_futures_lane(state: AppState) -> FuturesEngine | None:
    """Create isolated futures ledger + engine if FUTURES_ENABLED."""
    settings = state.settings
    if not settings.futures_enabled:
        log.info("Future Trader lane disabled")
        return None
    settings.assert_futures_config()
    ledger = PaperLedger(settings.futures_sqlite_path, settings.futures_bankroll_usd)
    state.futures_ledger = ledger
    state.futures_mark_source = "coinbase_perp"
    state.futures_session_states = {}
    state.futures_account_value_usd = None
    state.futures_budget_usd = None
    state.futures_per_index_allotment_usd = None
    for product in settings.futures_product_list or list(DEFAULT_FUTURES_PRODUCTS):
        pid = normalize_futures_product(product)
        state.futures_pairs[pid] = PairSnapshot(
            product=pid, max_open=settings.futures_max_positions
        )
    engine = FuturesEngine(state)
    state.futures_engine = engine
    live = settings.futures_live_orders_permitted()
    log.warning(
        "Future Trader lane attached",
        extra={
            "data": {
                "sqlite": str(settings.futures_sqlite_path),
                "bankroll": settings.futures_bankroll_usd,
                "products": settings.futures_product_list,
                "strategies": settings.futures_strategy_list,
                "mark_source": "coinbase_perp",
                "futures_mode": settings.futures_mode,
                "futures_live_enabled": settings.futures_live_enabled,
                "live_orders": live,
                "budget_pct": settings.futures_account_budget_pct,
                "session_engine": settings.futures_uses_session_engine(),
            }
        },
    )
    return engine
