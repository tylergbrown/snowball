"""Yolo Demon sidecar. YouTube + X official APIs. Never trades, never writes HALT."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from snowball.config import Settings
from snowball.models import utcnow
from snowball.yolo_demon.http import HttpClient, UrlLibHttp
from snowball.yolo_demon.score import recompute_ideas
from snowball.yolo_demon.store import YoloStore
from snowball.yolo_demon.tickers import extract_tickers
from snowball.yolo_demon.x_api import DEFAULT_QUERY as X_DEFAULT_QUERY
from snowball.yolo_demon.x_api import XClient
from snowball.yolo_demon.youtube import (
    DEFAULT_BACKFILL_SINCE,
    DEFAULT_QUERIES,
    YouTubeClient,
    YouTubeVideo,
    parse_handles,
)

log = logging.getLogger("snowball.yolo_demon")

SOURCE_YOUTUBE = "youtube"
SOURCE_X = "x"
WATCH_TICKER = "WATCH"
BACKFILL_META_KEY = "yolo_youtube_backfill_done"
BACKFILL_COUNTS_META = "yolo_youtube_backfill_counts"


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def _handles_from_counts(counts_raw: str | None) -> set[str]:
    text = (counts_raw or "").strip()
    if not text:
        return set()
    out: set[str] = set()
    for part in text.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        out.add(normalize_handle(part.split("=", 1)[0]))
    return out


def parse_backfill_completed(
    meta_raw: str | None,
    since: str,
    counts_raw: str | None = None,
) -> set[str]:
    """Handles already backfilled for this since value.

    New format: JSON {"since": "...", "completed": ["handle", ...]}.
    Legacy scalar (meta == since or "1"): infer completed from counts meta
    so newly added handles still backfill without --force.
    """
    raw = (meta_raw or "").strip()
    if not raw:
        return set()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(data, dict):
            return set()
        if str(data.get("since") or "").strip() != since:
            return set()
        completed = data.get("completed") or []
        if not isinstance(completed, list):
            return set()
        return {normalize_handle(str(h)) for h in completed if str(h).strip()}
    if raw == since or raw == "1":
        return _handles_from_counts(counts_raw)
    return set()


def encode_backfill_meta(since: str, completed: set[str] | list[str]) -> str:
    normed = sorted({normalize_handle(h) for h in completed if str(h).strip()})
    return json.dumps({"since": since, "completed": normed}, separators=(",", ":"))


def parse_backfill_counts(counts_raw: str | None) -> dict[str, int]:
    text = (counts_raw or "").strip()
    out: dict[str, int] = {}
    if not text:
        return out
    for part in text.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[normalize_handle(k)] = int(v)
        except ValueError:
            continue
    return out


def encode_backfill_counts(counts: dict[str, int]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(counts.items()))


class YoloDemonSidecar:
    def __init__(
        self,
        settings: Settings,
        store: YoloStore,
        http: HttpClient | None = None,
        running: Any = None,
        youtube: YouTubeClient | None = None,
        x_client: XClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = http or UrlLibHttp(user_agent="SnowballYoloDemon/1.0")
        self._running = running
        self.youtube = youtube or YouTubeClient(settings.youtube_api_key, self.http)
        self.x_client = x_client or XClient(settings.x_bearer_token, self.http)
        self.last_error: str | None = None
        self.last_poll_at = None
        self.disabled_reason: str | None = None
        self._idle_logged = False
        self._yt_query_idx = 0
        self.source_status: dict[str, str] = {}
        self._backfill_attempted = False

    def _alive(self) -> bool:
        if self._running is None:
            return True
        return bool(getattr(self._running, "running", True))

    def any_source_configured(self) -> bool:
        return self.youtube.configured() or (
            self.x_client.configured() and self.settings.x_enabled
        )

    def is_configured(self) -> bool:
        return self.any_source_configured()

    def poll_once(self) -> dict[str, int]:
        stats = {
            "posts": 0,
            "mentions": 0,
            "ideas": 0,
            "youtube": 0,
            "x": 0,
            "videos": 0,
            "backfill_videos": 0,
        }
        self.source_status = {
            "youtube": "configured" if self.youtube.configured() else "missing_key",
            "x": self._x_status_label(),
        }
        if not self.any_source_configured():
            if not self._idle_logged:
                log.info(
                    "Yolo Demon idle: no YouTube/X sources configured "
                    "(set YOUTUBE_API_KEY; "
                    "X needs X_BEARER_TOKEN and X_ENABLED=true). "
                    "StockTwits dropped (no API keys issued)."
                )
                self._idle_logged = True
            self.disabled_reason = "no_sources_configured"
            self.store.set_meta("disabled_reason", self.disabled_reason)
            self.last_poll_at = utcnow()
            self.store.set_meta(
                "source_status",
                ",".join(f"{k}={v}" for k, v in self.source_status.items()),
            )
            return stats

        self.disabled_reason = None
        self.store.set_meta("disabled_reason", "")
        now = utcnow()

        try:
            n, n_vid, n_bf = self._poll_youtube(now)
            stats["youtube"] = n
            stats["videos"] = n_vid
            stats["backfill_videos"] = n_bf
            stats["posts"] += n
        except Exception:
            log.exception("Yolo Demon YouTube poll failed")
            self.last_error = "youtube failed"
            self.source_status["youtube"] = "error"

        try:
            n = self._poll_x(now)
            stats["x"] = n
            stats["posts"] += n
        except Exception:
            log.exception("Yolo Demon X poll failed")
            self.last_error = "x failed"
            self.source_status["x"] = "error"

        self.store.prune_mentions(36)
        ideas = recompute_ideas(self.store, now)
        stats["ideas"] = len(ideas)
        stats["mentions"] = sum(i.mentions_24h for i in ideas)
        self.last_poll_at = now
        self.store.set_meta("last_poll_at", now.isoformat())
        self.store.set_meta(
            "source_status",
            ",".join(f"{k}={v}" for k, v in self.source_status.items()),
        )
        log.info("Yolo Demon poll complete", extra={"data": stats})
        return stats

    def _x_status_label(self) -> str:
        if not self.x_client.configured():
            return "missing_key"
        if not self.settings.x_enabled:
            return "disabled_by_default"
        used = self.store.x_reads_today()
        cap = int(self.settings.x_daily_max_reads)
        if used >= cap:
            return f"daily_cap_hit({used}/{cap})"
        return f"configured({used}/{cap})"

    def _poll_youtube(self, now) -> tuple[int, int, int]:
        """Returns (mention_posts, priority_videos_upserted, backfill_videos)."""
        if not self.youtube.configured():
            log.info("Yolo Demon YouTube idle: YOUTUBE_API_KEY unset")
            return 0, 0, 0
        handles = parse_handles(self.settings.youtube_channel_handles)
        posts = 0
        videos_upserted = 0
        backfill_n = 0
        priority_videos: list[YouTubeVideo] = []
        keyword_videos: list[YouTubeVideo] = []
        channel_attempted = False
        channel_failed = False

        # One-shot Jan→now backfill before normal recent poll.
        if handles:
            backfill_n = self._maybe_backfill(handles, now)

        max_results = max(1, min(int(self.settings.youtube_priority_max_results), 25))
        if handles:
            channel_attempted = True
            try:
                priority_videos = self.youtube.fetch_channel_handles(
                    handles, max_results=max_results
                )
            except Exception:
                log.exception("Yolo Demon YouTube channel fetch failed")
                channel_failed = True
                priority_videos = []

        allow_kw = bool(self.settings.youtube_allow_keyword_search)
        use_keyword = False
        if not handles:
            use_keyword = True
            log.info("Yolo Demon YouTube: no channel handles; keyword search")
        elif (not priority_videos or channel_failed) and allow_kw:
            use_keyword = True
            log.info(
                "Yolo Demon YouTube: channel fetch empty/failed; keyword fallback"
            )
        elif handles and not priority_videos and not allow_kw:
            log.info(
                "Yolo Demon YouTube: channel handles set; skipping keyword search "
                "(set YOUTUBE_ALLOW_KEYWORD_SEARCH=true to enable fallback)"
            )

        if use_keyword:
            q = DEFAULT_QUERIES[self._yt_query_idx % len(DEFAULT_QUERIES)]
            self._yt_query_idx += 1
            try:
                keyword_videos = list(self.youtube.search(q, max_results=5))
            except Exception:
                log.exception("Yolo Demon YouTube keyword search failed")

        # Priority channels: every video → yolo_videos + mention (WATCH if no ticker).
        for vid in priority_videos:
            n_m, n_v = self._ingest_priority_video(vid, now)
            posts += n_m
            videos_upserted += n_v

        # Keyword search: tickers required (unchanged).
        for vid in keyword_videos:
            posts += self._ingest_keyword_video(vid, now)

        if priority_videos or keyword_videos or backfill_n:
            self.source_status["youtube"] = "ok"
        elif channel_attempted and not channel_failed:
            self.source_status["youtube"] = "ok_no_tickers"
        else:
            self.source_status["youtube"] = "idle"
        return posts, videos_upserted, backfill_n

    def _maybe_backfill(self, handles: list[str], now) -> int:
        since = (self.settings.yolo_youtube_backfill_since or "").strip()
        if not since:
            return 0
        completed = parse_backfill_completed(
            self.store.get_meta(BACKFILL_META_KEY),
            since,
            self.store.get_meta(BACKFILL_COUNTS_META),
        )
        missing = [h for h in handles if normalize_handle(h) not in completed]
        if not missing:
            return 0
        if self._backfill_attempted:
            return 0
        self._backfill_attempted = True
        log.info(
            "Yolo Demon YouTube backfill start",
            extra={
                "data": {
                    "since": since,
                    "handles": missing,
                    "already_done": sorted(completed),
                }
            },
        )
        try:
            by_handle = self.youtube.backfill_channel_handles(
                missing, published_after=since
            )
        except Exception:
            log.exception("Yolo Demon YouTube backfill failed")
            self.last_error = "youtube backfill failed"
            return 0
        total = 0
        counts = parse_backfill_counts(self.store.get_meta(BACKFILL_COUNTS_META))
        for handle, vids in by_handle.items():
            h = normalize_handle(handle)
            counts[h] = len(vids)
            for vid in vids:
                _, n_v = self._ingest_priority_video(vid, now)
                total += n_v
            completed.add(h)
            # Persist after each handle so a crash mid-run does not redo finished ones.
            self.store.set_meta(BACKFILL_META_KEY, encode_backfill_meta(since, completed))
            self.store.set_meta(BACKFILL_COUNTS_META, encode_backfill_counts(counts))
        for handle in missing:
            h = normalize_handle(handle)
            if h not in completed:
                completed.add(h)
                counts.setdefault(h, 0)
        self.store.set_meta(BACKFILL_META_KEY, encode_backfill_meta(since, completed))
        self.store.set_meta(BACKFILL_COUNTS_META, encode_backfill_counts(counts))
        log.info(
            "Yolo Demon YouTube backfill complete",
            extra={"data": {"since": since, "counts": counts, "upserted": total}},
        )
        return total

    def _ingest_priority_video(self, vid: YouTubeVideo, now) -> tuple[int, int]:
        """Persist yolo_videos always; mention with tickers or WATCH. Returns (mentions, video_rows)."""
        handle = vid.channel_handle or "unknown"
        title_prefix = f"@{handle}: "
        sample = f"{title_prefix}{vid.title}" if vid.title else title_prefix
        tickers = extract_tickers(vid.title, vid.description)
        published_iso = vid.published_at_iso
        if not published_iso and vid.published_at_unix:
            published_iso = datetime.fromtimestamp(
                vid.published_at_unix, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        new_video = self.store.upsert_video(
            video_id=vid.video_id,
            channel_handle=vid.channel_handle,
            title=vid.title,
            url=vid.url,
            published_at=published_iso,
            tickers=tickers,
            fetched_at=now,
        )
        mention_tickers = tickers if tickers else [WATCH_TICKER]
        posts = 0
        for ticker in mention_tickers:
            if self.store.upsert_mention(
                ticker=ticker,
                post_id=f"yt-{vid.video_id}",
                created_utc=vid.published_at_unix or now.timestamp(),
                ups=0,
                comments=0,
                title=sample[:300],
                url=vid.url,
                subreddit=handle,
                fetched_at=now,
                source=SOURCE_YOUTUBE,
            ):
                posts += 1
        return posts, (1 if new_video else 0)

    def _ingest_keyword_video(self, vid: YouTubeVideo, now) -> int:
        """Keyword-search path: require tickers; do not write yolo_videos."""
        handle = vid.channel_handle or "search"
        title_prefix = f"@{handle}: " if vid.channel_handle else ""
        sample = f"{title_prefix}{vid.title}" if vid.title else title_prefix
        tickers = extract_tickers(vid.title, vid.description)
        if not tickers:
            return 0
        posts = 0
        for ticker in tickers:
            if self.store.upsert_mention(
                ticker=ticker,
                post_id=f"yt-{vid.video_id}",
                created_utc=vid.published_at_unix or now.timestamp(),
                ups=0,
                comments=0,
                title=sample[:300],
                url=vid.url,
                subreddit=handle,
                fetched_at=now,
                source=SOURCE_YOUTUBE,
            ):
                posts += 1
        return posts

    def _poll_x(self, now) -> int:
        if not self.x_client.configured():
            log.info("Yolo Demon X idle: X_BEARER_TOKEN unset")
            return 0
        if not self.settings.x_enabled:
            log.info(
                "Yolo Demon X skipped: X_ENABLED=false (pay-per-use; set X_ENABLED=true to call)"
            )
            self.source_status["x"] = "disabled_by_default"
            return 0
        cap = max(0, int(self.settings.x_daily_max_reads))
        used = self.store.x_reads_today()
        if used >= cap:
            log.info(
                "Yolo Demon X daily read cap hit; skipping",
                extra={"data": {"used": used, "cap": cap}},
            )
            self.source_status["x"] = f"daily_cap_hit({used}/{cap})"
            return 0
        remaining = cap - used
        # Recent search min max_results is 10; request min(10, remaining) but API needs >=10.
        # If remaining < 10, still request 10 but only count/process up to remaining.
        req = 10
        if remaining <= 0:
            return 0
        tweets = self.x_client.recent_search(X_DEFAULT_QUERY, max_results=req)
        tweets = tweets[:remaining]
        n_read = len(tweets)
        if n_read:
            total = self.store.add_x_reads(n_read)
            log.info(
                "Yolo Demon X reads",
                extra={"data": {"batch": n_read, "day_total": total, "cap": cap}},
            )
        posts = 0
        for tw in tweets:
            tickers = extract_tickers(tw.text)
            for ticker in tickers:
                if self.store.upsert_mention(
                    ticker=ticker,
                    post_id=f"x-{tw.tweet_id}",
                    created_utc=tw.created_at_unix or now.timestamp(),
                    ups=0,
                    comments=0,
                    title=tw.text[:280],
                    url=tw.url,
                    subreddit="x",
                    fetched_at=now,
                    source=SOURCE_X,
                ):
                    posts += 1
        self.source_status["x"] = self._x_status_label()
        return posts

    def run_forever(self) -> None:
        interval = max(60.0, float(self.settings.yolo_demon_poll_seconds))
        log.info("Yolo Demon loop start", extra={"data": {"poll_seconds": interval}})
        while self._alive():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("Yolo Demon tick failed (trading loop unaffected)")
                self.last_error = "tick failed"
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(0.05, remaining)
            while self._alive() and time.monotonic() < deadline:
                time.sleep(0.5)
        log.info("Yolo Demon stopped")
