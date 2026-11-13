"""Fed Desk engine — FedWatch research + dual-gated SPY/QQQ directional bets."""

from __future__ import annotations

from snowball import signal_context

import logging
import time
from datetime import datetime, timezone
from typing import Any

from snowball.allocation import effective_take_profit_floor
from snowball.config import LiveTradingRefused, Settings
from snowball.crash.triggers import sma, short_cover_allowed, short_unrealized_pnl_pct
from snowball.fed.market import (
    DEFAULT_FED_PRODUCTS,
    FedMarket,
    is_cfm_product,
    normalize_futures_product,
    order_size_for_product,
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

def _cfm_paper_notional(
    settings,
    store,
    product: str,
    *,
    price: float,
    notional_usd: float,
    now,
) -> float | None:
    """Return CFM 1-contract notional or None if unaffordable; passthrough for non-CFM."""
    if not is_cfm_product(product):
        return float(notional_usd)
    contracts = order_size_for_product(
        product,
        notional_usd=notional_usd,
        price=float(price),
        available_margin_usd=max(
            float(store.cash_usd()),
            float(notional_usd),
            float(settings.fed_max_notional_usd),
        ),
        max_contracts=settings.cfm_max_contracts_per_index(),
        leverage=settings.cfm_order_leverage(),
        margin_rate=float(settings.cfm_margin_rate),
        lane_max_notional_usd=settings.fed_max_notional_usd,
    )
    if contracts < 1:
        return None
    notion = float(contracts) * float(price)
    if store.cash_usd() + 1e-9 < notion:
        try:
            store.set_cash_usd(max(store.cash_usd(), notion * 2), ts=now)
        except Exception:  # noqa: BLE001
            log.exception("fed paper cash top-up failed")
    return notion



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
        from snowball.futures.roll import LaneRollSupport

        self._roll = LaneRollSupport(
            "fed",
            state,
            lambda: getattr(self.state, "fed_ledger", None),
            self.market,
            lambda: bool(self.state.settings.fed_live_orders_permitted()),
        )

    def product_list(self) -> list[str]:
        settings = self.state.settings
        items = [normalize_futures_product(p) for p in settings.fed_product_list]
        items = items or list(DEFAULT_FED_PRODUCTS)
        try:
            return self._roll.products(items)
        except Exception:  # noqa: BLE001
            log.exception("fed front-month resolution failed; using configured products")
            return items

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
        try:
            self._roll.tick(now, halted=halted, can_trade=can_trade)
        except Exception:  # noqa: BLE001
            log.exception("fed contract roll tick failed")

        marks: dict[str, float] = {}
        for product in products:
            snap = self._update_pair(product)
            if snap.last is not None:
                marks[product] = snap.last

        self._refresh_budget(marks)

        # Futures stop-loss + liquidation guard (Tb 2026-10-01): every poll, also under HALT /
        # daily kill, before exits/entries. FUTURES-ONLY exception to never-sell/cover-red.
        try:
            from snowball.futures.stops import LaneHooks, run_stops

            run_stops(self.state, LaneHooks(
                lane="fed", ledger=store, market=self.market, pairs=self.state.fed_pairs,
                live=bool(settings.fed_live_orders_permitted()), book=self._stop_book,
                skip=lambda lot: bool(self._roll.in_flight(lot.id)),
            ), now=now, can_trade=can_trade)
        except Exception:  # noqa: BLE001
            log.exception("fed stop tick failed")

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

        # (B) 2026-10-10: a high level alone no longer bets; the dominant outcome's odds must have
        # RISEN by >= FED_MIN_ODDS_SHIFT vs the FedWatch history lookback.
        min_shift = float(getattr(self.state.settings, "fed_min_odds_shift", 0.0) or 0.0)
        deltas = research.get("history_deltas") or {}
        shift = None
        if dominant in ("hike", "cut") and deltas and f"d_{dominant}" in deltas:
            try:
                shift = float(deltas[f"d_{dominant}"])
            except (TypeError, ValueError):
                shift = None
        shift_ok = min_shift <= 0 or (shift is not None and shift + 1e-9 >= min_shift)
        bet_eligible = bool(eligible_skew and in_window and shift_ok)
        status = "idle"
        if not in_window:
            status = "outside_window"
        elif dominant == "hold":
            status = "hold_flat"
        elif max_prob < BET_SKEW_THRESHOLD:
            status = "skew_below_threshold"
        elif not shift_ok:
            status = "odds_shift_below_min" if shift is not None else "no_odds_history"
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
            "odds_shift": shift,
            "min_odds_shift": min_shift,
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
            from snowball.watcher.feeds import CME_FEDWATCH_TOOL_URL
            summarized["source_attribution"] = {
                "name": "CME FedWatch Tool",
                "url": CME_FEDWATCH_TOOL_URL,
                "provider": "cme-fedwatch",
            }

            store = self._research_store()
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
            store = self._research_store()
            if store is not None:
                store.set_meta("last_research_error", str(exc))

    def _research_store(self) -> Any:
        """Where research snapshots go: the lane ledger (FED_ENABLED) or the research-only store."""
        return self.state.fed_ledger

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
        ok_t, why_t, _sma = self._trend_ok(product, direction, marks.get(product))
        if not ok_t:
            from snowball.futures.stops import _count, _throttled

            _count(self.state, "fed_entry_filtered")
            if _throttled(("fed_trend", product, why_t.split(":", 1)[0]), 3600.0):
                log.info("fed entry filtered: no trend confirmation (fix B)",
                         extra={"data": {"product": product, "direction": direction, "why": why_t}})
            return

        allot = float(self._last_budget.get("per_index_usd") or 0.0)
        budget_left = float(self._last_budget.get("budget_usd") or allot)
        if is_cfm_product(product):
            allot = max(budget_left, float(settings.fed_max_notional_usd))
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
        signal_context.stage(
            product, reason, lane="fed", strategy="fed_desk", action="entry", side=direction,
            rule="FedWatch bet eligible: dominant outcome >= threshold inside meeting window + odds shift >= FED_MIN_ODDS_SHIFT + trend confirmed",
            price=marks.get(product), now=now, timeframe="fomc",
            indicators={k: decision.get(k) for k in ("dominant", "max_prob", "p_hold", "p_hike", "p_cut",
                                                     "meeting_date", "days_left", "in_window", "bet_eligible")
                        if k in decision},
            research={"input": "CME FedWatch (Fed desk research)", "effect": "triggered this order",
                      "meeting": decision.get("meeting_date"), "dominant": decision.get("dominant"),
                      "max_prob": decision.get("max_prob")},
            target={"min_take_profit_pct": settings.min_take_profit_pct, "exit": "green only; prefer day after FOMC"},
            gates={"halted": bool(halted), "daily_killed": bool(daily_killed), "notional_usd": allot},
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

    def _exit_ok(self, lot: Position, px: float | None) -> tuple[bool, str]:
        """Exit gate for every Fed path (scan, paper fill, live limit).

        (B) 2026-10-10: FED_COVER_PCT (2.5%) net of real round-trip CFM fees when > 0, long or
        short; else legacy MIN_TAKE_PROFIT_PCT + FEE_BUFFER_PCT. Never closes red.
        """
        settings = self.state.settings
        cp = float(getattr(settings, "fed_cover_pct", 0.0) or 0.0)
        short = str(lot.side).lower() == "short"
        if cp <= 0:
            if short:
                return short_cover_allowed(lot.gate_entry_price, px, min_take_profit_pct=settings.min_take_profit_pct,
                                           fee_buffer_pct=settings.fee_buffer_pct, never_cover_red=True)
            return long_exit_allowed(lot.gate_entry_price, px, min_take_profit_pct=settings.min_take_profit_pct,
                                     fee_buffer_pct=settings.fee_buffer_pct, never_sell_red=True)
        if px is None or float(px) <= 0 or float(lot.gate_entry_price) <= 0:
            return False, "no_mark"
        q = float(lot.qty) if float(lot.qty) > 0 else 1.0
        basis = float(lot.gate_entry_price)
        gross = ((basis - float(px)) if short else (float(px) - basis)) * q
        if gross < 0:
            return False, "never_cover_red" if short else "never_sell_red"
        fees = float(settings.fee_buffer_pct) * basis * q
        if is_cfm_product(lot.product):
            try:
                from snowball.futures.stops import _aged_fees

                ent, ex = _aged_fees(self.state, self.state.fed_ledger, lot, float(px))
                fees = float(ent) + float(ex)
            except Exception:  # noqa: BLE001
                log.exception("fed exit fee estimate failed; using fee buffer")
        if (gross - fees) / (basis * q) < cp:
            return False, "below_take_profit"
        return True, "ok"

    def _trend_ok(self, product: str, direction: str, last: float | None) -> tuple[bool, str, float | None]:
        """(B) Trend confirmation on daily closes: short needs last < SMA, long needs last > SMA."""
        settings = self.state.settings
        if not bool(getattr(settings, "fed_require_trend", True)):
            return True, "trend_check_off", None
        n = int(getattr(settings, "fed_trend_sma_days", 20) or 20)
        cache = getattr(self, "_daily_cache", None)
        if cache is None:
            cache = self._daily_cache = {}
        hit = cache.get(product)
        closes = None
        if hit and time.monotonic() - hit[0] < 1800.0:
            closes = hit[1]
        else:
            try:
                rows = self.market.fetch_ohlcv(product, "1d", max(n + 5, 40))
                closes = [float(r[4]) for r in rows if len(r) >= 5]
                cache[product] = (time.monotonic(), closes)
            except Exception:  # noqa: BLE001
                log.exception("fed daily candles failed", extra={"data": {"product": product}})
                closes = None
        avg = sma(closes or [], n)
        px = float(last) if last is not None and float(last) > 0 else (float(closes[-1]) if closes else None)
        if avg is None or px is None:
            return False, "no_trend_data", avg
        if direction == "short" and not px < avg:
            return False, f"no_downtrend:{px:.2f}>=sma{n}:{avg:.2f}", avg
        if direction == "long" and not px > avg:
            return False, f"no_uptrend:{px:.2f}<=sma{n}:{avg:.2f}", avg
        return True, "ok", avg

    def _stop_book(self, lot: Position, fill_px: float, fee_usd: float, reason: str, now: datetime) -> float:
        store = self.state.fed_ledger
        assert store is not None
        if lot.side == "short":
            fill = store.cover_short(lot.id, fill_px=fill_px, slippage_bps=0.0, fee_usd=fee_usd, reason=reason, ts=now)
            realized = (lot.entry_price - fill.price) * fill.qty - fill.fee_usd
        else:
            fill = store.close_long(lot.id, fill_px=fill_px, slippage_bps=0.0, fee_usd=fee_usd, reason=reason, ts=now)
            realized = (fill.price - lot.entry_price) * fill.qty - fill.fee_usd
        if lot.product in self.state.fed_pairs:
            self.state.fed_pairs[lot.product].open_count = store.open_count(lot.product)
        return realized

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
                ok, why = self._exit_ok(lot, mark)
            else:
                ok, why = self._exit_ok(lot, mark)
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
            signal_context.stage(
                product, reason, lane="fed", strategy="fed_desk", action="exit",
                side=("buy" if lot.side == "short" else "sell"),
                rule=("post-FOMC green exit" if after else "green >= floor"), price=mark, now=now,
                floor={"min_take_profit_pct": settings.min_take_profit_pct, "fee_buffer_pct": settings.fee_buffer_pct,
                       "fed_cover_pct_net": float(getattr(settings, "fed_cover_pct", 0.0) or 0.0)},
                gates={"never_sell_red/never_cover_red": True, "exit_check": why, "after_fomc": bool(after),
                       "lot": {"id": lot.id, "side": lot.side, "entry": lot.gate_entry_price}},
            )
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
        from snowball.futures.stops import futures_entry_gate

        if not futures_entry_gate(self.state, product, "fed", now, side="long", market=self.market,
                                  live=bool(settings.fed_live_orders_permitted()))[0]:
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
        adj = _cfm_paper_notional(
            settings, store, product, price=float(paper_px), notional_usd=notional_usd, now=now
        )
        if adj is None:
            log.info("fed paper long skipped: cannot fund 1 CFM contract",
                     extra={"data": {"product": product}})
            return
        notional_usd = adj
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
        from snowball.futures.stops import futures_entry_gate

        if not futures_entry_gate(self.state, product, "fed", now, side="short", market=self.market,
                                  live=bool(settings.fed_live_orders_permitted()))[0]:
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
        adj = _cfm_paper_notional(
            settings, store, product, price=float(paper_px), notional_usd=notional_usd, now=now
        )
        if adj is None:
            log.info("fed paper short skipped: cannot fund 1 CFM contract",
                     extra={"data": {"product": product}})
            return
        notional_usd = adj
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
            lev = settings.cfm_order_leverage() if is_cfm_product(product) else 1.0
            amount = order_size_for_product(
                product,
                notional_usd=notional_usd,
                price=float(limit_px),
                available_margin_usd=max(
                    float(store.cash_usd()),
                    float(notional_usd),
                    float(settings.fed_max_notional_usd),
                ),
                max_contracts=settings.cfm_max_contracts_per_index(),
                leverage=lev,
                margin_rate=float(settings.cfm_margin_rate),
                lane_max_notional_usd=settings.fed_max_notional_usd,
            )
            min_amt = 1.0 if is_cfm_product(product) else 0.01
            if amount < min_amt:
                log.info(
                    "fed live entry skipped: amount below min",
                    extra={"data": {"product": product, "amount": amount}},
                )
                return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
                side,
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
        from snowball.futures.stops import _throttled, close_pending, is_netted

        if is_netted(self.state, "fed", lot.id):
            log.info("fed exit skipped: netted pair (bookkeeping only)", extra={"data": {"position_id": lot.id}})
            return
        if close_pending(self.state, "fed", lot.id):
            if _throttled(("close_pending", "fed", lot.id)):
                log.info("fed exit skipped: stop/aged-exit order already in flight for this lot",
                         extra={"data": {"position_id": lot.id}})
            return
        mark = marks.get(product)
        if lot.side == "short":
            ok, why = self._exit_ok(lot, mark)
        else:
            ok, why = self._exit_ok(lot, mark)
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
            ok2, why2 = self._exit_ok(lot, paper_px)
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
            ok2, why2 = self._exit_ok(lot, paper_px)
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
        if is_cfm_product(product):
            amount = float(max(1, int(round(float(lot.qty)))))
        else:
            amount = round_amount_down(float(lot.qty), 0.01)
            if amount < 0.01:
                return
        lev = settings.cfm_order_leverage() if is_cfm_product(product) else 1.0
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
                ok, why = self._exit_ok(lot, limit_px)
            else:
                ok, why = self._exit_ok(lot, limit_px)
            if not ok:
                log.info(
                    "fed live exit refused at limit",
                    extra={"data": {"product": product, "why": why, "limit_px": limit_px}},
                )
                return
            if is_cfm_product(product):
                from snowball.futures.stops import verify_close_allowed

                ok_pos, why_pos, ex_n = verify_close_allowed(self.market, product, lot.side, int(amount))
                if not ok_pos:
                    log.error("fed live exit refused by exchange-position check",
                              extra={"data": {"product": product, "position_id": lot.id, "why": why_pos,
                                              "exchange_contracts": ex_n}})
                    return
            timeout = float(getattr(settings, "maker_timeout_seconds", 90.0) or 90.0)
            order = self.market.create_swap_maker_limit_order(
                product,
                close_side,
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


class FedResearchRunner(FedEngine):
    """FedWatch research ONLY, for FED_ENABLED=false + FED_RESEARCH_ENABLED=true (Tb 2026-10-16).

    Same 3-hourly poll / summary / snapshot rows as the full lane (``_maybe_poll_research``),
    written to snowball_fed.db so the research report keeps its Fed section. It never builds a
    market/order client, never sets ``state.fed_ledger`` / ``state.fed_engine`` (so no stop,
    roll, entry or exit path sees a Fed lane), and every order-capable method is refused.
    """

    def __init__(self, state: AppState, store: Any) -> None:  # noqa: D107 - no super(): no market
        self.state = state
        self._store = store
        self.market = None
        self._roll = None
        self._last_budget = {"account_value_usd": 0.0, "budget_usd": 0.0, "per_index_usd": 0.0}
        self._last_research: dict[str, Any] = {}
        self._last_research_poll_mono = 0.0
        self._bet_status = "disabled"

    def _research_store(self) -> Any:
        return self._store

    def tick(self) -> None:
        if not getattr(self.state.settings, "fed_research_enabled", True):
            return
        self._maybe_poll_research()

    def __getattribute__(self, name: str) -> Any:
        # Hard refusal of anything that could trade (entries, exits, stops, rolls, flatten).
        if name.startswith(("_open", "_close", "_enter", "_exit", "_place", "_flatten", "_stop",
                            "_act", "_maybe_bet", "_maybe_exit", "_bet", "_live", "_paper",
                            "_update_pair", "_refresh_budget", "_roll")):
            raise RuntimeError(f"FedResearchRunner refuses {name}: FED_ENABLED=false (research only)")
        return object.__getattribute__(self, name)


def attach_fed_research(state: AppState) -> FedResearchRunner | None:
    """FedWatch research-only runner when FED_ENABLED=false and FED_RESEARCH_ENABLED=true."""
    settings = state.settings
    if settings.fed_enabled or not getattr(settings, "fed_research_enabled", True):
        return None
    store = FedStore(settings.fed_sqlite_path, settings.fed_bankroll_usd)
    runner = FedResearchRunner(state, store)
    state.fed_research_engine = runner
    log.warning(
        "Fed Desk research-only attached (FED_ENABLED=false; no Fed orders)",
        extra={"data": {"sqlite": str(settings.fed_sqlite_path),
                        "research_poll_seconds": settings.fed_research_poll_seconds}},
    )
    return runner


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
