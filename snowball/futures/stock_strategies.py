"""Former STOCK-lane strategies running inside the Future Trader lane (Task 10, 2026-09-30).

The stock lane only ever traded CFM US5/TEK futures, so it is merged INTO the
Future Trader (FT) lane instead of being retired:

* Strategies: the STOCK_STRATEGIES list (sma_15m, sma_5m, sma_1d, ema_15m,
  donchian_1d, rsi_15m, bb_15m, rsi_1d, bb_1d) run on the US5/TEK families with
  the same signal / fade / stall-take-profit / indicator-filter logic as before.
* Budget: the FT lane's single budget (FUTURES_ACCOUNT_BUDGET_PCT) via
  ``FuturesEngine._session_entry_notional``; sizing, CFM_MAX_CONTRACTS, leverage,
  never-sell-red, contract roll and live order paths are the FT lane's own.

ONE NET POSITION PER PRODUCT FAMILY (the rule)
  The whole FT lane (session_day, momentum_15m and every merged stock strategy)
  holds at most ONE net position per product family (US5, TEK, ...), capped at
  CFM_MAX_CONTRACTS contracts. A strategy may only ENTER when the lane is flat
  in that family; the first strategy to fire in a tick owns the lot (evaluation
  order: session_day, momentum_15m, then the stock strategies in configured
  order). No scale-in, no second lot, no opposite-side lot. Only the owning
  strategy (or contract roll / HALT / daily-kill emergency paths) exits it.
  ``FuturesEngine._open_lot`` enforces the cap again as a hard guard.
"""

from __future__ import annotations

from snowball import signal_context

import logging
from datetime import datetime, timezone
from typing import Any

from snowball.futures.market import is_cfm_product, normalize_futures_product
from snowball.futures.session import (
    DEFAULT_CASH_END,
    DEFAULT_CASH_START,
    _parse_hhmm,
    in_us_cash_session,
)
from snowball.gates import (
    indicator_filters_allow,
    indicator_snapshot_for_strategy,
    lot_unrealized_pnl_pct,
    momentum_fading,
    price_stalled,
    sma_fast_for_strategy,
    sma_slow_for_strategy,
    stall_exit_allowed,
    strategy_exit_allowed,
    trend_filter_allows,
)
from snowball.models import PairSnapshot, Position, Signal
from snowball.risk import allow_entry, allow_exit
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

log = logging.getLogger("snowball.futures.stock_strategies")

MIGRATION_REASON_TAG = "migrated_from_stock_lane"


def family_lots(ledger: Any, product: str, products: list[str]) -> list[Position]:
    """All open FT lots in the same contract family as ``product`` (any month)."""
    from snowball.futures.roll import family_of

    fam = family_of(product)
    out: list[Position] = []
    seen: set[int] = set()
    cands = set(products) | {product}
    try:
        cands |= {lot.product for lot in ledger.open_positions()}
    except Exception:  # noqa: BLE001
        pass
    for p in cands:
        if family_of(p) != fam:
            continue
        for lot in ledger.open_positions(p):
            if lot.id not in seen:
                seen.add(lot.id)
                out.append(lot)
    return out


def family_contracts(ledger: Any, product: str, products: list[str]) -> float:
    from snowball.futures.market import contracts_from_qty

    total = 0.0
    for lot in family_lots(ledger, product, products):
        try:
            total += float(contracts_from_qty(lot.product, float(lot.qty)))
        except Exception:  # noqa: BLE001
            total += 1.0
    return total


