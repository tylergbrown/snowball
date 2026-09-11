"""Crash Guard engine — short hedge on SPY/QQQ INTX (paper + dual-gated live)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from snowball.config import LiveTradingRefused, Settings
from snowball.crash.market import (
    DEFAULT_CRASH_PRODUCTS,
    CrashMarket,
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
    parse_futures_order_fill,
    round_amount_down,
)
from snowball.crash.store import CrashStore
from snowball.crash.triggers import (
    btc_24h_twin_log as btc_24h_twin_log,
    evaluate_crash_triggers,
    short_cover_allowed,
    short_unrealized_pnl_pct,
)
from snowball.halt import halt_active, trading_enabled
from snowball.market import fill_price
from snowball.models import PairSnapshot, Position, Ticker, utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState

log = logging.getLogger("snowball.crash.engine")

CRASH_STRATEGY = "crash_guard"


def _refuse_unless_paper_or_dual_live(settings: Settings) -> None:
    if settings.crash_mode == "paper":
        if settings.crash_live_enabled:
            raise LiveTradingRefused(
                "Crash Guard refused: CRASH_LIVE_ENABLED=true while "
                "CRASH_MODE is not live (dual gate required)."
            )
        return
    if not settings.crash_live_orders_permitted():
        raise LiveTradingRefused(
            f"Crash Guard refused: CRASH_MODE={settings.crash_mode!r} "
            f"CRASH_LIVE_ENABLED={settings.crash_live_enabled!r} "
            "(both must be true for live crash shorts)."
        )


class CrashEngine:
    """Short-only crash hedge; never touches long books."""

    def __init__(self, state: AppState, market: CrashMarket | None = None) -> None:
        self.state = state
        self._last_budget: dict[str, float] = {
            "account_value_usd": 0.0,
            "budget_usd": 0.0,
            "per_index_usd": 0.0,
        }
        self._last_triggers: dict[str, dict[str, Any]] = {}
        if market is not None:
            self.market = market
        else:
            s = state.settings
            allow = s.crash_live_orders_permitted()
            self.market = CrashMarket(
                api_key=s.coinbase_api_key,
                api_secret=s.coinbase_api_secret,
                api_passphrase=s.coinbase_api_passphrase,
                allow_orders=allow,
            )

    def product_list(self) -> list[str]:
        settings = self.state.settings
        items = [normalize_futures_product(p) for p in settings.crash_product_list]
        return items or list(DEFAULT_CRASH_PRODUCTS)

    def tick(self) -> None:
        settings = self.state.settings
        if not settings.crash_enabled:
            return
        try:
            _refuse_unless_paper_or_dual_live(settings)
        except LiveTradingRefused as exc:
            log.error(str(exc))
            return
        store = self.state.crash_ledger
        if store is None:
            return

        products = self.product_list()
        for product in products:
            if product not in self.state.crash_pairs:
                self.state.crash_pairs[product] = PairSnapshot(
                    product=product, max_open=settings.crash_max_positions
                )

        now = utcnow()
        halted = halt_active(settings.halt_file)
        can_trade = trading_enabled(settings)

        marks: dict[str, float] = {}
        for product in products:
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        self._refresh_budget(marks)
        self._maybe_log_btc_twin()

        equity = store.equity_usd(marks)
        utc_date, start_eq, killed = store.ensure_utc_day(now, equity)
        if not killed and daily_loss_breached(
            equity, start_eq, settings.crash_daily_loss_kill_usd
        ):
            log.warning(
                "crash daily loss kill",
                extra={
                    "data": {
                        "equity": equity,
                        "start": start_eq,
                        "kill": settings.crash_daily_loss_kill_usd,
                    }
                },
            )
            # Do NOT flatten red shorts on daily kill — never cover red.
            # Only block new entries for the day.
            store.set_daily_killed(utc_date)
            killed = True

        if halted:
            log.info("crash halt active — no new shorts; existing shorts held")

        for product in products:
            self._act_on_product(
                product=product,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=killed,
                marks=marks,
            )

        self.state.crash_account_value_usd = self._last_budget["account_value_usd"]
        self.state.crash_budget_usd = self._last_budget["budget_usd"]
        self.state.crash_per_index_allotment_usd = self._last_budget["per_index_usd"]
        self.state.crash_last_triggers = dict(self._last_triggers)
        self.state.crash_last_tick_at = now
        self.state.crash_mark_source = getattr(self.market, "mark_source", "coinbase_perp")

    def _refresh_budget(self, marks: dict[str, float]) -> None:
        settings = self.state.settings
        store = self.state.crash_ledger
        assert store is not None
        n = max(1, len(self.product_list()))
        account_value = 0.0
        if settings.crash_live_orders_permitted():
            try:
                crypto_marks = {}
                try:
                    crypto_marks = self.state.marks()
                except Exception:  # noqa: BLE001
                    crypto_marks = {}
                # Prefer futures AV cache if fresher
                ft_av = getattr(self.state, "futures_account_value_usd", None)
                if ft_av is not None and float(ft_av) > 0:
                    account_value = float(ft_av)
                else:
                    account_value = float(
                        self.market.fetch_account_value_usd(crypto_marks=crypto_marks)
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "crash account value fetch failed; falling back to store equity",
                    extra={"data": {"error": str(exc)}},
                )
                account_value = float(store.equity_usd(marks))
        else:
            account_value = float(store.equity_usd(marks))
            if account_value <= 0:
                account_value = float(settings.crash_bankroll_usd)

        pct = float(settings.crash_account_budget_pct)
        budget = max(0.0, account_value * pct)
        per_index = budget / float(n)
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

    def _maybe_log_btc_twin(self) -> None:
        """Optional BTC −5% 24h twin — log only, no BTC short required in v1."""
        try:
            t = self.market.fetch_ticker("BTC-PERP-INTX")
            # Prefer percentage from ticker if exchange provides; else skip
            change = None
            raw = getattr(self.market, "_exchange", None)
            if raw is not None:
                try:
                    from snowball.crash.market import to_futures_ccxt_symbol

                    sym = to_futures_ccxt_symbol("BTC-PERP-INTX")
                    info = raw.fetch_ticker(sym)  # type: ignore[attr-defined]
                    pct = info.get("percentage")
                    if pct is not None:
                        change = float(pct) / 100.0
                    elif info.get("open") and info.get("last"):
                        op = float(info["open"])
                        if op > 0:
                            change = (float(info["last"]) - op) / op
                except Exception:  # noqa: BLE001
                    change = None
            msg = btc_24h_twin_log(change)
            if msg:
                log.info("crash btc twin (log-only)", extra={"data": {"msg": msg, "ticker_last": t.last}})
                self._last_triggers["BTC-TWIN"] = {"fire": True, "reasons": [msg], "change_24h": change}
        except Exception:  # noqa: BLE001
            # BTC twin is best-effort; product may not exist on this venue path
            pass

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.crash_pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.crash_max_positions
        )
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("crash ticker failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)

        # Daily OHLCV for triggers
        try:
            limit = max(settings.ohlcv_fetch_limit, 40)
            rows = self.market.fetch_ohlcv(product, "1d", limit)
            closes = [float(r[4]) for r in rows if len(r) >= 5]
            if closes:
                tr = evaluate_crash_triggers(
                    closes,
                    rsi_period=settings.rsi_period,
                    bb_period=settings.bb_period,
                    bb_std_mult=settings.bb_std_mult,
                )
                snap.rsi_1d = tr.rsi_1d
                snap.bb_lower_1d = tr.bb_lower
                if tr.bb_width is not None and tr.bb_lower is not None:
                    # mid ≈ lower + width/2; upper = lower + width
                    mid = tr.bb_lower + (tr.bb_width / 2.0)
                    snap.bb_mid_1d = mid
                    snap.bb_upper_1d = tr.bb_lower + tr.bb_width
                self._last_triggers[product] = {
                    "fire": tr.fire,
                    "reasons": list(tr.reasons),
                    "daily_return": tr.daily_return,
                    "rsi_1d": tr.rsi_1d,
                    "bb_width": tr.bb_width,
                    "bb_width_prior": tr.bb_width_prior,
                }
                # candle ts from last bar
                if rows:
                    ts_ms = float(rows[-1][0])
                    snap.candle_ts_1d = datetime.fromtimestamp(
                        ts_ms / 1000.0, tz=__import__("datetime").timezone.utc
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("crash ohlcv/triggers failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)

        store = self.state.crash_ledger
        if store is not None:
            snap.open_count = store.open_count(product)
        snap.max_open = settings.crash_max_positions
        self.state.crash_pairs[product] = snap
        return snap

    def _act_on_product(
        self,
        *,
        product: str,
        now: datetime,
        halted: bool,
        can_trade: bool,
        daily_killed: bool,
        marks: dict[str, float],
    ) -> None:
        settings = self.state.settings
        store = self.state.crash_ledger
        assert store is not None
        lots = store.open_positions(product)
        mark = marks.get(product)

        # Manage open short: cover only if green enough
        if lots:
            self._maybe_cover(product, lots, marks, now)
            return  # max 1 lot — no pyramiding / second entry while open

        if halted or not can_trade or daily_killed:
            return

        tr_info = self._last_triggers.get(product) or {}
        if not tr_info.get("fire"):
            return

        # Flat + trigger → open short
        allot = float(self._last_budget.get("per_index_usd") or 0.0)
        if allot <= 1.0:
            log.info(
                "crash entry skipped: allotment too small",
                extra={"data": {"product": product, "allot": allot}},
            )
            return
        reasons = tr_info.get("reasons") or []
        reason = f"crash_guard:trigger:{','.join(reasons)}"
        self._open_short(
            product=product,
            marks=marks,
            reason=reason,
            now=now,
            notional_usd=allot,
        )

    def _maybe_cover(
        self,
        product: str,
        lots: list[Position],
        marks: dict[str, float],
        now: datetime,
    ) -> None:
        settings = self.state.settings
        mark = marks.get(product)
        for lot in lots:
            ok, why = short_cover_allowed(
                lot.entry_price,
                mark,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_cover_red=True,
            )
            if not ok:
                log.info(
                    "crash cover refused",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "reason": why,
                            "mark": mark,
                            "entry": lot.entry_price,
                            "short_pnl_pct": short_unrealized_pnl_pct(lot.entry_price, mark),
                        }
                    },
                )
                continue
            self._cover_short(product, lot, marks, reason="crash_guard:take_profit", now=now)

    def _open_short(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        notional_usd: float,
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        store = self.state.crash_ledger
        assert store is not None
        if store.open_count(product) >= settings.crash_max_positions:
            log.info("crash open blocked: max positions", extra={"data": {"product": product}})
            return
        if settings.crash_live_orders_permitted():
            self._open_short_live(product, reason=reason, now=now, notional_usd=notional_usd)
            return
        # Paper path (tests / paper mode)
        snap = self.state.crash_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        # Maker-limit short ≈ sell at ask (post-only simulation via fill_price sell)
        paper_px = fill_price(ticker, "sell", settings.slippage_bps)
        if paper_px is None or paper_px <= 0:
            log.warning("crash paper short skipped: no mark", extra={"data": {"product": product}})
            return
        if is_cfm_product(product):
            contracts = order_size_for_product(
                product,
                notional_usd=notional_usd,
                price=float(paper_px),
                available_margin_usd=max(float(store.cash_usd()), float(notional_usd)),
                max_contracts=settings.cfm_max_contracts_per_index(),
                leverage=settings.cfm_order_leverage(),
                margin_rate=float(settings.cfm_margin_rate),
            )
            if contracts < 1:
                log.info(
                    "crash paper short skipped: cannot fund 1 CFM contract",
                    extra={"data": {"product": product, "notional": notional_usd, "px": paper_px}},
                )
                return
            notional_usd = float(contracts) * float(paper_px)
            if store.cash_usd() + 1e-9 < notional_usd:
                try:
                    store.set_cash_usd(max(store.cash_usd(), notional_usd * 2), ts=now)
                except Exception:  # noqa: BLE001
                    log.exception("crash paper cash top-up failed")
        try:
            pos, fill = store.open_short(
                product=product,
                fill_px=paper_px,
                notional_usd=notional_usd,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
                strategy=CRASH_STRATEGY,
            )
        except ValueError as exc:
            log.info(
                "crash paper short refused",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        self.state.crash_pairs[product].open_count = store.open_count(product)
        log.warning(
            "crash PAPER short",
            extra={
                "data": {
                    "product": product,
                    "qty": pos.qty,
                    "price": fill.price,
                    "notional": fill.notional_usd,
                    "reason": reason,
                    "position_id": pos.id,
                }
            },
        )

    def _open_short_live(
        self,
        product: str,
        *,
        reason: str,
        now: datetime,
        notional_usd: float,
    ) -> None:
        settings = self.state.settings
        if not settings.crash_live_orders_permitted():
            raise LiveTradingRefused("crash live short refused: dual gate not set")
        store = self.state.crash_ledger
        assert store is not None
        snap = self.state.crash_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        ref = ticker.reference or ticker.last
        if ref is None or ref <= 0:
            log.warning("crash live short skipped: no mark", extra={"data": {"product": product}})
            return
        lev = settings.cfm_order_leverage() if is_cfm_product(product) else 1.0
        max_c = settings.cfm_max_contracts_per_index()
        margin_rate = float(getattr(settings, "cfm_margin_rate", 0.10))
        avail = max(float(store.cash_usd()), float(notional_usd))
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
                "crash live short skipped: amount below min",
                extra={"data": {"product": product, "notional": notional_usd, "ref": ref, "amount": amount}},
            )
            return
        bid = ticker.bid
        ask = ticker.ask
        try:
            from snowball.maker import maker_sell_price

            limit_px = maker_sell_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                bid2, ask2 = self.market.fetch_bba(product)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_sell_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "crash live short skipped: no book ask for maker sell",
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
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
                "sell",
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
                "crash live short failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "crash live short unfilled/canceled maker limit",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        notional = fill_qty * fill_px
        margin = notional + fee_usd
        if store.cash_usd() + 1e-9 < margin:
            try:
                store.set_cash_usd(margin * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("crash live ledger cash sync failed")
        pos, fill = store.open_short(
            product=product,
            fill_px=fill_px,
            notional_usd=notional,
            slippage_bps=0.0,
            fee_usd=fee_usd,
            reason=reason,
            ts=now,
            strategy=CRASH_STRATEGY,
        )
        self.state.crash_pairs[product].open_count = store.open_count(product)
        log.warning(
            "crash LIVE short",
            extra={
                "data": {
                    "product": product,
                    "qty": fill_qty,
                    "price": fill_px,
                    "notional": fill.notional_usd,
                    "fee": fee_usd,
                    "reason": reason,
                    "position_id": pos.id,
                    "order_id": order.get("id"),
                }
            },
        )

    def _cover_short(
        self,
        product: str,
        lot: Position,
        marks: dict[str, float],
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        store = self.state.crash_ledger
        assert store is not None
        # Re-check never cover red at fill time
        mark = marks.get(product)
        ok, why = short_cover_allowed(
            lot.entry_price,
            mark,
            min_take_profit_pct=settings.min_take_profit_pct,
            fee_buffer_pct=settings.fee_buffer_pct,
            never_cover_red=True,
        )
        if not ok:
            log.info(
                "crash cover aborted",
                extra={"data": {"product": product, "why": why}},
            )
            return
        if settings.crash_live_orders_permitted():
            self._cover_short_live(product, lot, reason=reason, now=now)
            return
        snap = self.state.crash_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        # Cover = buy to close; paper fill on buy side
        paper_px = fill_price(ticker, "buy", settings.slippage_bps)
        if paper_px is None or paper_px <= 0:
            return
        ok2, why2 = short_cover_allowed(
            lot.entry_price,
            paper_px,
            min_take_profit_pct=settings.min_take_profit_pct,
            fee_buffer_pct=settings.fee_buffer_pct,
            never_cover_red=True,
        )
        if not ok2:
            log.info(
                "crash paper cover refused at fill",
                extra={"data": {"product": product, "why": why2, "px": paper_px}},
            )
            return
        fill = store.cover_short(
            lot.id,
            fill_px=paper_px,
            slippage_bps=settings.slippage_bps,
            fee_usd=0.0,
            reason=reason,
            ts=now,
        )
        self.state.crash_pairs[product].open_count = store.open_count(product)
        log.warning(
            "crash PAPER cover",
            extra={
                "data": {
                    "product": product,
                    "position_id": lot.id,
                    "price": fill.price,
                    "reason": reason,
                }
            },
        )

    def _cover_short_live(
        self,
        product: str,
        lot: Position,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        if not settings.crash_live_orders_permitted():
            raise LiveTradingRefused("crash live cover refused: dual gate not set")
        store = self.state.crash_ledger
        assert store is not None
        snap = self.state.crash_pairs[product]
        ticker = Ticker(
            product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now
        )
        if is_cfm_product(product):
            amount = float(max(1, int(round(float(lot.qty)))))
        else:
            amount = round_amount_down(float(lot.qty), 0.01)
            if amount < 0.01:
                return
        lev = settings.cfm_order_leverage() if is_cfm_product(product) else 1.0
        bid = ticker.bid
        ask = ticker.ask
        try:
            from snowball.maker import maker_buy_price

            limit_px = maker_buy_price(bid, ask, ticker.last)
            if limit_px is None or limit_px <= 0:
                bid2, ask2 = self.market.fetch_bba(product)
                bid = bid if bid is not None else bid2
                ask = ask if ask is not None else ask2
                limit_px = maker_buy_price(bid, ask, ticker.last)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "crash live cover skipped: no book bid for maker buy",
                    extra={"data": {"product": product}},
                )
                return
            # Final green check at intended cover price
            ok, why = short_cover_allowed(
                lot.entry_price,
                limit_px,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_cover_red=True,
            )
            if not ok:
                log.info(
                    "crash live cover refused at limit",
                    extra={"data": {"product": product, "why": why, "limit_px": limit_px}},
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
                reduce_only=True,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "crash live cover failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "crash live cover unfilled/canceled",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        ok3, why3 = short_cover_allowed(
            lot.entry_price,
            fill_px,
            min_take_profit_pct=settings.min_take_profit_pct,
            fee_buffer_pct=settings.fee_buffer_pct,
            never_cover_red=True,
        )
        if not ok3:
            # Should be rare (fill worse than limit); leave position accounting alone —
            # but order already filled. Record cover anyway with warning; never leave
            # exchange flat while ledger open.
            log.error(
                "crash live cover fill below green floor — recording anyway",
                extra={"data": {"product": product, "why": why3, "fill_px": fill_px}},
            )
        fill = store.cover_short(
            lot.id,
            fill_px=fill_px,
            slippage_bps=0.0,
            fee_usd=fee_usd,
            reason=reason,
            ts=now,
        )
        self.state.crash_pairs[product].open_count = store.open_count(product)
        log.warning(
            "crash LIVE cover",
            extra={
                "data": {
                    "product": product,
                    "position_id": lot.id,
                    "price": fill.price,
                    "fee": fee_usd,
                    "reason": reason,
                    "order_id": order.get("id"),
                }
            },
        )

    def run_forever(self) -> None:
        settings = self.state.settings
        while self.state.running:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("crash tick failed")
            time.sleep(max(1.0, float(settings.crash_poll_seconds)))


CrashPaperEngine = CrashEngine  # tests alias


def attach_crash_lane(state: AppState) -> CrashEngine | None:
    """Create isolated crash ledger + engine if CRASH_ENABLED."""
    settings = state.settings
    if not settings.crash_enabled:
        log.info("Crash Guard lane disabled")
        return None
    settings.assert_crash_config()
    store = CrashStore(settings.crash_sqlite_path, settings.crash_bankroll_usd)
    state.crash_ledger = store
    state.crash_mark_source = "coinbase_perp"
    state.crash_account_value_usd = None
    state.crash_budget_usd = None
    state.crash_per_index_allotment_usd = None
    state.crash_last_triggers = {}
    for product in settings.crash_product_list or list(DEFAULT_CRASH_PRODUCTS):
        pid = normalize_futures_product(product)
        state.crash_pairs[pid] = PairSnapshot(
            product=pid, max_open=settings.crash_max_positions
        )
    engine = CrashEngine(state)
    state.crash_engine = engine
    live = settings.crash_live_orders_permitted()
    log.warning(
        "Crash Guard lane attached",
        extra={
            "data": {
                "sqlite": str(settings.crash_sqlite_path),
                "bankroll": settings.crash_bankroll_usd,
                "products": settings.crash_product_list,
                "crash_mode": settings.crash_mode,
                "crash_live_enabled": settings.crash_live_enabled,
                "live_orders": live,
                "budget_pct": settings.crash_account_budget_pct,
            }
        },
    )
    return engine
