"""Earnings Scout sidecar. Research logger only — never trades, never writes HALT."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from snowball.config import Settings
from snowball.earnings.calendar import filter_watchlist, parse_calendar_payload
from snowball.earnings.http import (
    HttpClient,
    UrlLibHttp,
    nasdaq_calendar_headers,
    nasdaq_calendar_url,
    yahoo_chart_url,
)
from snowball.earnings.momentum import parse_yahoo_closes, post_reaction, pre_momentum
from snowball.earnings.store import EarningsStore
from snowball.earnings.watchlist import earnings_watchlist
from snowball.models import utcnow

log = logging.getLogger("snowball.earnings")

MIN_POLL_SECONDS = 10800.0  # 3h floor; config default 14400
DEFAULT_POLL_SECONDS = 14400.0
LOOKAHEAD_DAYS = 10
POST_LOOKBACK_CALENDAR_DAYS = 16  # covers ~10 trading days
# Research-only. This sidecar has no order path.
PLACES_ORDERS = False


def earnings_db_path(settings: Settings) -> Path:
    """Isolated DB. Never the crypto / stock / futures / clerk ledgers."""
    raw = Path(getattr(settings, "earnings_sqlite_path", Path("./data/snowball_earnings.db")))
    parent = Path(settings.sqlite_path).parent
    if raw.name == "snowball_earnings.db" or str(raw) in {
        "./data/snowball_earnings.db",
        "data/snowball_earnings.db",
    }:
        path = parent / "snowball_earnings.db"
    else:
        path = raw
    forbidden = {"snowball.db", "snowball_stocks.db", "snowball_futures.db", "snowball_clerk.db"}
    if path.name in forbidden:
        path = parent / "snowball_earnings.db"
    try:
        for other in (
            settings.sqlite_path,
            settings.stock_sqlite_path,
            getattr(settings, "futures_sqlite_path", None),
            getattr(settings, "clerk_sqlite_path", None),
        ):
            if other is None:
                continue
            if path.resolve() == Path(other).resolve():
                path = parent / "snowball_earnings.db"
                break
    except OSError:
        pass
    return path


class EarningsScout:
    """Logs earnings calendar + momentum/reaction. Does not place orders or write HALT."""

    places_orders = False

    def __init__(
        self,
        settings: Settings,
        store: EarningsStore,
        http: HttpClient | None = None,
        running: Any = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        watchlist: set[str] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = http or UrlLibHttp()
        self._running = running
        self._sleep = sleep or time.sleep
        self._now = now or utcnow
        self._watchlist = watchlist
        self.last_error: str | None = None
        self.last_poll_at: datetime | None = None
        self.last_counts: dict[str, int] = {}
        self.places_orders = False

    def _alive(self) -> bool:
        if self._running is None:
            return True
        return bool(getattr(self._running, "running", True))

    def poll_interval(self) -> float:
        raw = float(getattr(self.settings, "earnings_poll_seconds", DEFAULT_POLL_SECONDS) or DEFAULT_POLL_SECONDS)
        # Clamp to [3h, 6h] window intent; allow up to 6h+, floor 3h.
        return max(MIN_POLL_SECONDS, min(raw, 21600.0) if raw > 0 else DEFAULT_POLL_SECONDS)

    def watchlist(self) -> set[str]:
        if self._watchlist is not None:
            return set(self._watchlist)
        return earnings_watchlist()

    def _due(self) -> bool:
        last = self.store.get_meta("last_poll_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (self._now() - dt).total_seconds()
        return age >= self.poll_interval()

    def _today(self) -> date:
        return self._now().astimezone(timezone.utc).date()

    def _fetch_calendar_day(self, day: date) -> list[dict[str, Any]]:
        url = nasdaq_calendar_url(day.isoformat())
        resp = self.http.get(url, headers=nasdaq_calendar_headers(), timeout=8.0)
        if resp.status != 200:
            raise RuntimeError(f"Nasdaq calendar HTTP {resp.status} for {day}")
        payload = resp.json()
        return parse_calendar_payload(payload, report_date=day.isoformat())

    def _fetch_closes(self, ticker: str) -> list[tuple[date, float]]:
        url = yahoo_chart_url(ticker, range_="2mo", interval="1d")
        resp = self.http.get(url, timeout=8.0)
        if resp.status != 200:
            raise RuntimeError(f"Yahoo chart HTTP {resp.status} for {ticker}")
        return parse_yahoo_closes(resp.json())

    def poll_once(self, *, force: bool = False) -> dict[str, int]:
        if not force and not self._due():
            stats = {"skipped": 1, "dates": 0, "upserted": 0, "pre": 0, "post": 0, "errors": 0}
            self.last_counts = stats
            last = self.store.get_meta("last_poll_at")
            if last:
                try:
                    self.last_poll_at = datetime.fromisoformat(last)
                except ValueError:
                    self.last_poll_at = None
            self.last_error = self.store.get_meta("last_error") or None
            log.info("Earnings Scout poll skipped; interval not elapsed")
            return stats

        stats = {
            "skipped": 0,
            "dates": 0,
            "rows": 0,
            "upserted": 0,
            "pre": 0,
            "post": 0,
            "errors": 0,
        }
        errors: list[str] = []
        today = self._today()
        watch = self.watchlist()

        for offset in range(0, LOOKAHEAD_DAYS + 1):
            if not self._alive():
                break
            day = today + timedelta(days=offset)
            try:
                events = self._fetch_calendar_day(day)
                stats["dates"] += 1
                stats["rows"] += len(events)
                kept = filter_watchlist(events, watch)
                for ev in kept:
                    self.store.upsert_event(ev)
                    stats["upserted"] += 1
            except Exception as exc:  # noqa: BLE001 — research poll must not kill the bot
                log.exception("Earnings calendar fetch failed", extra={"data": {"date": day.isoformat()}})
                errors.append(f"{day}: {exc}")
                stats["errors"] += 1
            self._sleep(0.15)

        # Pre-momentum for events in [today .. +10]
        pre_to = (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
        for ev in self.store.events_needing_pre(from_date=today.isoformat(), to_date=pre_to):
            if not self._alive():
                break
            ticker = str(ev["ticker"])
            try:
                closes = self._fetch_closes(ticker)
                snap = pre_momentum(closes)
                self.store.update_momentum(
                    ticker,
                    str(ev["report_date"]),
                    {
                        "pre_ret_5d": snap.get("ret_5d"),
                        "pre_ret_20d": snap.get("ret_20d"),
                        "pre_momentum": snap.get("pre_momentum"),
                        "pre_asof_date": snap.get("asof_date"),
                        "pre_last_close": snap.get("last_close"),
                    },
                )
                stats["pre"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "pre-momentum failed",
                    extra={"data": {"ticker": ticker, "error": str(exc)}},
                )
                stats["errors"] += 1
            self._sleep(0.1)

        # Post-reaction for events in the past ~10 trading / 16 calendar days
        post_from = (today - timedelta(days=POST_LOOKBACK_CALENDAR_DAYS)).isoformat()
        for ev in self.store.events_needing_post(from_date=post_from, to_date=today.isoformat()):
            if not self._alive():
                break
            ticker = str(ev["ticker"])
            try:
                closes = self._fetch_closes(ticker)
                rd = date.fromisoformat(str(ev["report_date"]))
                snap = post_reaction(closes, report_date=rd, session=str(ev.get("session") or "UNK"))
                self.store.update_momentum(
                    ticker,
                    str(ev["report_date"]),
                    {
                        "post_ret_0d": snap.get("ret_0d"),
                        "post_ret_1d": snap.get("ret_1d"),
                        "post_ret_5d": snap.get("ret_5d"),
                        "post_anchor_date": snap.get("anchor_date"),
                        "post_anchor_close": snap.get("anchor_close"),
                    },
                )
                stats["post"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "post-reaction failed",
                    extra={"data": {"ticker": ticker, "error": str(exc)}},
                )
                stats["errors"] += 1
            self._sleep(0.1)

        now = self._now()
        self.last_poll_at = now
        self.last_counts = stats
        err_msg = "; ".join(errors[:5]) if errors else ""
        self.last_error = err_msg or None
        self.store.set_meta("last_poll_at", now.isoformat())
        self.store.set_meta("last_error", err_msg)
        self.store.set_meta("last_counts", str(stats))
        log.info(
            "Earnings Scout poll done",
            extra={"data": {**stats, "places_orders": False}},
        )
        return stats

    def run_forever(self) -> None:
        interval = self.poll_interval()
        log.info(
            "Earnings Scout loop start",
            extra={
                "data": {
                    "poll_seconds": interval,
                    "places_orders": False,
                    "source": "api.nasdaq.com + query1.finance.yahoo.com",
                    "note": "research only; never trades",
                }
            },
        )
        while self._alive():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("Earnings Scout tick failed (trading loop unaffected)")
                self.last_error = "tick failed"
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(1.0, remaining)
            while self._alive() and time.monotonic() < deadline:
                self._sleep(0.5)
        log.info("Earnings Scout stopped")