def refresh_indicators(snap: PairSnapshot, market: Any, product: str, settings: Any,
                       strategy_ids: list[str]) -> PairSnapshot:
    """Stock-lane indicator refresh (verbatim logic) on FT market candles."""
    wanted = set(strategy_ids)
    limit = settings.ohlcv_fetch_limit
    frames = enabled_timeframes(strategy_ids) or ["1d"]
    if (
        bool(getattr(settings, "stock_cfm_stall_exit_enabled", True))
        and "15m" not in frames
        and is_cfm_product(product)
    ):
        frames = list(frames) + ["15m"]
    snap.stalled_15m = False
    for timeframe in frames:
        try:
            rows = market.fetch_ohlcv(product, timeframe, limit)
            closes = [float(r[4]) for r in rows]
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            candle_ts = None
            if rows:
                candle_ts = datetime.fromtimestamp(float(rows[-1][0]) / 1000.0, tz=timezone.utc)
            sig = crossover_signal(closes, settings.sma_fast, settings.sma_slow)
            fast_v = sma(closes, settings.sma_fast)
            slow_v = sma(closes, settings.sma_slow)
            if timeframe == "15m" and SMA_15M in wanted and len(closes) >= settings.sma_slow:
                snap.sma_fast, snap.sma_slow, snap.signal, snap.candle_ts = fast_v, slow_v, sig.value, candle_ts
            elif timeframe == "5m" and SMA_5M in wanted and len(closes) >= settings.sma_slow:
                snap.sma_fast_5m, snap.sma_slow_5m = fast_v, slow_v
                snap.signal_5m, snap.candle_ts_5m = sig.value, candle_ts
            elif timeframe == "1d" and SMA_1D in wanted:
                snap.sma_fast_1d, snap.sma_slow_1d = fast_v, slow_v
                snap.signal_1d, snap.candle_ts_1d = sig.value, candle_ts
                if snap.sma_fast is None and fast_v is not None:
                    snap.sma_fast, snap.sma_slow, snap.signal, snap.candle_ts = fast_v, slow_v, sig.value, candle_ts
            if timeframe == "15m" and EMA_15M in wanted:
                snap.ema_fast_15m = ema(closes, 12)
                snap.ema_slow_15m = ema(closes, 26)
                snap.signal_ema_15m = ema_crossover_signal(closes).value
                if snap.candle_ts is None:
                    snap.candle_ts = candle_ts
            if timeframe == "1d" and DONCHIAN_1D in wanted:
                high, low = donchian_channels(highs, lows)
                snap.donchian_high_1d, snap.donchian_low_1d = high, low
                snap.signal_donchian_1d = donchian_breakout_signal(closes, highs, lows).value
                if snap.candle_ts_1d is None:
                    snap.candle_ts_1d = candle_ts
            populate_rsi_bb(
                snap, closes, timeframe, wanted=wanted,
                filters_enabled=bool(getattr(settings, "indicator_filters_enabled", True)),
                rsi_period=int(getattr(settings, "rsi_period", 14) or 14),
                bb_period=int(getattr(settings, "bb_period", 20) or 20),
                bb_std_mult=float(getattr(settings, "bb_std_mult", 2.0) or 2.0),
            )
            if timeframe == "15m" and bool(getattr(settings, "stock_cfm_stall_exit_enabled", True)):
                mark_for_stall = snap.last if snap.last is not None else (closes[-1] if closes else None)
                snap.stalled_15m = price_stalled(
                    highs, lows, closes, mark_for_stall,
                    lookback=int(getattr(settings, "stock_cfm_stall_lookback_bars", 5) or 5),
                    new_high_tol=float(getattr(settings, "stock_cfm_stall_new_high_tol", 0.002) or 0.002),
                    range_compress_pct=float(getattr(settings, "stock_cfm_stall_range_compress_pct", 0.006) or 0.006),
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("ft stock-strategy ohlcv failed",
                          extra={"data": {"product": product, "timeframe": timeframe}})
            snap.last_error = str(exc)
    return snap


class MergedStockStrategies:
    """Runs the former stock strategies on the FT ledger/budget/order paths."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.last_decisions: dict[str, dict[str, Any]] = {}

    # --- config ---------------------------------------------------------------
    @property
    def settings(self) -> Any:
        return self.engine.state.settings

    def enabled(self) -> bool:
        return bool(getattr(self.settings, "futures_stock_merge_enabled", True)) and bool(self.strategies())

    def strategies(self) -> list[str]:
        try:
            return list(self.settings.futures_stock_strategy_list)
        except Exception:  # noqa: BLE001
            return []

    def products(self) -> list[str]:
        """FT products (front months) whose family is in FUTURES_STOCK_PRODUCTS."""
        from snowball.futures.roll import same_family_member

        try:
            fams = list(self.settings.futures_stock_product_list)
        except Exception:  # noqa: BLE001
            fams = []
        out = [p for p in self.engine.product_list() if same_family_member(p, fams)]
        # open merged-strategy lots are always managed, even if config drifts
        ledger = self.engine.state.futures_ledger
        strat = set(self.strategies())
        if ledger is not None:
            for lot in ledger.open_positions():
                if lot.strategy in strat and lot.product not in out:
                    out.append(lot.product)
        return out

    def entry_disabled(self) -> set[str]:
        """FUTURES_STOCK_ENTRY_DISABLED: these strategies manage their lots but never open new ones."""
        try:
            return set(self.settings.futures_stock_entry_disabled_list)
        except Exception:  # noqa: BLE001
            return set()

    def owns(self, lot: Position) -> bool:
        return lot.strategy in set(self.strategies())

    # --- per tick ---------------------------------------------------------------
    def _pairs(self) -> dict[str, PairSnapshot]:
        st = self.engine.state
        pairs = getattr(st, "futures_stock_pairs", None)
        if pairs is None:
            pairs = {}
            try:
                st.futures_stock_pairs = pairs
            except Exception:  # noqa: BLE001
                pass
        return pairs

    def refresh(self, product: str) -> PairSnapshot:
        pairs = self._pairs()
        ft_snap = self.engine.state.futures_pairs.get(product)
        snap = pairs.get(product) or PairSnapshot(product=product, max_open=1)
        if ft_snap is not None:
            snap.last, snap.bid, snap.ask = ft_snap.last, ft_snap.bid, ft_snap.ask
        refresh_indicators(snap, self.engine.market, product, self.settings, self.strategies())
        ledger = self.engine.state.futures_ledger
        snap.open_count = ledger.open_count(product) if ledger is not None else 0
        pairs[product] = snap
        return snap

    def _roll_busy(self) -> bool:
        """A contract roll is mid-flight (e.g. close-first leg done, open leg pending)."""
        roller = getattr(getattr(self.engine, "_roll", None), "_roller", None)
        try:
            return bool(roller is not None and roller._inflight_ids())
        except Exception:  # noqa: BLE001
            return True

    def _crash_has_opposing_short(self, product: str) -> bool:
        """Kept from the stock lane: skip a long entry while Crash Guard is short."""
        store = getattr(self.engine.state, "crash_ledger", None)
        if store is None:
            return False
        try:
            lots = store.open_positions(normalize_futures_product(product))
        except Exception:  # noqa: BLE001
            return False
        return any(str(getattr(lot, "side", "")).lower() == "short" for lot in lots)

    def tick(self, *, now: datetime, halted: bool, can_trade: bool, daily_killed: bool,
             marks: dict[str, float]) -> None:
        if not self.enabled():
            return
        for product in self.products():
            if product not in self.engine.state.futures_pairs:
                continue
            try:
                self.refresh(product)
                for sid in self.strategies():
                    self.act(product, sid, now=now, halted=halted, can_trade=can_trade,
                             daily_killed=daily_killed, marks=marks)
            except Exception:  # noqa: BLE001
                log.exception("ft stock-strategy tick failed", extra={"data": {"product": product}})

    def _note(self, product: str, sid: str, what: str, **kw: Any) -> None:
        self.last_decisions.setdefault(product, {})[sid] = {"decision": what, **kw}

    def act(self, product: str, strategy_id: str, *, now: datetime, halted: bool, can_trade: bool,
            daily_killed: bool, marks: dict[str, float]) -> None:
        from snowball.futures.engine import _futures_risk_ctx

        eng = self.engine
        settings = self.settings
        ledger = eng.state.futures_ledger
        assert ledger is not None
        snap = self._pairs()[product]
        all_lots = ledger.open_positions(product)
        strategy_lots = [lot for lot in all_lots if lot.strategy == strategy_id]
        mark = marks.get(product)
        if mark is None and snap.last is not None:
            mark = snap.last
        fee_buf = float(getattr(settings, "fee_buffer_pct", 0.0) or 0.0)
        lots_to_close: list[Position] = []
        exit_reason = f"{strategy_id}:exit"

        def _exit_ctx():
            return _futures_risk_ctx(
                settings, now=now, halted=halted, can_trade=can_trade, daily_killed=daily_killed,
                open_count_for_pair=len(all_lots),
                last_entry_at=ledger.last_entry_at(product, strategy_id),
                cash_usd=ledger.cash_usd(), requested_notional=settings.futures_max_notional_usd,
                cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            )

        # CFM stall early-bank (indicator independent), US cash hours only.
        if (strategy_lots and bool(getattr(settings, "stock_cfm_stall_exit_enabled", True))
                and is_cfm_product(product) and bool(getattr(snap, "stalled_15m", False))):
            cs = _parse_hhmm(getattr(settings, "stock_cfm_stall_start_et", "09:30"), DEFAULT_CASH_START)
            ce = _parse_hhmm(getattr(settings, "stock_cfm_stall_end_et", "15:45"), DEFAULT_CASH_END)
            if in_us_cash_session(now, start=cs, end=ce):
                pct = float(getattr(settings, "stock_cfm_stall_exit_pct", 0.05) or 0.05)
                for lot in strategy_lots:
                    ok_st, _r = stall_exit_allowed(lot, mark, stall_exit_pct=pct,
                                                   never_sell_red=settings.never_sell_red)
                    if ok_st:
                        lots_to_close.append(lot)
                if lots_to_close:
                    ok, reason = allow_exit(_exit_ctx())
                    if not ok:
                        self._note(product, strategy_id, "exit_blocked", reason=reason)
                        return
                    signal_context.stage(
                        product, "stall_take_profit", lane="futures", strategy=strategy_id, action="exit",
                        side="sell", rule="cash-session stall and lot P/L >= stall %", price=mark, snap=snap,
                        now=now, floor={"stall_exit_pct": pct},
                        gates={"never_sell_red": bool(settings.never_sell_red), "allow_exit": "passed",
                               "lots": [{"id": l.id, "entry": l.gate_entry_price,
                                         "pnl_pct": lot_unrealized_pnl_pct(l, mark)} for l in lots_to_close]},
                    )
                    eng._close_lots(product, lots_to_close, marks, reason="stall_take_profit", now=now)
                    self._note(product, strategy_id, "stall_take_profit", lots=[l.id for l in lots_to_close])
                    return

        if strategy_id not in MEAN_REVERSION_STRATEGY_IDS:
            if sma_fast_for_strategy(snap, strategy_id) is None or sma_slow_for_strategy(snap, strategy_id) is None:
                self._note(product, strategy_id, "warming")
                return

        signal, uptrend = signal_for_strategy(snap, strategy_id)
        want_entry = signal is Signal.ENTER  # never scale in: one net position per family
        want_signal_exit = signal is Signal.EXIT and len(strategy_lots) > 0
        fade = momentum_fading(last=mark, sma_fast=sma_fast_for_strategy(snap, strategy_id),
                               sma_slow=sma_slow_for_strategy(snap, strategy_id))

        if strategy_lots and want_signal_exit:
            for lot in strategy_lots:
                ok_sw, reason_sw = strategy_exit_allowed(
                    lot, mark, min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                    never_sell_red=settings.never_sell_red, fee_buffer_pct=fee_buf)
                if ok_sw:
                    lots_to_close.append(lot)
                else:
                    log.info("ft stock-strategy swing hold", extra={"data": {
                        "product": product, "strategy": strategy_id, "position_id": lot.id, "reason": reason_sw}})
        elif strategy_lots and fade:
            eligible = [
                lot for lot in strategy_lots
                if strategy_exit_allowed(lot, mark,
                                         min_take_profit_pct=settings.min_take_profit_pct_for(lot.strategy),
                                         never_sell_red=settings.never_sell_red, fee_buffer_pct=fee_buf)[0]
            ]
            if eligible:
                best = max(eligible, key=lambda l: lot_unrealized_pnl_pct(l, mark) or float("-inf"))
                lots_to_close = [best]
                exit_reason = f"{strategy_id}:fade"

        if lots_to_close:
            ok, reason = allow_exit(_exit_ctx())
            if not ok:
                self._note(product, strategy_id, "exit_blocked", reason=reason)
                return
            signal_context.stage(
                product, exit_reason, lane="futures", strategy=strategy_id, action="exit", side="sell",
                rule=("signal EXIT after green floor" if exit_reason.endswith(":exit")
                      else "momentum fade after green floor"),
                price=mark, snap=snap, now=now, indicators={"signal": signal.value, "fade": bool(fade)},
                floor={"min_take_profit_pct": settings.min_take_profit_pct_for(strategy_id), "fee_buffer_pct": fee_buf},
                gates={"never_sell_red": bool(settings.never_sell_red), "allow_exit": "passed",
                       "lots": [{"id": l.id, "entry": l.gate_entry_price,
                                 "pnl_pct": lot_unrealized_pnl_pct(l, mark)} for l in lots_to_close]},
            )
            eng._close_lots(product, lots_to_close, marks, reason=exit_reason, now=now)
            self._note(product, strategy_id, exit_reason, lots=[l.id for l in lots_to_close])
            return

        if strategy_lots:
            self._note(product, strategy_id, "holding", signal=signal.value)
            return
        if want_signal_exit or not want_entry:
            self._note(product, strategy_id, "no_entry_signal", signal=signal.value)
            return

        # 2026-10-16: entry switched off for this strategy (exits above still run).
        if strategy_id in self.entry_disabled():
            self._note(product, strategy_id, "entry_disabled", reason="FUTURES_STOCK_ENTRY_DISABLED")
            return

        # --- ONE NET POSITION PER FAMILY -------------------------------------
        fam = family_lots(ledger, product, eng.product_list())
        if fam:
            self._note(product, strategy_id, "entry_skipped_one_net_position",
                       held_by=[(l.id, l.strategy) for l in fam])
            return
        if eng._expiry_blocks_entry(product, now):
            self._note(product, strategy_id, "entry_skipped_expiry_window")
            return
        if self._roll_busy():
            self._note(product, strategy_id, "entry_skipped_roll_in_flight")
            return
        if self._crash_has_opposing_short(product):
            self._note(product, strategy_id, "entry_skipped_crash_short")
            return
        if settings.pair_pause_enabled and ledger.is_pair_paused(product, now):
            self._note(product, strategy_id, "entry_skipped_pair_paused")
            return
        use_trend = settings.trend_filter_enabled and strategy_id not in MEAN_REVERSION_STRATEGY_IDS
        ok_tf, _r = trend_filter_allows(snap.last, sma_slow_for_strategy(snap, strategy_id), enabled=use_trend)
        if not ok_tf:
            self._note(product, strategy_id, "entry_blocked_trend_filter")
            return
        rsi_v, bb_up, bb_mid, _lo = indicator_snapshot_for_strategy(snap, strategy_id)
        ok_ind, reason_ind = indicator_filters_allow(
            last=snap.last if snap.last is not None else mark, rsi=rsi_v, bb_upper=bb_up, bb_mid=bb_mid,
            enabled=bool(getattr(settings, "indicator_filters_enabled", True)))
        if not ok_ind:
            self._note(product, strategy_id, "entry_blocked_indicator", reason=reason_ind)
            return
        notional = eng._session_entry_notional(product, ledger)
        if notional <= 1.0:
            self._note(product, strategy_id, "entry_skipped_budget_full")
            return
        ctx = _futures_risk_ctx(
            settings, now=now, halted=halted, can_trade=can_trade, daily_killed=daily_killed,
            open_count_for_pair=0, last_entry_at=ledger.last_entry_at(product, strategy_id),
            cash_usd=max(ledger.cash_usd(), notional), requested_notional=notional,
            cooldown_seconds=settings.cooldown_seconds_for(strategy_id),
            max_notional=max(notional, float(settings.futures_max_notional_usd)),
        )
        ok, reason = allow_entry(ctx)
        if not ok:
            self._note(product, strategy_id, "entry_blocked_risk", reason=reason)
            return
        signal_context.stage(
            product, f"{strategy_id}:enter", lane="futures", strategy=strategy_id, action="entry", side="long",
            rule="signal ENTER (no scale-in; one net position per family)", price=mark, snap=snap, now=now,
            indicators={"signal": signal.value, "uptrend": bool(uptrend)},
            target={"min_take_profit_pct": settings.min_take_profit_pct_for(strategy_id)},
            gates={"one_net_position": "passed (family flat)", "expiry_window": "clear", "roll": "idle",
                   "crash_opposing_short": "none", "pair_pause": "clear",
                   "trend_filter": ("on" if use_trend else "off") + ": passed",
                   "indicator_filters": reason_ind or "passed", "allow_entry": "passed", "notional_usd": notional},
        )
        eng._open_lot(product, marks, reason=f"{strategy_id}:enter", now=now, strategy=strategy_id,
                      notional_usd=notional)
        self._note(product, strategy_id, "enter_attempted")

    def payload(self) -> dict[str, Any]:
        pairs = self._pairs()
        return {
            "enabled": self.enabled(),
            "strategies": self.strategies(),
            "entry_disabled": sorted(self.entry_disabled()),
            "products": sorted(pairs),
            "rule": "one net position per product family across the whole Futures lane "
                    "(session_day, momentum_15m, merged stock strategies); max CFM_MAX_CONTRACTS; "
                    "no scale-in",
            "signals": {
                p: {
                    "last": s.last, "signal_sma_15m": s.signal, "signal_5m": getattr(s, "signal_5m", None),
                    "signal_1d": getattr(s, "signal_1d", None),
                    "signal_ema_15m": getattr(s, "signal_ema_15m", None),
                    "signal_donchian_1d": getattr(s, "signal_donchian_1d", None),
                    "stalled_15m": bool(getattr(s, "stalled_15m", False)),
                    "last_error": s.last_error,
                } for p, s in pairs.items()
            },
            "decisions": self.last_decisions,
        }


# --- reporting: former stock lane archive + combined Futures lane P/L ------------

def legacy_stock_archive(settings: Any) -> dict[str, Any]:
    """Closed P/L history of the retired separate STOCK lane (archive DB, read-only).

    Tagged by original lane so combined Futures all-time / YTD stay honest.
    Migrated-open lots (status ``migrated``) are excluded: they live in the FT DB.
    """
    from pathlib import Path

    from snowball.reports.closed_pnl import current_ct_year, sum_lane_closed_pnl

    db = Path(getattr(settings, "stock_sqlite_path", "./data/snowball_stocks.db"))
    year = current_ct_year()
    out: dict[str, Any] = {"lane": "stocks", "db": str(db), "archived": True, "year": year}
    try:
        lane = sum_lane_closed_pnl(db, year=year)
        out.update({
            "closed_live_all_time_usd": round(lane.all_time, 2),
            "closed_live_ytd_usd": round(lane.ytd, 2),
            "closed_live_all_time_count": lane.all_time_count,
            "closed_live_ytd_count": lane.ytd_count,
            "closed_paper_all_time_usd": round(lane.paper_all_time, 2),
        })
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    try:
        import sqlite3

        if db.exists():
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                out["open_positions"] = con.execute(
                    "SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
                out["migrated_positions"] = [
                    int(r[0]) for r in con.execute("SELECT id FROM positions WHERE status='migrated'")]
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def combined_futures_closed_pnl(settings: Any) -> dict[str, Any]:
    from pathlib import Path

    from snowball.reports.closed_pnl import current_ct_year, sum_lane_closed_pnl

    year = current_ct_year()
    ft = sum_lane_closed_pnl(Path(getattr(settings, "futures_sqlite_path", "./data/snowball_futures.db")), year=year)
    st = legacy_stock_archive(settings)
    s_all = float(st.get("closed_live_all_time_usd") or 0.0)
    s_ytd = float(st.get("closed_live_ytd_usd") or 0.0)
    return {
        "year": year,
        "all_time_usd": round(ft.all_time + s_all, 2),
        "ytd_usd": round(ft.ytd + s_ytd, 2),
        "by_original_lane": {
            "futures": {"all_time_usd": round(ft.all_time, 2), "ytd_usd": round(ft.ytd, 2),
                        "all_time_count": ft.all_time_count, "ytd_count": ft.ytd_count},
            "stocks": {"all_time_usd": round(s_all, 2), "ytd_usd": round(s_ytd, 2),
                       "all_time_count": st.get("closed_live_all_time_count", 0),
                       "ytd_count": st.get("closed_live_ytd_count", 0)},
        },
        "note": "live closed realized P/L; former STOCK lane history from its archive DB",
    }
