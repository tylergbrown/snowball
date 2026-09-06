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
from snowball.watcher.feeds import SKIPPED_FEEDS, WATCHER_RSS_FEEDS
from snowball.watcher.fred import fetch_fred_series
from snowball.watcher.halt import apply_watcher_halt, matching_halt_event
from snowball.watcher.http import HttpClient, UrlLibHttp, fetch_text
from snowball.watcher.rss import parse_rss
from snowball.watcher.store import ResearchEvent, WatcherStore, event_fingerprint
from snowball.watcher.tagger import tag_text
from snowball.watcher.te import fetch_calendar

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
        stats = {"rss_new": 0, "te_new": 0, "fred_new": 0, "rss_errors": 0}
        if not self._logged_skip:
            for skipped in SKIPPED_FEEDS:
                log.warning(
                    "The Watcher skipping dead official feed",
                    extra={"data": {"source": skipped.source, "url": skipped.url, "reason": skipped.reason}},
                )
            self._logged_skip = True
        try:
            stats["rss_new"], stats["rss_errors"] = self._poll_rss()
        except Exception:
            log.exception("The Watcher RSS poll failed")
            self.last_error = "rss poll failed"
        try:
            stats["te_new"] = self._poll_te()
        except Exception:
            log.exception("The Watcher TE poll failed")
            self.last_error = "te poll failed"
        try:
            stats["fred_new"] = self._poll_fred()
        except Exception:
            log.exception("The Watcher FRED poll failed")
            self.last_error = "fred poll failed"
        try:
            self._maybe_halt()
        except Exception:
            log.exception("The Watcher halt window failed")
            self.last_error = "halt window failed"
        self.last_poll_at = utcnow()
        self.store.set_meta("last_poll_at", self.last_poll_at.isoformat())
        if self.last_error:
            self.store.set_meta("last_error", self.last_error)
        log.info("The Watcher poll complete", extra={"data": stats})
        return stats

    def _poll_rss(self) -> tuple[int, int]:
        inserted = 0
        errors = 0
        now = utcnow()
        for feed in WATCHER_RSS_FEEDS:
            try:
                body = fetch_text(self.http, feed.url)
                items = parse_rss(body)
                if not items:
                    log.info(
                        "The Watcher RSS empty or non-XML",
                        extra={"data": {"source": feed.source, "url": feed.url}},
                    )
                    continue
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
            except Exception:
                errors += 1
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
