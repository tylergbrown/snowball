"""Fed Desk engine — FedWatch research + dual-gated SPY/QQQ directional bets."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from snowball.allocation import effective_take_profit_floor
from snowball.config import LiveTradingRefused, Settings
from snowball.crash.triggers import short_cover_allowed, short_unrealized_pnl_pct
from snowball.fed.market import (
    DEFAULT_FED_PRODUCTS,
    FedMarket,
    normalize_futures_product,
    parse_futures_order_fill,
    round_amount_down,
)
from snowball.fed.probs import (
    BET_SKEW_THRESHOLD,
    classify_meeting_probs,
    history_deltas,
    in_fomc_bet_window,
    prefer_exit_after_fomc,
    summarize_fedwatch_payload,
)
from snowball.fed.store import FedStore
from snowball.gates import strategy_exit_allowed
from snowball.halt import halt_active, trading_enabled
from snowball.market import fill_price
from snowball.models import PairSnapshot, Position, Ticker, utcnow
from snowball.risk import daily_loss_breached
from snowball.state import AppState

log = logging.getLogger("snowball.fed.engine")

FED_STRATEGY = "fed_desk"


def _refuse_unless_paper_or_dual_live(settings: Settings) -> None:
    if settings.fed_mode == "paper":
        if settings.fed_live_enabled:
            raise LiveTradingRefused(
                "Fed Desk refused: FED_LIVE_ENABLED=true while "
                "FED_MODE is not live (dual gate required)."
            )
        return
    if not settings.fed_live_orders_permitted():
        raise LiveTradingRefused(
            f"Fed Desk refused: FED_MODE={settings.fed_mode!r} "
            f"FED_LIVE_ENABLED={settings.fed_live_enabled!r} "
            "(both must be true for live Fed Desk orders)."
        )


def long_exit_allowed(
    entry_price: float,
    mark: float | None,
    *,
    min_take_profit_pct: float = 0.06,
    fee_buffer_pct: float = 0.01,
    never_sell_red: bool = True,
) -> tuple[bool, str]:
    if mark is None or mark <= 0 or entry_price <= 0:
        return False, "no_mark"
    pnl = (float(mark) - float(entry_price)) / float(entry_price)
    if never_sell_red and pnl < 0:
        return False, "never_sell_red"
    floor = effective_take_profit_floor(min_take_profit_pct, fee_buffer_pct)
    if pnl < floor:
        return False, "below_take_profit"
    return True, "ok"


class FedEngine:
    """Research always-on; educated bets only in FOMC window with >=70% skew."""

    def __init__(self, state: AppState, market: FedMarket | None = None) -> None:
        self.state = state
        self._last_budget: dict[str, float] = {
            "account_value_usd": 0.0,
            "budget_usd": 0.0,
            "per_index_usd": 0.0,
        }
        self._last_research: dict[str, Any] = {}
        self._last_research_poll_mono: float = 0.0
        self._bet_status: str = "idle"
        if market is not None:
            self.market = market
        else:
            s = state.settings
            allow = s.fed_live_orders_permitted()
            self.market = FedMarket(
                api_key=s.coinbase_api_key,
                api_secret=s.coinbase_api_secret,
                api_passphrase=s.coinbase_api_passphrase,
                allow_orders=allow,
            )

    def product_list(self) -> list[str]:
        settings = self.state.settings
        items = [normalize_futures_product(p) for p in settings.fed_product_list]
        return items or list(DEFAULT_FED_PRODUCTS)

    def tick(self) -> None:
        settings = self.state.settings
        if not settings.fed_enabled:
            return
        try:
            _refuse_unless_paper_or_dual_live(settings)
        except LiveTradingRefused as exc:
            log.error(str(exc))
            return
        store = self.state.fed_ledger
        if store is None:
            return

        self._maybe_poll_research()

        products = self.product_list()
        for product in products:
            if product not in self.state.fed_pairs:
                self.state.fed_pairs[product] = PairSnapshot(
                    product=product, max_open=settings.fed_max_positions
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

        equity = store.equity_usd(marks)
        utc_date, start_eq, killed = store.ensure_utc_day(now, equity)
        if not killed and daily_loss_breached(
            equity, start_eq, settings.fed_daily_loss_kill_usd
        ):
            log.warning(
                "fed daily loss kill — block new entries; hold open lots",
                extra={
                    "data": {
                        "equity": equity,
                        "start": start_eq,
                        "kill": settings.fed_daily_loss_kill_usd,
                    }
                },
            )
            store.set_daily_killed(utc_date)
            killed = True

        if halted:
            log.info("fed halt active — no new bets; existing lots held")

        research = self._last_research or store.last_research() or {}
        decision = self._bet_decision(research)
        self._bet_status = decision.get("status") or "idle"

        for product in products:
            self._act_on_product(
                product=product,
                now=now,
                halted=halted,
                can_trade=can_trade,
                daily_killed=killed,
                marks=marks,
                decision=decision,
            )

        self.state.fed_account_value_usd = self._last_budget["account_value_usd"]
        self.state.fed_budget_usd = self._last_budget["budget_usd"]
        self.state.fed_per_index_allotment_usd = self._last_budget["per_index_usd"]
        self.state.fed_last_research = dict(self._last_research or research or {})
        self.state.fed_bet_status = self._bet_status
        self.state.fed_last_tick_at = now
        self.state.fed_mark_source = getattr(self.market, "mark_source", "coinbase_perp")

    def _bet_decision(self, research: dict[str, Any]) -> dict[str, Any]:
        """Compute whether to enter and which direction from latest research."""
        if not research:
            return {"status": "no_research", "bet_eligible": False, "direction": None}
        next_date = research.get("next_meeting_date") or (
            (research.get("next_meeting") or {}).get("date")
        )
        in_window = bool(research.get("in_window"))
        if next_date and not in_window:
            in_window = in_fomc_bet_window(str(next_date))
        p_hold = float(research.get("p_hold") or 0.0)
        p_hike = float(research.get("p_hike") or 0.0)
        p_cut = float(research.get("p_cut") or 0.0)
        current_target = str(research.get("current_target") or "")
        # Recompute for safety
        probs = {}
        nm = research.get("next_meeting") or {}
        if nm.get("probabilities"):
            probs = nm["probabilities"]
        elif research.get("raw_next_probabilities"):
            probs = research["raw_next_probabilities"]
        if probs and current_target:
            summary = classify_meeting_probs(probs, current_target)
            p_hold, p_hike, p_cut = summary.p_hold, summary.p_hike, summary.p_cut
            dominant = summary.dominant
            max_prob = summary.max_prob
            direction = summary.direction
            eligible_skew = summary.bet_eligible
        else:
            ranked = sorted(
                (("hold", p_hold), ("hike", p_hike), ("cut", p_cut)),
                key=lambda x: -x[1],
            )
            dominant = ranked[0][0]
            max_prob = ranked[0][1]
            eligible_skew = max_prob >= BET_SKEW_THRESHOLD and dominant in ("hike", "cut")
            direction = None
            if eligible_skew:
                direction = "long" if dominant == "cut" else "short"
            if dominant == "hold":
                eligible_skew = False
                direction = None

        bet_eligible = bool(eligible_skew and in_window)
        status = "idle"
        if not in_window:
            status = "outside_window"
        elif dominant == "hold":
            status = "hold_flat"
        elif max_prob < BET_SKEW_THRESHOLD:
            status = "skew_below_threshold"
        elif bet_eligible:
            status = f"bet_{direction}"
        return {
            "status": status,
            "bet_eligible": bet_eligible,
            "direction": direction if bet_eligible else None,
            "dominant": dominant,
            "max_prob": max_prob,
            "p_hold": p_hold,
            "p_hike": p_hike,
            "p_cut": p_cut,
            "in_window": in_window,
            "meeting_date": next_date,
            "threshold": BET_SKEW_THRESHOLD,
        }

    def _maybe_poll_research(self) -> None:
        settings = self.state.settings
        interval = float(getattr(settings, "fed_research_poll_seconds", 10800.0) or 10800.0)
        now_m = time.monotonic()
        if self._last_research and (now_m - self._last_research_poll_mono) < interval:
            return
        try:
            payload = self._fetch_fedwatch()
            summarized = summarize_fedwatch_payload(payload)
            # Attach history deltas for next meeting
            try:
                from cme_fedwatch import get_history

                hist = get_history()
                lookback = (hist or {}).get("lookback") or []
                prior = None
                if lookback:
                    prior = (lookback[0] or {}).get("probabilities")
                nm = summarized.get("next_meeting") or {}
                deltas = history_deltas(
                    nm.get("probabilities") or {},
                    prior,
                    str(summarized.get("current_target") or ""),
                )
                summarized["history_deltas"] = deltas
                summarized["history_lookback"] = lookback
                summarized["raw_next_probabilities"] = nm.get("probabilities") or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("fed history fetch failed: %s", exc)
                summarized["history_deltas"] = {}

            # Tag FOMC/SEP headlines from Watcher into research payload
            summarized["watcher_fomc_headlines"] = self._watcher_fomc_headlines()

            store = self.state.fed_ledger
            if store is not None:
                store.save_research_snapshot(
                    summarized,
                    meeting_date=summarized.get("next_meeting_date"),
                )
            self._last_research = summarized
            self._last_research_poll_mono = now_m
            self.state.fed_last_research = dict(summarized)
            log.info(
                "fed research polled",
                extra={
                    "data": {
                        "next": summarized.get("next_meeting_date"),
                        "p_hold": summarized.get("p_hold"),
                        "p_hike": summarized.get("p_hike"),
                        "p_cut": summarized.get("p_cut"),
                        "bet_eligible": summarized.get("bet_eligible"),
                        "in_window": summarized.get("in_window"),
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("fed research poll failed", extra={"data": {"error": str(exc)}})
            store = self.state.fed_ledger
            if store is not None:
                store.set_meta("last_research_error", str(exc))

    def _fetch_fedwatch(self) -> dict[str, Any]:
        from cme_fedwatch import get_probabilities

        return get_probabilities()

    def _watcher_fomc_headlines(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        watcher = getattr(self.state, "watcher", None)
        if watcher is None:
            return out
        try:
            from snowball.watcher.store import event_to_dict

            for ev in watcher.latest_press(40):
                tags = set(getattr(ev, "tags", None) or [])
                title = (getattr(ev, "title", None) or "").lower()
                if tags & {"fomc", "rate_decision"} or "fomc" in title or "sep" in title or "dot plot" in title:
                    d = event_to_dict(ev)
                    d["fed_tags"] = sorted(tags & {"fomc", "rate_decision"} or {"headline"})
                    out.append(d)
                    if len(out) >= 15:
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("fed watcher headline tag failed: %s", exc)
        return out

    def _refresh_budget(self, marks: dict[str, float]) -> None:
        settings = self.state.settings
        store = self.state.fed_ledger
        assert store is not None
        n = max(1, len(self.product_list()))
        account_value = 0.0
        if settings.fed_live_orders_permitted():
            try:
                ft_av = getattr(self.state, "futures_account_value_usd", None)
                cg_av = getattr(self.state, "crash_account_value_usd", None)
                if ft_av is not None and float(ft_av) > 0:
                    account_value = float(ft_av)
                elif cg_av is not None and float(cg_av) > 0:
                    account_value = float(cg_av)
                else:
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
                    "fed account value fetch failed; falling back to store equity",
                    extra={"data": {"error": str(exc)}},
                )
                account_value = float(store.equity_usd(marks))
        else:
            account_value = float(store.equity_usd(marks))
            if account_value <= 0:
                account_value = float(settings.fed_bankroll_usd)

        pct = float(settings.fed_account_budget_pct)
        budget = max(0.0, account_value * pct)
        per_index = budget / float(n)
        per_index = min(per_index, float(settings.fed_max_notional_usd))
        self._last_budget = {
            "account_value_usd": account_value,
            "budget_usd": budget,
            "per_index_usd": per_index,
        }

    def _update_pair(self, product: str) -> PairSnapshot:
        settings = self.state.settings
        snap = self.state.fed_pairs.get(product) or PairSnapshot(
            product=product, max_open=settings.fed_max_positions
        )
        try:
            ticker = self.market.fetch_ticker(product)
            snap.last = ticker.last if ticker.last is not None else ticker.reference
            snap.bid = ticker.bid
            snap.ask = ticker.ask
            snap.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("fed ticker failed", extra={"data": {"product": product}})
            snap.last_error = str(exc)
        store = self.state.fed_ledger
        if store is not None:
            snap.open_count = store.open_count(product)
        snap.max_open = settings.fed_max_positions
        self.state.fed_pairs[product] = snap
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
        decision: dict[str, Any],
    ) -> None:
        settings = self.state.settings
        store = self.state.fed_ledger
        assert store is not None
        lots = store.open_positions(product)

        if lots:
            self._maybe_exit(product, lots, marks, now, decision)
            return

        if halted or not can_trade or daily_killed:
            return
        if not decision.get("bet_eligible"):
            return
        direction = decision.get("direction")
        if direction not in ("long", "short"):
            return

        allot = float(self._last_budget.get("per_index_usd") or 0.0)
        if allot <= 1.0:
            log.info(
                "fed entry skipped: allotment too small",
                extra={"data": {"product": product, "allot": allot}},
            )
            return
        reason = (
            f"fed_desk:{direction}:dominant={decision.get('dominant')}:"
            f"max={float(decision.get('max_prob') or 0):.2f}:"
            f"meeting={decision.get('meeting_date')}"
        )
        if direction == "long":
            self._open_long(
                product=product,
                marks=marks,
                reason=reason,
                now=now,
                notional_usd=allot,
                meeting_date=str(decision.get("meeting_date") or ""),
                dominant=str(decision.get("dominant") or ""),
            )
        else:
            self._open_short(
                product=product,
                marks=marks,
                reason=reason,
                now=now,
                notional_usd=allot,
                meeting_date=str(decision.get("meeting_date") or ""),
                dominant=str(decision.get("dominant") or ""),
            )

    def _maybe_exit(
        self,
        product: str,
        lots: list[Position],
        marks: dict[str, float],
        now: datetime,
        decision: dict[str, Any],
    ) -> None:
        settings = self.state.settings
        mark = marks.get(product)
        meeting = str(decision.get("meeting_date") or "")
        after = prefer_exit_after_fomc(meeting) if meeting else False
        for lot in lots:
            if lot.side == "short":
                ok, why = short_cover_allowed(
                    lot.entry_price,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    fee_buffer_pct=settings.fee_buffer_pct,
                    never_cover_red=True,
                )
            else:
                ok, why = long_exit_allowed(
                    lot.entry_price,
                    mark,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    fee_buffer_pct=settings.fee_buffer_pct,
                    never_sell_red=True,
                )
            if not ok:
                log.info(
                    "fed exit refused",
                    extra={
                        "data": {
                            "product": product,
                            "position_id": lot.id,
                            "side": lot.side,
                            "reason": why,
                            "mark": mark,
                            "after_fomc": after,
                        }
                    },
                )
                continue
            # Prefer exiting the day after FOMC when green; still allow green exits anytime
            reason = "fed_desk:take_profit"
            if after:
                reason = "fed_desk:post_fomc_green_exit"
            self._close_lot(product, lot, marks, reason=reason, now=now)

    def _open_long(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        notional_usd: float,
        meeting_date: str,
        dominant: str,
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        store = self.state.fed_ledger
        assert store is not None
        if store.open_count(product) >= settings.fed_max_positions:
            return
        if settings.fed_live_orders_permitted():
            self._open_side_live(
                product, side="buy", reason=reason, now=now, notional_usd=notional_usd,
                meeting_date=meeting_date, dominant=dominant, direction="long",
            )
            return
        snap = self.state.fed_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        paper_px = fill_price(ticker, "buy", settings.slippage_bps)
        if paper_px is None or paper_px <= 0:
            return
        try:
            pos, fill = store.open_long(
                product=product,
                fill_px=paper_px,
                notional_usd=notional_usd,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
                strategy=FED_STRATEGY,
                meeting_date=meeting_date,
                dominant=dominant,
                direction="long",
            )
        except ValueError as exc:
            log.info("fed paper long refused", extra={"data": {"error": str(exc)}})
            return
        self.state.fed_pairs[product].open_count = store.open_count(product)
        log.warning(
            "fed PAPER long",
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

    def _open_short(
        self,
        product: str,
        marks: dict[str, float],
        reason: str,
        now: datetime,
        notional_usd: float,
        meeting_date: str,
        dominant: str,
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        store = self.state.fed_ledger
        assert store is not None
        if store.open_count(product) >= settings.fed_max_positions:
            return
        if settings.fed_live_orders_permitted():
            self._open_side_live(
                product, side="sell", reason=reason, now=now, notional_usd=notional_usd,
                meeting_date=meeting_date, dominant=dominant, direction="short",
            )
            return
        snap = self.state.fed_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        paper_px = fill_price(ticker, "sell", settings.slippage_bps)
        if paper_px is None or paper_px <= 0:
            return
        try:
            pos, fill = store.open_short(
                product=product,
                fill_px=paper_px,
                notional_usd=notional_usd,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
                strategy=FED_STRATEGY,
                meeting_date=meeting_date,
                dominant=dominant,
                direction="short",
            )
        except ValueError as exc:
            log.info("fed paper short refused", extra={"data": {"error": str(exc)}})
            return
        self.state.fed_pairs[product].open_count = store.open_count(product)
        log.warning(
            "fed PAPER short",
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

    def _open_side_live(
        self,
        product: str,
        *,
        side: str,
        reason: str,
        now: datetime,
        notional_usd: float,
        meeting_date: str,
        dominant: str,
        direction: str,
    ) -> None:
        settings = self.state.settings
        if not settings.fed_live_orders_permitted():
            raise LiveTradingRefused("fed live entry refused: dual gate not set")
        store = self.state.fed_ledger
        assert store is not None
        snap = self.state.fed_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        ref = ticker.reference or ticker.last
        if ref is None or ref <= 0:
            return
        bid = ticker.bid
        ask = ticker.ask
        try:
            if side == "buy":
                from snowball.maker import maker_buy_price

                limit_px = maker_buy_price(bid, ask, ref)
                if limit_px is None or limit_px <= 0:
                    bid2, ask2 = self.market.fetch_bba(product)
                    bid = bid if bid is not None else bid2
                    ask = ask if ask is not None else ask2
                    limit_px = maker_buy_price(bid, ask, ref)
            else:
                from snowball.maker import maker_sell_price

                limit_px = maker_sell_price(bid, ask, ref)
                if limit_px is None or limit_px <= 0:
                    bid2, ask2 = self.market.fetch_bba(product)
                    bid = bid if bid is not None else bid2
                    ask = ask if ask is not None else ask2
                    limit_px = maker_sell_price(bid, ask, ref)
            if limit_px is None or limit_px <= 0:
                log.info(
                    "fed live entry skipped: no maker price",
                    extra={"data": {"product": product, "side": side}},
                )
                return
            amount = round_amount_down(notional_usd / float(limit_px), 0.01)
            if amount < 0.01:
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
                side,
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
                "fed live entry failed",
                extra={"data": {"product": product, "side": side, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "fed live entry unfilled/canceled",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        notional = fill_qty * fill_px
        need = notional + fee_usd
        if store.cash_usd() + 1e-9 < need:
            try:
                store.set_cash_usd(need * 2, ts=now)
            except Exception:  # noqa: BLE001
                log.exception("fed live ledger cash sync failed")
        if direction == "long":
            pos, fill = store.open_long(
                product=product,
                fill_px=fill_px,
                notional_usd=notional,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
                strategy=FED_STRATEGY,
                meeting_date=meeting_date,
                dominant=dominant,
                direction="long",
            )
        else:
            pos, fill = store.open_short(
                product=product,
                fill_px=fill_px,
                notional_usd=notional,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
                strategy=FED_STRATEGY,
                meeting_date=meeting_date,
                dominant=dominant,
                direction="short",
            )
        self.state.fed_pairs[product].open_count = store.open_count(product)
        log.warning(
            "fed LIVE entry",
            extra={
                "data": {
                    "product": product,
                    "direction": direction,
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

    def _close_lot(
        self,
        product: str,
        lot: Position,
        marks: dict[str, float],
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        _refuse_unless_paper_or_dual_live(settings)
        store = self.state.fed_ledger
        assert store is not None
        mark = marks.get(product)
        if lot.side == "short":
            ok, why = short_cover_allowed(
                lot.entry_price,
                mark,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_cover_red=True,
            )
        else:
            ok, why = long_exit_allowed(
                lot.entry_price,
                mark,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_sell_red=True,
            )
        if not ok:
            return
        if settings.fed_live_orders_permitted():
            self._close_lot_live(product, lot, reason=reason, now=now)
            return
        snap = self.state.fed_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        side = "buy" if lot.side == "short" else "sell"
        paper_px = fill_price(ticker, side, settings.slippage_bps)
        if paper_px is None or paper_px <= 0:
            return
        if lot.side == "short":
            ok2, why2 = short_cover_allowed(
                lot.entry_price,
                paper_px,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_cover_red=True,
            )
            if not ok2:
                return
            fill = store.cover_short(
                lot.id,
                fill_px=paper_px,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
            )
        else:
            ok2, why2 = long_exit_allowed(
                lot.entry_price,
                paper_px,
                min_take_profit_pct=settings.min_take_profit_pct,
                fee_buffer_pct=settings.fee_buffer_pct,
                never_sell_red=True,
            )
            if not ok2:
                return
            fill = store.close_long(
                lot.id,
                fill_px=paper_px,
                slippage_bps=settings.slippage_bps,
                fee_usd=0.0,
                reason=reason,
                ts=now,
            )
        self.state.fed_pairs[product].open_count = store.open_count(product)
        log.warning(
            "fed PAPER exit",
            extra={
                "data": {
                    "product": product,
                    "position_id": lot.id,
                    "side": lot.side,
                    "price": fill.price,
                    "reason": reason,
                }
            },
        )

    def _close_lot_live(
        self,
        product: str,
        lot: Position,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        settings = self.state.settings
        if not settings.fed_live_orders_permitted():
            raise LiveTradingRefused("fed live exit refused: dual gate not set")
        store = self.state.fed_ledger
        assert store is not None
        snap = self.state.fed_pairs[product]
        ticker = Ticker(product=product, last=snap.last, bid=snap.bid, ask=snap.ask, ts=now)
        amount = round_amount_down(float(lot.qty), 0.01)
        if amount < 0.01:
            return
        close_side = "buy" if lot.side == "short" else "sell"
        bid = ticker.bid
        ask = ticker.ask
        try:
            if close_side == "buy":
                from snowball.maker import maker_buy_price

                limit_px = maker_buy_price(bid, ask, ticker.last)
                if limit_px is None or limit_px <= 0:
                    bid2, ask2 = self.market.fetch_bba(product)
                    bid = bid if bid is not None else bid2
                    ask = ask if ask is not None else ask2
                    limit_px = maker_buy_price(bid, ask, ticker.last)
            else:
                from snowball.maker import maker_sell_price

                limit_px = maker_sell_price(bid, ask, ticker.last)
                if limit_px is None or limit_px <= 0:
                    bid2, ask2 = self.market.fetch_bba(product)
                    bid = bid if bid is not None else bid2
                    ask = ask if ask is not None else ask2
                    limit_px = maker_sell_price(bid, ask, ticker.last)
            if limit_px is None or limit_px <= 0:
                return
            if lot.side == "short":
                ok, why = short_cover_allowed(
                    lot.entry_price,
                    limit_px,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    fee_buffer_pct=settings.fee_buffer_pct,
                    never_cover_red=True,
                )
            else:
                ok, why = long_exit_allowed(
                    lot.entry_price,
                    limit_px,
                    min_take_profit_pct=settings.min_take_profit_pct,
                    fee_buffer_pct=settings.fee_buffer_pct,
                    never_sell_red=True,
                )
            if not ok:
                log.info(
                    "fed live exit refused at limit",
                    extra={"data": {"product": product, "why": why, "limit_px": limit_px}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
                close_side,
                amount,
                price=limit_px,
                bid=bid,
                ask=ask,
                leverage=1.0,
                reduce_only=True,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "fed live exit failed",
                extra={"data": {"product": product, "error": str(exc)}},
            )
            return
        fill_px, fill_qty, fee_usd = parse_futures_order_fill(order)
        if fill_px <= 0 or fill_qty <= 0:
            log.error(
                "fed live exit unfilled/canceled",
                extra={"data": {"product": product, "order_id": order.get("id")}},
            )
            return
        if lot.side == "short":
            fill = store.cover_short(
                lot.id,
                fill_px=fill_px,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
            )
        else:
            fill = store.close_long(
                lot.id,
                fill_px=fill_px,
                slippage_bps=0.0,
                fee_usd=fee_usd,
                reason=reason,
                ts=now,
            )
        self.state.fed_pairs[product].open_count = store.open_count(product)
        log.warning(
            "fed LIVE exit",
            extra={
                "data": {
                    "product": product,
                    "position_id": lot.id,
                    "side": lot.side,
                    "price": fill.price,
                    "fee": fee_usd,
                    "reason": reason,
                    "order_id": order.get("id"),
                }
            },
        )

    def run_forever(self) -> None:
        settings = self.state.settings
        # Force an immediate research poll on start
        self._last_research_poll_mono = 0.0
        while self.state.running:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("fed tick failed")
            time.sleep(max(1.0, float(settings.fed_poll_seconds)))


def attach_fed_lane(state: AppState) -> FedEngine | None:
    """Create isolated Fed Desk ledger + engine if FED_ENABLED."""
    settings = state.settings
    if not settings.fed_enabled:
        log.info("Fed Desk lane disabled")
        return None
    settings.assert_fed_config()
    store = FedStore(settings.fed_sqlite_path, settings.fed_bankroll_usd)
    state.fed_ledger = store
    state.fed_mark_source = "coinbase_perp"
    state.fed_account_value_usd = None
    state.fed_budget_usd = None
    state.fed_per_index_allotment_usd = None
    state.fed_last_research = {}
    state.fed_bet_status = "idle"
    for product in settings.fed_product_list or list(DEFAULT_FED_PRODUCTS):
        pid = normalize_futures_product(product)
        state.fed_pairs[pid] = PairSnapshot(
            product=pid, max_open=settings.fed_max_positions
        )
    engine = FedEngine(state)
    state.fed_engine = engine
    live = settings.fed_live_orders_permitted()
    log.warning(
        "Fed Desk lane attached",
        extra={
            "data": {
                "sqlite": str(settings.fed_sqlite_path),
                "bankroll": settings.fed_bankroll_usd,
                "products": settings.fed_product_list,
                "fed_mode": settings.fed_mode,
                "fed_live_enabled": settings.fed_live_enabled,
                "live_orders": live,
                "budget_pct": settings.fed_account_budget_pct,
            }
        },
    )
    return engine
