"""The Watcher sidecar: poll official RSS + optional TE/FRED into the event log.

Runs on a thread next to the paper engine. Never places orders, never touches
the live broker. One source failure must not kill the trading loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from snowball.config import Settings
from snowball.models import utcnow
from snowball.watcher.feeds import (
    SKIPPED_FEEDS,
    WATCHER_OFFICIAL_SOURCES,
    WATCHER_RSS_FEEDS,
)
from snowball.watcher.fred import fetch_fred_series
from snowball.watcher.halt import apply_watcher_halt, matching_halt_event
from snowball.watcher.http import HttpClient, UrlLibHttp, fetch_text
from snowball.watcher.rss import parse_rss
from snowball.watcher.store import ResearchEvent, WatcherStore, event_fingerprint
from snowball.watcher.tagger import tag_text
from snowball.watcher.te import fetch_calendar
from snowball.watcher.fedwatch import poll_fedwatch_into_store

log = logging.getLogger("snowball.watcher")


class WatcherSidecar:
    def __init__(
        self,
        settings: Settings,
        store: WatcherStore,
        http: HttpClient | None = None,
        running: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = http or UrlLibHttp()
        # running is either AppState or a namespace with .running
        self._running = running
        self.last_error: str | None = None
        self.last_poll_at = None
        self._logged_skip = False

    def _alive(self) -> bool:
        if self._running is None:
            return True
        return bool(getattr(self._running, "running", True))

    def poll_once(self) -> dict[str, int]:
        """Fetch all sources. Never raises to the engine loop."""
        stats = {"rss_new": 0, "te_new": 0, "fred_new": 0, "fedwatch_new": 0, "rss_errors": 0}
        # Task 15: per-feed status persisted for the daily PDF (no secrets, no URLs with keys)
        self._feed_status: dict[str, dict] = {}
        for skipped in SKIPPED_FEEDS:
            self._feed_status[f"skipped:{skipped.source}"] = {
                "source": skipped.source, "state": "unavailable", "why": skipped.reason}
        if not self._logged_skip:
            for skipped in SKIPPED_FEEDS:
                log.warning(
                    "The Watcher skipping dead official feed",
                    extra={"data": {"source": skipped.source, "url": skipped.url, "reason": skipped.reason}},
                )
            for src in WATCHER_OFFICIAL_SOURCES:
                log.info(
                    "The Watcher official research source",
                    extra={"data": {"source": src.source, "url": src.url, "label": src.label, "fetch_path": src.fetch_path}},
                )
            self._logged_skip = True
        try:
            stats["rss_new"], stats["rss_errors"] = self._poll_rss()
        except Exception:
            log.exception("The Watcher RSS poll failed")
            self.last_error = "rss poll failed"
        try:
            stats["te_new"] = self._poll_te()
            self._feed_status["te_calendar"] = (
                {"source": "tradingeconomics", "state": "ok", "new": stats["te_new"]}
                if (self.settings.tradingeconomics_api_key or "").strip()
                else {"source": "tradingeconomics", "state": "unavailable",
                      "why": "TRADINGECONOMICS_API_KEY unset (no FOMC/rate calendar events)"})
        except Exception as exc:
            log.exception("The Watcher TE poll failed")
            self.last_error = "te poll failed"
            self._feed_status["te_calendar"] = {"source": "tradingeconomics", "state": "error",
                                                "why": type(exc).__name__ + ": " + str(exc)[:120]}
        try:
            stats["fred_new"] = self._poll_fred()
            self._feed_status["fred"] = (
                {"source": "fred", "state": "ok", "new": stats["fred_new"]}
                if (self.settings.fred_api_key or "").strip()
                else {"source": "fred", "state": "unavailable", "why": "FRED_API_KEY unset"})
        except Exception as exc:
            log.exception("The Watcher FRED poll failed")
            self.last_error = "fred poll failed"
            self._feed_status["fred"] = {"source": "fred", "state": "error",
                                         "why": type(exc).__name__ + ": " + str(exc)[:120]}
        try:
            stats["fedwatch_new"] = self._poll_fedwatch()
            self._feed_status["cme_fedwatch"] = {"source": "cme_fedwatch", "state": "ok",
                                                 "new": stats["fedwatch_new"]}
        except Exception as exc:
            log.exception("The Watcher FedWatch poll failed")
            self.last_error = "fedwatch poll failed"
            self._feed_status["cme_fedwatch"] = {"source": "cme_fedwatch", "state": "error",
                                                 "why": type(exc).__name__ + ": " + str(exc)[:120]}
        try:
            self._maybe_halt()
        except Exception:
            log.exception("The Watcher halt window failed")
            self.last_error = "halt window failed"
        self.last_poll_at = utcnow()
        self.store.set_meta("last_poll_at", self.last_poll_at.isoformat())
        try:
            import json as _json

            self.store.set_meta("feed_status", _json.dumps(
                {"at": self.last_poll_at.isoformat(), "feeds": self._feed_status}, default=str))
        except Exception:  # noqa: BLE001
            log.exception("The Watcher feed_status meta write failed")
        if self.last_error:
            self.store.set_meta("last_error", self.last_error)
        log.info("The Watcher poll complete", extra={"data": stats})
        return stats

    def _poll_rss(self) -> tuple[int, int]:
        inserted = 0
        errors = 0
        now = utcnow()
        status = getattr(self, "_feed_status", None)
        if status is None:
            status = self._feed_status = {}
        for feed in WATCHER_RSS_FEEDS:
            key = f"rss:{feed.source}:{feed.url.rsplit('/', 1)[-1][:40]}"
            try:
                body = fetch_text(self.http, feed.url)
                items = parse_rss(body)
                if not items:
                    log.info(
                        "The Watcher RSS empty or non-XML",
                        extra={"data": {"source": feed.source, "url": feed.url}},
                    )
                    status[key] = {"source": feed.source, "state": "empty", "why": "empty or non-XML body"}
                    continue
                status[key] = {"source": feed.source, "state": "ok", "items": len(items)}
                for item in items:
                    tags = tag_text(item.title, item.summary)
                    if feed.source == "treasury" and "treasury" not in tags:
                        tags.append("treasury")
                    ev = ResearchEvent(
                        source=feed.source,
                        kind="press",
                        title=item.title,
                        url=item.url or None,
                        published_at=item.published_at,
                        country=feed.country,
                        importance=None,
                        tags=tags,
                        raw_json=item.raw,
                        fetched_at=now,
                        fingerprint=event_fingerprint(
                            feed.source, item.url, item.title, item.published_at
                        ),
                    )
                    if self.store.upsert(ev):
                        inserted += 1
            except Exception as exc:
                errors += 1
                status[key] = {"source": feed.source, "state": "error",
                               "why": type(exc).__name__ + ": " + str(exc)[:120]}
                log.exception(
                    "The Watcher RSS feed failed",
                    extra={"data": {"source": feed.source, "url": feed.url}},
                )
        return inserted, errors

    def _poll_te(self) -> int:
        key = self.settings.tradingeconomics_api_key
        events = fetch_calendar(self.http, key)
        return self.store.upsert_many(events)

    def _poll_fred(self) -> int:
        return fetch_fred_series(self.http, self.settings.fred_api_key, self.store)

    def _poll_fedwatch(self) -> int:
        """CME FedWatch probs via cme-fedwatch package — research only, no orders."""
        return poll_fedwatch_into_store(self.store)

    def _maybe_halt(self) -> None:
        settings = self.settings
        if not settings.watcher_halt_around_fomc:
            return
        now = utcnow()
        events = self.store.high_importance_rate_events()
        match = matching_halt_event(
            events,
            now,
            settings.watcher_halt_minutes_before,
            settings.watcher_halt_minutes_after,
        )
        reason = None
        if match is not None:
            reason = f"{match.source}:{match.title}"
        apply_watcher_halt(settings.halt_file, match is not None, reason=reason)

    def run_forever(self) -> None:
        interval = max(30.0, float(self.settings.watcher_poll_seconds))
        log.info(
            "The Watcher loop start",
            extra={"data": {"poll_seconds": interval, "enabled": True}},
        )
        while self._alive():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("The Watcher tick failed (trading loop unaffected)")
                self.last_error = "tick failed"
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(0.05, remaining)
            while self._alive() and time.monotonic() < deadline:
                time.sleep(0.5)
        log.info("The Watcher stopped")
