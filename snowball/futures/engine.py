"""Future Trader — session overnight roll + intraday momentum (paper + dual-gated live CFM CDE).

America/New_York longs on CFM US5/TEK:
  • Budget = futures_account_budget_pct (default 30%) of Coinbase account value
  • 50/50 notional split across products; max 1 open lot per index (no double-long)
  • session_day: enter preferred open window; exit ~15:55–16:00 ET only if green
    else hold overnight; skip new session entry while overnight open
  • momentum_15m: short-horizon breakout during US cash hours; exit on TP / stall
    / EOD if green enough; never_sell_red; shares FT budget with session_day
  • Prefer one momentum OR one session lot per product

Legacy sma_1d/donchian_1d paper path remains when neither session/momentum configured.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from snowball.config import LiveTradingRefused, Settings
from snowball.gates import (
    is_emergency_flatten_reason,
    is_stall_exit_reason,
    lot_unrealized_pnl_pct,
    momentum_fading,
    scale_in_allowed,
    sma_fast_for_strategy,
    sma_slow_for_strategy,
    strategy_exit_allowed,
    trend_filter_allows,
)
from snowball import signal_context
from snowball.halt import halt_active, trading_enabled
from snowball.market import fill_price
from snowball.models import PairSnapshot, Position, Signal, Ticker, utcnow
from snowball.paper import PaperLedger
from snowball.risk import RiskContext, allow_entry, allow_exit, daily_loss_breached
from snowball.state import AppState
from snowball.futures.ledger import FuturesLedger
from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    CoinbaseFuturesMarket,
    contract_size_for_product,
    contracts_from_qty,
    expiry_for_product,
    is_cfm_product,
    margin_rate_for_product,
    normalize_futures_product,
    order_size_for_product,
    parse_futures_order_fill,
    round_amount_down,
)
from snowball.futures.session import (
    classify_session_state,
    entry_allowed,
    entry_allowed_for_settings,
    et_date_str,
    in_exit_window,
    in_us_cash_session,
    session_times_from_settings,
    to_et,
)
from snowball.futures.momentum import (
    MOMENTUM_15M,
    momentum_breakdown,
    momentum_breakout,
    parse_ohlcv_ohlc,
    price_stalled_short,
    side_pnl_pct,
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
MOMENTUM_STRATEGY = MOMENTUM_15M
FT_PRIMARY_STRATEGIES = frozenset({SESSION_STRATEGY, MOMENTUM_STRATEGY})


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
        self._net_green_log_at: dict[tuple[int, str], float] = {}
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
        from snowball.futures.roll import LaneRollSupport

        self._roll = LaneRollSupport(
            "futures",
            state,
            lambda: self.state.futures_ledger,
            self.market,
            lambda: bool(self.state.settings.futures_live_orders_permitted()),
        )
        # Task 10: former STOCK-lane strategies run inside this lane (one net
        # position per product family across all FT strategies).
        from snowball.futures.stock_strategies import MergedStockStrategies

        self._stock = MergedStockStrategies(self)

    def product_list(self) -> list[str]:
        """Configured products resolved to the front tradable month (+ open-lot months)."""
        settings = self.state.settings
        items = [
            normalize_futures_product(p)
            for p in settings.futures_product_list
        ]
        items = items or list(DEFAULT_FUTURES_PRODUCTS)
        try:
            return self._roll.products(items)
        except Exception:  # noqa: BLE001 — never break the lane on catalog trouble
            log.exception("futures front-month resolution failed; using configured products")
            return items

    # --- 2026-09-30 multi-product helpers (BTC nano perp, crude) -------------

    def _session_products(self) -> set[str]:
        """Products that run session_day (index longs). Default US5/TEK."""
        from snowball.futures.roll import same_family_member

        lst = [
            normalize_futures_product(p)
            for p in getattr(self.state.settings, "futures_session_product_list", [])
        ]
        prods = self.product_list()
        if not lst:
            return set(prods)
        # family match so a rolled month keeps its role (US5/TEK session only)
        return {p for p in prods if same_family_member(p, lst)} | set(lst)

    def _session_priority_order(self, products: list[str], session_products: set[str]) -> list[str]:
        """All session products in this tick's loop: SESSION_PRODUCT_PRIORITY families first (in
        that order), then any other session products in their usual order. Unknown families ignored."""
        from snowball.futures.roll import family_of

        prio = list(getattr(self.state.settings, "session_product_priority_list", []) or [])
        sess = [p for p in products if p in session_products]
        out: list[str] = []
        for fam in prio:
            out.extend(p for p in sess if family_of(p) == fam and p not in out)
        out.extend(p for p in sess if p not in out)
        return out

    def _short_allowed(self, product: str) -> bool:
        from snowball.futures.roll import same_family_member

        lst = getattr(self.state.settings, "futures_short_product_list", []) or []
        # family match: FUTURES_SHORT_PRODUCTS=NOL-19OCT26-CDE also covers the
        # auto-rolled NOL months.
        return same_family_member(normalize_futures_product(product), list(lst))

    def _expiry_blocks_entry(self, product: str, now: datetime) -> bool:
        exp = expiry_for_product(product)
        if exp is None:
            return False
        days = float(getattr(self.state.settings, "futures_expiry_entry_block_days", 3.0) or 0.0)
        return (exp - now).total_seconds() <= days * 86400.0

    def _one_contract_margin_usd(self, product: str, price: float, side: str) -> float:
        from snowball.sizing import cfm_required_margin_usd

        settings = self.state.settings
        return cfm_required_margin_usd(
            float(price),
            contracts=1,
            leverage=settings.cfm_order_leverage(),
            contract_size=contract_size_for_product(product),
            margin_rate=margin_rate_for_product(
                product, side, float(getattr(settings, "cfm_margin_rate", 0.10))
            ),
        )

    def _budget_left_usd(self, ledger: PaperLedger) -> float:
        from snowball.sizing import cfm_required_margin_usd

        settings = self.state.settings
        base_rate = float(getattr(settings, "cfm_margin_rate", 0.10))
        lev = settings.cfm_order_leverage()
        open_used = 0.0
        for prod in self.product_list():
            for pos in ledger.open_positions(prod):
                if is_cfm_product(prod):
                    open_used += cfm_required_margin_usd(
                        float(pos.entry_price),
                        contracts=contracts_from_qty(prod, float(pos.qty)),
                        leverage=lev,
                        contract_size=contract_size_for_product(prod),
                        margin_rate=margin_rate_for_product(prod, pos.side, base_rate),
                    )
                else:
                    open_used += float(pos.notional_usd)
        return max(0.0, float(self._last_budget["budget_usd"]) - open_used)

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

        # Contract roll (explicit path; the ONLY exit allowed red — reason contract_roll)
        try:
            self._roll.tick(now, halted=halted, can_trade=can_trade)
        except Exception:  # noqa: BLE001
            log.exception("futures contract roll tick failed")

        # Futures stop-loss + liquidation guard (Tb 2026-10-01). Every poll, BEFORE strategy
        # dispatch, also while HALT / daily-kill are active. Netted pairs excluded.
        try:
            self._stop_tick(now=now, can_trade=can_trade)
        except Exception:  # noqa: BLE001
            log.exception("futures stop tick failed")

        uses_session = settings.futures_uses_session_engine()
        uses_mom = settings.futures_uses_momentum()
        if uses_session or uses_mom:
            session_products = self._session_products()
            # session_day product priority (Tb 10/2, SESSION_PRODUCT_PRIORITY=TEK,US5): at the first
            # session slot, run session_day for every session product in priority order, so TEK is
            # tried before US5 (and before US5 momentum) when the one-index gate leaves one slot.
            # Order only; every gate / stop / net-green rule / size is unchanged.
            session_order = self._session_priority_order(products, session_products)
            session_done = False
            for product in products:
                if uses_session and product in session_products and not session_done:
                    session_done = True
                    for sp in session_order:
                        self._act_session(
                            product=sp,
                            now=now,
                            halted=halted,
                            can_trade=can_trade,
                            daily_killed=killed,
                            marks=marks,
                        )
                if uses_mom:
                    self._act_momentum(
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

        # Former STOCK-lane strategies (US5/TEK families), after session/momentum.
        try:
            self._stock.tick(now=now, halted=halted, can_trade=can_trade,
                             daily_killed=killed, marks=marks)
        except Exception:  # noqa: BLE001
            log.exception("futures merged stock strategies tick failed")

        # Task 16: research_idea (autonomous high-confidence proposals), own lots only.
        from snowball.engine import research_idea_active

        if research_idea_active(settings, self.state):
            for product in products:
                try:
                    self._act_research_idea(product=product, now=now, halted=halted, can_trade=can_trade,
                                            daily_killed=killed, marks=marks)
                except Exception:  # noqa: BLE001
                    log.exception("research_idea futures step failed", extra={"data": {"product": product}})

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
        # Fetch OHLCV for legacy strategies and/or momentum_15m (session is marks-only).
        ohlcv_strats = [s for s in settings.futures_strategy_list if s != SESSION_STRATEGY]
        if ohlcv_strats:
            limit = settings.ohlcv_fetch_limit
            frames = enabled_timeframes(ohlcv_strats)
            # Ensure configured momentum timeframe is fetched even if alias differs.
            mom_tf = str(getattr(settings, "futures_momentum_timeframe", "15m") or "15m")
            if MOMENTUM_STRATEGY in wanted and mom_tf not in frames:
                frames.append(mom_tf)
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
        elif wanted & FT_PRIMARY_STRATEGIES:
            # Session / momentum: surface session state for dashboard
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

        # Exit window first: close green session_day lots only (momentum has its own path).
        # Red/flat → hold overnight; clarify in logs. Skip new entry while any lot open.
        session_lots = [lot for lot in lots if lot.strategy == SESSION_STRATEGY]
        if session_lots and in_exit_window(
            now, start=times["exit_start"], end=times["exit_end"]
        ):
            green_lots: list[Position] = []
            for lot in session_lots:
                if mark is None or mark < lot.gate_entry_price:
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
                if settings.never_sell_red and mark < lot.gate_entry_price:
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
                    signal_context.stage(
                        product, f"{SESSION_STRATEGY}:session_close", lane="futures",
                        strategy=SESSION_STRATEGY, action="exit",
                        side=("buy" if any(l.side == "short" for l in green_lots) else "sell"),
                        rule="session exit window and lot green (mark >= entry)", price=mark,
                        now=now, timeframe="session",
                        indicators={"exit_window_et": f"{times['exit_start']}-{times['exit_end']}"},
                        floor={"mark_ge_entry": True},
                        gates={"never_sell_red": bool(settings.never_sell_red), "allow_exit": "passed",
                               "lots": [{"id": l.id, "entry": l.gate_entry_price} for l in green_lots]},
                    )
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
        if not entry_allowed_for_settings(now, settings):
            return
        today_et = et_date_str(now)
        if self._entry_dates_et.get(product) == today_et:
            return  # already entered (or attempted) this ET day

        notional = self._session_entry_notional(product, ledger)
        per_leg = float(
            self._last_budget.get("per_leg_notional_usd")
            or settings.effective_per_leg_notional_usd(
                float(self._last_budget.get("account_value_usd") or 0.0)
            )
        )
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
            max_notional=max(
                notional,
                per_leg,
                float(settings.futures_max_notional_usd),
            ),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            log.info(
                "futures session entry blocked",
                extra={"data": {"product": product, "reason": reason}},
            )
            return
        self._entry_dates_et[product] = today_et
        signal_context.stage(
            product, f"{SESSION_STRATEGY}:session_enter", lane="futures", strategy=SESSION_STRATEGY,
            action="entry", side="long", rule="session day-trade: flat, in ET entry window, once per ET day",
            price=marks.get(product), snap=self.state.futures_pairs.get(product), now=now, timeframe="session",
            indicators={"entry_window_et": f"{times['entry_start']}-{times['entry_end']}", "et_date": today_et},
            target={"exit_window_et": f"{times['exit_start']}-{times['exit_end']}", "rule": "exit green in window"},
            gates={"flat": True, "allow_entry": "passed", "notional_usd": notional, "halted": bool(halted)},
        )
        self._open_lot(
            product,
            marks,
            reason=f"{SESSION_STRATEGY}:session_enter",
            now=now,
            strategy=SESSION_STRATEGY,
            notional_usd=notional,
        )


    # --- Intraday momentum path (momentum_15m) ---------------------------------

    def _act_momentum(
        self,
        product: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        """Short-horizon breakout during US cash hours; shares FT budget with session."""
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        times = session_times_from_settings(settings)
        lots = ledger.open_positions(product)
        mom_lots = [lot for lot in lots if lot.strategy == MOMENTUM_STRATEGY]
        mark = marks.get(product)
        snap = self.state.futures_pairs[product]
        if mark is None and snap.last is not None:
            mark = snap.last

        # Manage open momentum lots: TP / stall / EOD green exit
        if mom_lots:
            self._momentum_maybe_exit(
                product=product,
                mom_lots=mom_lots,
                mark=mark,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=daily_killed,
                marks=marks,
                times=times,
            )
            return

        # Entry: flat only (any strategy blocks — no double-long same index)
        if lots:
            return
        if not in_us_cash_session(now):
            return
        # Prefer session in its preferred open window; momentum waits until after.
        if settings.futures_uses_session_engine():
            from snowball.futures.session import in_entry_preferred_window

            if in_entry_preferred_window(
                now, start=times["entry_start"], end=times["entry_end"]
            ):
                return
        if in_exit_window(now, start=times["exit_start"], end=times["exit_end"]):
            return
        if self._expiry_blocks_entry(product, now):
            log.debug(
                "futures momentum entry blocked: near contract expiry",
                extra={"data": {"product": product}},
            )
            return

        tf = str(getattr(settings, "futures_momentum_timeframe", "15m") or "15m")
        lookback = int(getattr(settings, "futures_momentum_lookback_bars", 8) or 8)
        min_pct = float(getattr(settings, "futures_momentum_min_pct", 0.003) or 0.0)
        try:
            limit = max(int(settings.ohlcv_fetch_limit), lookback + 5)
            rows = self.market.fetch_ohlcv(product, tf, limit)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "futures momentum ohlcv failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        opens, highs, lows, closes = parse_ohlcv_ohlc(rows)
        side = "long"
        if not momentum_breakout(
            highs, lows, closes, opens, lookback=lookback, min_momentum_pct=min_pct
        ):
            if not (
                self._short_allowed(product)
                and momentum_breakdown(
                    highs, lows, closes, opens, lookback=lookback, min_momentum_pct=min_pct
                )
            ):
                return
            side = "short"

        # Added (non-session) products: one contract's exchange-rate margin must
        # fit what is left of the FT budget (US5/TEK path unchanged).
        if product not in self._session_products() and is_cfm_product(product):
            px_ref = mark if mark is not None else (closes[-1] if closes else None)
            if px_ref is None or px_ref <= 0:
                return
            req1 = self._one_contract_margin_usd(product, float(px_ref), side)
            left = self._budget_left_usd(ledger)
            if req1 > left:
                log.info(
                    "futures momentum entry blocked: 1-contract margin exceeds FT budget left",
                    extra={"data": {"product": product, "side": side,
                                    "required_margin_usd": req1, "budget_left_usd": left}},
                )
                return

        notional = self._session_entry_notional(product, ledger)
        if notional <= 1.0:
            log.info(
                "futures momentum entry skipped small notional",
                extra={"data": {"product": product, "notional": notional}},
            )
            return
        per_leg = float(
            self._last_budget.get("per_leg_notional_usd")
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
            open_count_for_pair=0,
            last_entry_at=ledger.last_entry_at(product, MOMENTUM_STRATEGY),
            cash_usd=max(ledger.cash_usd(), notional),
            requested_notional=notional,
            cooldown_seconds=0,
            max_notional=max(
                notional,
                per_leg,
                float(settings.futures_max_notional_usd),
            ),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            log.info(
                "futures momentum entry blocked",
                extra={"data": {"product": product, "reason": reason}},
            )
            return
        from snowball.futures.momentum import momentum_values

        _mreason = (
            f"{MOMENTUM_STRATEGY}:breakout_enter"
            if side == "long"
            else f"{MOMENTUM_STRATEGY}:breakdown_short_enter"
        )
        signal_context.stage(
            product, _mreason, lane="futures", strategy=MOMENTUM_STRATEGY, action="entry", side=side,
            rule=("close > prior-window high by min % on green bar, above lookback SMA" if side == "long"
                  else "close < prior-window low by min % on red bar, at/below lookback SMA"),
            price=mark, now=now, timeframe=tf,
            candle_ts=(datetime.fromtimestamp(float(rows[-1][0]) / 1000.0, tz=timezone.utc) if rows else None),
            indicators=momentum_values(highs, lows, closes, opens, lookback=lookback, min_momentum_pct=min_pct),
            target={"take_profit_pct": float(getattr(settings, "futures_momentum_take_profit_pct", 0.008) or 0.0),
                    "stall_exit_pct": float(getattr(settings, "futures_momentum_stall_exit_pct", 0.004) or 0.0)},
            gates={"flat": True, "cash_session": True, "expiry_window": "clear", "allow_entry": "passed",
                   "notional_usd": notional, "short_allowed": (self._short_allowed(product) if side == "short" else None),
                   "halted": bool(halted)},
        )
        self._open_lot(
            product,
            marks,
            reason=_mreason,
            now=now,
            strategy=MOMENTUM_STRATEGY,
            notional_usd=notional,
            side=side,
        )

    def _momentum_maybe_exit(
        self,
        *,
        product: str,
        mom_lots: list[Position],
        mark: float | None,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
        times: dict,
    ) -> None:
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        from snowball.gates import lot_unrealized_pnl_pct, price_stalled, stall_exit_allowed

        to_close: list[Position] = []
        exit_reason = f"{MOMENTUM_STRATEGY}:exit"

        in_eod = in_exit_window(
            now, start=times["exit_start"], end=times["exit_end"]
        )
        tp = float(getattr(settings, "futures_momentum_take_profit_pct", 0.008) or 0.0)
        stall_on = bool(getattr(settings, "futures_momentum_stall_exit_enabled", True))
        stall_pct = float(
            getattr(settings, "futures_momentum_stall_exit_pct", 0.004) or 0.0
        )
        stall_lb = int(
            getattr(settings, "futures_momentum_stall_lookback_bars", 4) or 4
        )

        stalled = False
        stalled_short = False
        if stall_on and mark is not None:
            try:
                tf = str(getattr(settings, "futures_momentum_timeframe", "15m") or "15m")
                limit = max(int(settings.ohlcv_fetch_limit), stall_lb + 5)
                rows = self.market.fetch_ohlcv(product, tf, limit)
                _o, highs, lows, closes = parse_ohlcv_ohlc(rows)
                stalled = price_stalled(
                    highs, lows, closes, mark, lookback=stall_lb
                )
                if any(lot.side == "short" for lot in mom_lots):
                    stalled_short = price_stalled_short(
                        highs, lows, closes, mark, lookback=stall_lb
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "futures momentum stall check failed: %s",
                    exc,
                    extra={"data": {"product": product}},
                )

        for lot in mom_lots:
            if mark is None:
                continue
            if lot.side == "short":
                # Short momentum lot (crude): mirror of the long path.
                # never-cover-red: hold while mark > entry (HALT/daily-kill too since 2026-10-01 net-green).
                spnl = side_pnl_pct("short", lot.gate_entry_price, mark)
                if spnl is None:
                    continue
                if settings.never_sell_red and spnl < 0:
                    continue
                if tp > 0 and spnl >= tp:
                    to_close.append(lot)
                    exit_reason = f"{MOMENTUM_STRATEGY}:take_profit"
                    continue
                if stalled_short:
                    if spnl >= max(0.0, stall_pct):
                        to_close.append(lot)
                        exit_reason = f"{MOMENTUM_STRATEGY}:stall_exit"
                    else:
                        log.info(
                            "futures momentum stall hold",
                            extra={"data": {"product": product, "position_id": lot.id,
                                            "side": "short",
                                            "reason": "below_stall_take_profit"}},
                        )
                    continue
                if in_eod and spnl >= 0:
                    to_close.append(lot)
                    exit_reason = f"{MOMENTUM_STRATEGY}:eod_green"
                    continue
                if in_eod:
                    log.info(
                        "futures momentum hold overnight (red/flat at close)",
                        extra={"data": {"product": product, "position_id": lot.id,
                                        "side": "short", "mark": mark,
                                        "entry": lot.entry_price}},
                    )
                continue
            pnl = lot_unrealized_pnl_pct(lot, mark)
            if pnl is None:
                continue
            if settings.never_sell_red and mark < lot.gate_entry_price:
                continue
            if tp > 0 and pnl >= tp:
                to_close.append(lot)
                exit_reason = f"{MOMENTUM_STRATEGY}:take_profit"
                continue
            if stalled:
                ok_st, reason_st = stall_exit_allowed(
                    lot,
                    mark,
                    stall_exit_pct=stall_pct,
                    never_sell_red=settings.never_sell_red,
                )
                if ok_st:
                    to_close.append(lot)
                    exit_reason = f"{MOMENTUM_STRATEGY}:stall_exit"
                else:
                    log.info(
                        "futures momentum stall hold",
                        extra={
                            "data": {
                                "product": product,
                                "position_id": lot.id,
                                "reason": reason_st,
                            }
                        },
                    )
                continue
            if in_eod and mark >= lot.gate_entry_price:
                to_close.append(lot)
                exit_reason = f"{MOMENTUM_STRATEGY}:eod_green"
                continue
            if in_eod and mark < lot.gate_entry_price:
                log.info(
                    "futures momentum hold overnight (red/flat at close)",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "mark": mark,
                            "entry": lot.entry_price,
                        }
                    },
                )

        if not to_close:
            return
        seen: set[int] = set()
        uniq: list[Position] = []
        for lot in to_close:
            if lot.id in seen:
                continue
            seen.add(lot.id)
            uniq.append(lot)
        ctx = _futures_risk_ctx(
            settings,
            now=now,
            halted=halted,
            can_trade=can_trade,
            daily_killed=daily_killed,
            open_count_for_pair=len(ledger.open_positions(product)),
            last_entry_at=ledger.last_entry_at(product, MOMENTUM_STRATEGY),
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
        if not ok:
            log.info(
                "futures momentum exit blocked",
                extra={"data": {"product": product, "reason": reason}},
            )
            return
        signal_context.stage(
            product, exit_reason, lane="futures", strategy=MOMENTUM_STRATEGY, action="exit",
            side=("buy" if any(l.side == "short" for l in uniq) else "sell"),
            rule={"take_profit": "lot P/L >= take-profit %", "stall_exit": "price stalled and P/L >= stall %",
                  "eod_green": "end-of-day window and lot green"}.get(exit_reason.split(":")[-1], exit_reason),
            price=mark, now=now, timeframe=str(getattr(settings, "futures_momentum_timeframe", "15m") or "15m"),
            indicators={"stalled": bool(stalled), "stalled_short": bool(stalled_short), "in_eod_window": bool(in_eod)},
            target={"take_profit_pct": tp, "stall_exit_pct": stall_pct},
            gates={"never_sell_red": bool(settings.never_sell_red), "allow_exit": "passed",
                   "lots": [{"id": l.id, "side": l.side, "entry": l.gate_entry_price,
                             "pnl_pct": side_pnl_pct(l.side, l.gate_entry_price, mark)} for l in uniq]},
        )
        self._close_lots(product, uniq, marks, reason=exit_reason, now=now)

    # --- Task 16: research_idea (autonomous high-confidence proposals) ---------

    def _act_research_idea(
        self,
        product: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        from snowball.engine import RESEARCH_IDEA as sid
        from snowball.futures.stock_strategies import family_lots

        settings = self.state.settings
        ledger = self.state.futures_ledger
        store = self.state.proposals
        assert ledger is not None and store is not None
        snap = self.state.futures_pairs.get(product)
        mark = marks.get(product) if marks.get(product) is not None else (snap.last if snap else None)
        if mark is None:
            return
        tp = float(getattr(settings, "futures_momentum_take_profit_pct", 0.008) or 0.0)
        lots = ledger.open_positions(product)
        own = [lot for lot in lots if lot.strategy == sid]
        if own:
            to_close: list[Position] = []
            exit_reason = f"{sid}:take_profit"
            thesis = "unknown"
            for lot in own:
                pnl = side_pnl_pct(lot.side, lot.gate_entry_price, mark)
                if pnl is None or pnl < 0:
                    continue  # never-sell-red / never-cover-red: hold
                if tp > 0 and pnl >= tp:
                    to_close.append(lot)
                    continue
                pid = None
                try:
                    r = ledger._conn.execute(
                        "SELECT proposal_id FROM fills WHERE position_id=? AND proposal_id IS NOT NULL "
                        "ORDER BY id LIMIT 1", (lot.id,)).fetchone()
                    pid = int(r[0]) if r and r[0] is not None else None
                except Exception:  # noqa: BLE001
                    pid = None
                thesis = store.thesis_state(pid, product=product, side=lot.side, now=now)
                fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)
                if thesis in ("expired", "invalidated") and pnl > fee_buf:
                    to_close.append(lot)
                    exit_reason = f"{sid}:thesis_{thesis}"
            if not to_close:
                return
            ctx = _futures_risk_ctx(
                settings, now=now, halted=halted, can_trade=can_trade, daily_killed=daily_killed,
                open_count_for_pair=len(lots), last_entry_at=ledger.last_entry_at(product, sid),
                cash_usd=ledger.cash_usd(), requested_notional=self._last_budget["per_index_usd"],
                cooldown_seconds=0,
                max_notional=max(self._last_budget["per_index_usd"], float(settings.futures_max_notional_usd)),
            )
            ok, why = allow_exit(ctx)
            if not ok:
                log.info("research_idea exit blocked", extra={"data": {"product": product, "reason": why}})
                return
            signal_context.stage(
                product, exit_reason, lane="futures", strategy=sid, action="exit",
                side=("buy" if any(l.side == "short" for l in to_close) else "sell"),
                rule=("lane green target reached" if exit_reason.endswith("take_profit")
                      else f"thesis {thesis} and lot green"),
                price=mark, snap=snap, now=now, target={"take_profit_pct": tp},
                gates={"never_sell_red": True, "allow_exit": "passed", "thesis_state": thesis,
                       "lots": [{"id": l.id, "side": l.side, "entry": l.gate_entry_price,
                                 "pnl_pct": side_pnl_pct(l.side, l.gate_entry_price, mark)} for l in to_close]},
                match_proposal=False,
            )
            self._close_lots(product, to_close, marks, reason=exit_reason, now=now)
            return
        prop = None
        side = "long"
        for sd in ("long", "short"):
            prop = store.match_live(product=product, side=sd, lane="futures", now=now, autonomous_only=True)
            if prop is not None:
                side = sd
                break
        if prop is None:
            return
        note = getattr(store, "note_engine_block", None)

        def _blocked(why: str) -> None:
            if note is not None:
                try:
                    note(int(prop["id"]), why, now)
                except Exception:  # noqa: BLE001
                    pass

        if lots or family_lots(ledger, product, self.product_list()):
            _blocked("one position per product family (family already holds a lot)")
            return  # one position per product family
        from snowball.futures.stops import futures_entry_gate

        ok_gate, why_gate = futures_entry_gate(
            self.state, product, "futures", now, side=side, market=self.market,
            live=bool(self.state.settings.futures_live_orders_permitted()),
        )
        if not ok_gate:
            _blocked(f"futures entry gate: {why_gate}")
            return
        if side == "short" and not self._short_allowed(product):
            _blocked("shorts not permitted (FUTURES_SHORT_PRODUCTS)")
            return
        if self._expiry_blocks_entry(product, now):
            _blocked("contract expiry window")
            return
        if is_cfm_product(product):
            req1 = self._one_contract_margin_usd(product, float(mark), side)
            if req1 > self._budget_left_usd(ledger):
                log.info("research_idea entry blocked: 1-contract margin exceeds FT budget left",
                         extra={"data": {"product": product}})
                _blocked("1-contract margin exceeds FT budget left")
                return
        notional = self._session_entry_notional(product, ledger)
        if notional <= 1.0:
            _blocked("no FT budget left")
            return
        ctx = _futures_risk_ctx(
            settings, now=now, halted=halted, can_trade=can_trade, daily_killed=daily_killed,
            open_count_for_pair=0, last_entry_at=ledger.last_entry_at(product, sid),
            cash_usd=max(ledger.cash_usd(), notional), requested_notional=notional, cooldown_seconds=0,
            max_notional=max(notional, float(settings.futures_max_notional_usd)),
        )
        ok, why = allow_entry(ctx)
        if not ok:
            log.info("research_idea entry blocked", extra={"data": {"product": product, "reason": why}})
            _blocked(f"risk gate: {why}")
            return
        reason = f"{sid}:enter" if side == "long" else f"{sid}:short_enter"
        from snowball.engine import _research_rule

        signal_context.stage(
            product, reason, lane="futures", strategy=sid, action="entry", side=side,
            rule=_research_rule(prop, settings),
            price=mark, snap=snap, now=now, target={"take_profit_pct": tp},
            gates={"one_position_per_product": "passed (family flat)", "expiry_window": "clear",
                   "allow_entry": "passed", "notional_usd": notional, "contracts_cap": settings.cfm_max_contracts_per_index(),
                   "leverage": settings.cfm_order_leverage(), "sizing": "same FT notional (no research boost)",
                   "halted": bool(halted)},
            proposal=prop, match_proposal=False,
        )
        self._open_lot(product, marks, reason=reason, now=now, strategy=sid, notional_usd=notional, side=side)

    def _session_entry_notional(self, product: str, ledger: PaperLedger) -> float:
        """Shared FT notional sizing for session_day and momentum_15m entries."""
        settings = self.state.settings
        per_index = float(self._last_budget["per_index_usd"])
        # Contract-size + per-product margin aware (US5/TEK: size 1, 10% → unchanged).
        budget_left = self._budget_left_usd(ledger)
        per_leg = float(
            self._last_budget.get("per_leg_notional_usd")
            or settings.effective_per_leg_notional_usd(
                float(self._last_budget.get("account_value_usd") or 0.0)
            )
        )
        if is_cfm_product(product):
            notional = max(budget_left, float(settings.futures_max_notional_usd))
            if budget_left <= 1.0:
                notional = 0.0
        else:
            notional = min(per_index, budget_left, per_leg)
        return float(notional)


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
            if strategy_id in FT_PRIMARY_STRATEGIES:
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
            signal_context.stage(
                product, exit_reason, lane="futures", strategy=strategy_id, action="exit", side="sell",
                rule=("signal EXIT after green floor" if exit_reason.endswith(":exit") else "momentum fade after green floor"),
                price=mark, snap=snap, now=now, indicators={"signal": signal.value, "fade": bool(fade)},
                floor={"min_take_profit_pct": settings.min_take_profit_pct_for(strategy_id)},
                gates={"never_sell_red": bool(settings.never_sell_red), "allow_exit": "passed",
                       "lots": [{"id": l.id, "entry": l.gate_entry_price,
                                 "pnl_pct": lot_unrealized_pnl_pct(l, mark)} for l in lots_to_close]},
            )
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
        signal_context.stage(
            product, entry_reason, lane="futures", strategy=strategy_id, action="entry", side="long",
            rule=("signal ENTER" if signal is Signal.ENTER else "scale-in in uptrend"), price=mark, snap=snap,
            now=now, indicators={"signal": signal.value, "uptrend": bool(uptrend)},
            target={"min_take_profit_pct": settings.min_take_profit_pct_for(strategy_id)},
            gates={"trend_filter": reason_tf or "passed", "allow_entry": "passed", "notional_usd": allot},
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
        side: str = "long",
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        ledger = self.state.futures_ledger
        assert ledger is not None
        side = "short" if str(side).lower() == "short" else "long"
        from snowball.futures.stops import futures_entry_gate

        ok_gate, why_gate = futures_entry_gate(
            self.state, product, "futures", now, side=side, market=self.market,
            live=bool(self.state.settings.futures_live_orders_permitted()),
        )
        if not ok_gate:
            return
        if side == "short" and not self._short_allowed(product):
            log.warning(
                "futures short refused: product not in FUTURES_SHORT_PRODUCTS",
                extra={"data": {"product": product}},
            )
            return
        if is_cfm_product(product):
            # Hard guard: ONE net position per product family for the whole FT
            # lane (session, momentum, merged stock strategies), <= CFM cap.
            from snowball.futures.stock_strategies import family_contracts

            held = family_contracts(ledger, product, self.product_list())
            cap = int(settings.cfm_max_contracts_per_index())
            if held + 1 > cap:
                log.warning(
                    "futures entry refused: one_net_position",
                    extra={"data": {"product": product, "strategy": strategy,
                                    "held_contracts": held, "cap": cap, "reason": reason}},
                )
                return
        snap = self.state.futures_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        if notional_usd is not None:
            target = float(notional_usd)
        elif is_cfm_product(product):
            target = float(
                self._last_budget.get("budget_usd")
                or self._last_budget.get("per_index_usd")
                or settings.futures_max_notional_usd
            )
        else:
            target = float(
                self._last_budget.get("per_leg_notional_usd")
                or settings.per_leg_base_usd
            )
        if settings.futures_live_orders_permitted():
            self._open_lot_live(
                product=product,
                ticker=ticker,
                reason=reason,
                now=now,
                strategy=strategy,
                notional_usd=target,
                side=side,
            )
            return
        px = fill_price(ticker, "buy" if side == "long" else "sell", settings.slippage_bps)
        cs = contract_size_for_product(product)
        if is_cfm_product(product):
            contracts = order_size_for_product(
                product,
                notional_usd=target,
                price=float(px),
                available_margin_usd=max(
                    float(ledger.cash_usd()),
                    float(target),
                    float(settings.futures_max_notional_usd),
                ),
                max_contracts=settings.cfm_max_contracts_per_index(),
                leverage=settings.cfm_order_leverage(),
                margin_rate=margin_rate_for_product(
                    product, side, float(settings.cfm_margin_rate)
                ),
                lane_max_notional_usd=settings.futures_max_notional_usd,
                contract_size=cs,
            )
            if contracts < 1:
                log.info(
                    "futures paper buy skipped: cannot fund 1 CFM contract",
                    extra={"data": {"product": product, "target": target, "px": px}},
                )
                return
            notional = float(contracts) * float(cs) * float(px)
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
        opener = ledger.open_short if side == "short" else ledger.open_buy
        pos, fill = opener(
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
            "futures paper buy" if side == "long" else "futures paper sell short",
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
        side: str = "long",
    ) -> None:
        settings = self.state.settings
        if not settings.futures_live_orders_permitted():
            raise LiveTradingRefused("futures live open refused: dual gate not set")
        side = "short" if str(side).lower() == "short" else "long"
        order_side = "buy" if side == "long" else "sell"
        cs = contract_size_for_product(product)
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
        margin_rate = margin_rate_for_product(
            product, side, float(getattr(settings, "cfm_margin_rate", 0.10))
        )
        avail = max(float(ledger.cash_usd()), float(notional_usd))
        amount = order_size_for_product(
            product,
            notional_usd=notional_usd,
            price=float(ref),
            available_margin_usd=max(avail, float(settings.futures_max_notional_usd)),
            max_contracts=max_c,
            leverage=lev,
            margin_rate=margin_rate,
            lane_max_notional_usd=settings.futures_max_notional_usd,
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
            from snowball.maker import maker_buy_price, maker_sell_price
            from snowball.futures.market import (
                is_invalid_limit_price_post_only,
                tick_size_for_product,
            )

            # CFM: this product's book only (never US5 mark on TEK).
            if is_cfm_product(product):
                bid2, ask2 = self.market.fetch_bba(product)
                if bid2 is not None:
                    bid = bid2
                if ask2 is not None:
                    ask = ask2
            tick = (
                tick_size_for_product(product, getattr(self.market, "exchange", None))
                if is_cfm_product(product)
                else None
            )
            maker_px = maker_buy_price if side == "long" else maker_sell_price
            limit_px = maker_px(bid, ask, ref, tick=tick)
            if limit_px is None or limit_px <= 0:
                bid2, ask2 = self.market.fetch_bba(product)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_px(bid, ask, ref, tick=tick)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "futures live buy skipped: no book bid for maker",
                    extra={"data": {"product": product, "bid": bid, "ask": ask, "tick": tick}},
                )
                return
            amount = order_size_for_product(
                product,
                notional_usd=notional_usd,
                price=float(limit_px),
                available_margin_usd=max(avail, float(settings.futures_max_notional_usd)),
                max_contracts=max_c,
                leverage=lev,
                margin_rate=margin_rate,
                lane_max_notional_usd=settings.futures_max_notional_usd,
            )
            if amount < min_amt:
                log.info(
                    "futures live buy skipped: amount below min",
                    extra={"data": {"product": product, "limit_px": limit_px}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            try:
                order = self.market.create_swap_maker_limit_order(
                    product,
                    order_side,
                    amount,
                    price=limit_px,
                    bid=bid,
                    ask=ask,
                    leverage=lev,
                    reduce_only=False,
                    timeout_sec=timeout,
                )
            except Exception as post_exc:  # noqa: BLE001
                times = session_times_from_settings(settings)
                in_window = entry_allowed(
                    now,
                    entry_start=times["entry_start"],
                    exit_start=times["exit_start"],
                )
                if not (
                    is_invalid_limit_price_post_only(post_exc)
                    and in_window
                ):
                    raise
                # One taker fallback so the second index can still fill this session.
                log.warning(
                    "futures post-only rejected; one taker fallback",
                    extra={
                        "data": {
                            "product": product,
                            "error": str(post_exc),
                            "bid": bid,
                            "ask": ask,
                            "limit_px": limit_px,
                            "tick": tick,
                        }
                    },
                )
                bid3, ask3 = self.market.fetch_bba(product)
                bid = bid3 if bid3 is not None else bid
                ask = ask3 if ask3 is not None else ask
                far = ask if side == "long" else bid
                if far is not None and float(far) > 0:
                    order = self.market.create_swap_maker_limit_order(
                        product,
                        order_side,
                        amount,
                        price=float(far),
                        bid=bid,
                        ask=ask,
                        leverage=lev,
                        reduce_only=False,
                        timeout_sec=timeout,
                        post_only=False,
                    )
                else:
                    order = self.market.create_swap_market_order(
                        product,
                        order_side,
                        amount,
                        leverage=lev,
                        reduce_only=False,
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "futures live buy failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        fill_px, fill_contracts, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_contracts <= 0:
            log.error(
                "futures live buy unfilled/canceled maker limit"
                if side == "long"
                else "futures live short unfilled/canceled maker limit",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        # ccxt filled = CFM contracts; ledger qty = underlying units.
        fill_qty = float(fill_contracts) * float(cs) if is_cfm_product(product) else float(fill_contracts)
        # Ensure ledger can record (futures book is separate; top-up cash for accounting)
        cost = fill_qty * fill_px + fee_usd
        if ledger.cash_usd() + 1e-9 < cost:
            try:
                ledger.set_cash_usd(cost * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("futures live ledger cash sync failed")
        opener = ledger.open_short if side == "short" else ledger.open_buy
        pos, fill = opener(
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
            "futures LIVE buy" if side == "long" else "futures LIVE sell short",
            extra={
                "data": {
                    "product": product,
                    "strategy": strategy,
                    "side": side,
                    "contracts": fill_contracts,
                    "contract_size": cs,
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

    def _stop_book(self, lot: Position, fill_px: float, fee_usd: float, reason: str, now: datetime) -> float:
        settings = self.state.settings
        ledger = self.state.futures_ledger
        assert ledger is not None
        if lot.side == "short":
            fill = ledger.cover_short(position_id=lot.id, fill_px=fill_px, slippage_bps=0.0,
                                      fee_usd=fee_usd, reason=reason, ts=now)
            realized = (lot.entry_price - fill.price) * fill.qty - fill.fee_usd
        else:
            fill = ledger.close_position(position_id=lot.id, fill_px=fill_px, slippage_bps=0.0,
                                         fee_usd=fee_usd, reason=reason, ts=now)
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
        try:
            ledger.record_closed_trade_for_pause(
                lot.product, realized, now=now, enabled=settings.pair_pause_enabled,
                loss_threshold=settings.pair_pause_losses, pause_hours=settings.pair_pause_hours)
        except Exception:  # noqa: BLE001
            log.exception("pair pause record after stop failed")
        if lot.product in self.state.futures_pairs:
            self.state.futures_pairs[lot.product].open_count = ledger.open_count(lot.product)
        return realized

    def _stop_tick(self, *, now: datetime, can_trade: bool) -> None:
        from snowball.futures.stops import LaneHooks, run_stops

        ledger = self.state.futures_ledger
        if ledger is None:
            return

        def _skip(lot: Position) -> bool:
            try:
                return bool(self._roll.in_flight(lot.id))
            except Exception:  # noqa: BLE001
                return False

        run_stops(self.state, LaneHooks(
            lane="futures", ledger=ledger, market=self.market, pairs=self.state.futures_pairs,
            live=bool(self.state.settings.futures_live_orders_permitted()), book=self._stop_book, skip=_skip,
        ), now=now, can_trade=can_trade)

    def _netted_skip(self, product: str, lot: Position, reason: str) -> bool:
        from snowball.futures.stops import is_netted

        if is_netted(self.state, "futures", lot.id):
            log.info("futures close skipped: netted pair (bookkeeping only; exchange nets to 0)",
                     extra={"data": {"product": product, "position_id": lot.id, "reason": reason}})
            return True
        from snowball.futures.stops import _throttled, close_pending

        if close_pending(self.state, "futures", lot.id):
            if _throttled(("close_pending", "futures", lot.id)):
                log.info("futures close skipped: stop/aged-exit order already in flight for this lot",
                         extra={"data": {"product": product, "position_id": lot.id, "reason": reason}})
            return True
        return False

    def _net_green_hold(self, product: str, lot: Position, exit_px: float | None, reason: str, now: datetime) -> bool:
        """True = HOLD (net P&L after round-trip fees <= FUTURES_NET_GREEN_BUFFER_USD).

        Applies to every FT close reason (strategy, fade, stall, EOD, session close, HALT and
        daily-loss flatten). Only contract_roll is exempt (its own path in futures/roll.py).
        """
        from snowball.futures.net_green import is_roll_reason, net_green_check

        from snowball.futures.stops import is_stop_reason

        if is_roll_reason(reason) or is_stop_reason(reason):
            return False
        settings = self.state.settings
        ledger = self.state.futures_ledger
        if is_cfm_product(product):
            contracts = float(contracts_from_qty(product, float(lot.qty)))
            csize = float(contract_size_for_product(product))
        else:
            contracts, csize = float(lot.qty), 1.0
        entry_fee = None
        try:
            entry_fee = ledger.open_fee_usd(lot.id) if hasattr(ledger, "open_fee_usd") else None
        except Exception:  # noqa: BLE001
            entry_fee = None
        res = net_green_check(
            side=lot.side, basis=float(lot.gate_entry_price), qty=float(lot.qty), exit_px=exit_px,
            contracts=contracts, contract_size=csize,
            fee_pct_per_side=float(getattr(settings, "futures_fee_pct_per_side", 0.0) or 0.0),
            fee_per_contract=float(getattr(settings, "futures_fee_per_contract", 0.12) or 0.0),
            buffer_usd=float(getattr(settings, "futures_net_green_buffer_usd", 0.50) or 0.0),
            entry_fee_usd=entry_fee,
        )
        if res.ok:
            return False
        key = (int(lot.id), str(reason).split(":", 1)[-1] if ":" in str(reason) else str(reason))
        log_at = self.__dict__.setdefault("_net_green_log_at", {})
        last = log_at.get(key)
        mono = time.monotonic()
        if last is None or mono - last >= 300.0:
            log_at[key] = mono
            log.info(
                "futures close held: not net green after fees",
                extra={"data": {"product": product, "position_id": lot.id, "side": lot.side,
                                "strategy": lot.strategy, "reason": reason,
                                "emergency": is_emergency_flatten_reason(reason), **res.as_dict()}},
            )
        return True

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
        paper_px_sell = fill_price(ticker, "sell", settings.slippage_bps)
        paper_px_buy = fill_price(ticker, "buy", settings.slippage_bps)
        for lot in lots:
            paper_px = paper_px_buy if lot.side == "short" else paper_px_sell
            if self._netted_skip(product, lot, reason):
                continue
            if self._net_green_hold(product, lot, paper_px, reason, now):
                continue
            if not is_emergency_flatten_reason(reason):
                # Session closes already gated to green; legacy uses strategy_exit_allowed
                is_ft_primary = (
                    SESSION_STRATEGY in reason
                    or MOMENTUM_STRATEGY in reason
                    or lot.strategy in FT_PRIMARY_STRATEGIES
                    or is_stall_exit_reason(reason)  # merged stock stall bank: red check only
                )
                if not is_ft_primary:
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
                elif settings.never_sell_red and (
                    (side_pnl_pct(lot.side, lot.gate_entry_price, paper_px) or 0.0) < 0
                ):
                    continue
            if lot.side == "short":
                fill = ledger.cover_short(
                    position_id=lot.id,
                    fill_px=paper_px,
                    slippage_bps=settings.slippage_bps,
                    fee_usd=0.0,
                    reason=reason,
                    ts=now,
                )
                realized = (lot.entry_price - fill.price) * fill.qty - fill.fee_usd
            else:
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
            if is_emergency_flatten_reason(reason) and realized < 0:
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
        from snowball.futures.net_green import conservative_exit_px

        for lot in lots:
            is_short = lot.side == "short"
            close_side = "buy" if is_short else "sell"
            # Net-green gate at the worst touch (long -> bid, short -> ask), all reasons but roll.
            if self._netted_skip(product, lot, reason):
                continue
            ng_px = conservative_exit_px(lot.side, ticker.bid, ticker.ask, ticker.reference or ticker.last)
            if self._net_green_hold(product, lot, ng_px, reason, now):
                continue
            if is_cfm_product(product):
                # CFM has no reduce-only: refuse any close that would open/flip an exchange position.
                from snowball.futures.stops import verify_close_allowed

                ok_pos, why_pos, ex_n = verify_close_allowed(
                    self.market, product, lot.side, int(contracts_from_qty(product, float(lot.qty))))
                if not ok_pos:
                    log.error("futures live close refused by exchange-position check",
                              extra={"data": {"product": product, "position_id": lot.id, "side": lot.side,
                                              "reason": reason, "why": why_pos, "exchange_contracts": ex_n}})
                    continue
            if not is_emergency_flatten_reason(reason):
                ref = ticker.reference or ticker.last
                red = ref is not None and (
                    (side_pnl_pct(lot.side, lot.gate_entry_price, ref) or 0.0) < 0
                )
                if settings.never_sell_red and red:
                    log.info(
                        "futures live close skipped never_sell_red"
                        if not is_short
                        else "futures live cover skipped never_cover_red",
                        extra={"data": {"product": product, "position_id": lot.id}},
                    )
                    continue
            if is_cfm_product(product):
                # Ledger qty is underlying units; order in integer contracts.
                amount = float(contracts_from_qty(product, float(lot.qty)))
            else:
                amount = round_amount_down(lot.qty, 0.01)
                if amount < 0.01:
                    continue
            try:
                from snowball.maker import (
                    URGENT_MAKER_TIMEOUT_SEC,
                    maker_buy_price,
                    maker_sell_price,
                    session_close_urgent,
                )

                maker_close_px = maker_buy_price if is_short else maker_sell_price
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
                        product, close_side, amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
                    )
                else:
                    bid, ask = ticker.bid, ticker.ask
                    sell_px = maker_close_px(bid, ask, ticker.last)
                    if sell_px is None:
                        bid2, ask2 = self.market.fetch_bba(product)
                        bid = bid if bid is not None else bid2
                        ask = ask if ask is not None else ask2
                        sell_px = maker_close_px(bid, ask, ticker.last)
                    if sell_px is None:
                        if urgent:
                            # Session would miss close — careful market fallback.
                            log.warning(
                                "futures session close maker skipped; market fallback",
                                extra={"data": {"product": product, "position_id": lot.id}},
                            )
                            order = self.market.create_swap_market_order(
                                product, close_side, amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
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
                            close_side,
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
                                product, close_side, amount, leverage=(settings.cfm_order_leverage() if is_cfm_product(product) else 1.0), reduce_only=True
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
            if is_short:
                fill = ledger.cover_short(
                    position_id=lot.id,
                    fill_px=fill_px,
                    slippage_bps=0.0,
                    fee_usd=fee_usd,
                    reason=reason,
                    ts=now,
                )
                realized = (lot.entry_price - fill.price) * fill.qty - fill.fee_usd
            else:
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
                "futures LIVE sell" if not is_short else "futures LIVE buy to cover",
                extra={
                    "data": {
                        "product": product,
                        "side": lot.side,
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
                signal_context.stage(
                    product, reason, lane="futures", strategy="(all)", action="exit",
                    rule=f"emergency flatten: {reason}", price=marks.get(product),
                    snap=self.state.futures_pairs.get(product), now=now, gates={"emergency": True},
                )
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
    ledger = FuturesLedger(settings.futures_sqlite_path, settings.futures_bankroll_usd)
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
                "session_products": sorted(engine._session_products()),
                "short_products": settings.futures_short_product_list,
                "strategies": settings.futures_strategy_list,
                "mark_source": "coinbase_perp",
                "futures_mode": settings.futures_mode,
                "futures_live_enabled": settings.futures_live_enabled,
                "live_orders": live,
                "budget_pct": settings.futures_account_budget_pct,
                "session_engine": settings.futures_uses_session_engine(),
                "momentum_engine": settings.futures_uses_momentum(),
            }
        },
    )
    return engine
