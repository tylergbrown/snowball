"""YouTube Data API v3 client for Yolo Demon. Prefer uploads playlist (1 quota unit)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from snowball.yolo_demon.http import HttpClient

log = logging.getLogger("snowball.yolo_demon")

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

DEFAULT_CHANNEL_HANDLES = ("thetradingfraternity", "thestockmarket", "elliotrades_official")
DEFAULT_BACKFILL_SINCE = "2026-01-01T00:00:00Z"

# Only used when no handles configured or YOUTUBE_ALLOW_KEYWORD_SEARCH=true.
DEFAULT_QUERIES = (
    '"stock market"',
    '"day trading"',
    "$TSLA",
    "crypto",
)


@dataclass
class YouTubeVideo:
    video_id: str
    title: str
    description: str
    published_at_unix: float
    url: str
    channel_handle: str | None = None
    channel_id: str | None = None
    published_at_iso: str | None = None


@dataclass
class ChannelInfo:
    channel_id: str
    uploads_playlist_id: str | None = None


class YouTubeClient:
    def __init__(self, api_key: str, http: HttpClient) -> None:
        self.api_key = (api_key or "").strip()
        self.http = http
        self._handle_to_channel: dict[str, ChannelInfo] = {}
        # Back-compat cache used by older resolve_handle callers/tests.
        self._handle_to_id: dict[str, str] = {}

    def configured(self) -> bool:
        return bool(self.api_key)

    def resolve_handle(self, handle: str) -> str | None:
        """Resolve @handle → channelId via channels.list(forHandle=...)."""
        info = self.resolve_channel(handle)
        return info.channel_id if info else None

    def resolve_channel(self, handle: str) -> ChannelInfo | None:
        """Resolve @handle → channelId + uploads playlist (contentDetails). ~1 quota unit."""
        h = handle.strip().lstrip("@")
        if not h:
            return None
        key = h.lower()
        if key in self._handle_to_channel:
            return self._handle_to_channel[key]
        params = urlencode(
            {
                "part": "id,snippet,contentDetails",
                "forHandle": h,
                "key": self.api_key,
            }
        )
        url = f"{CHANNELS_URL}?{params}"
        raw = self.http.get_bytes(url)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        info = parse_channel_info(payload)
        if info:
            self._handle_to_channel[key] = info
            self._handle_to_id[key] = info.channel_id
            return info
        # Fallback: search for the channel (costs more quota); no uploads playlist.
        cid = self._resolve_via_search(h)
        if not cid:
            return None
        info = ChannelInfo(channel_id=cid, uploads_playlist_id=None)
        self._handle_to_channel[key] = info
        self._handle_to_id[key] = cid
        return info

    def _resolve_via_search(self, handle: str) -> str | None:
        params = urlencode(
            {
                "part": "snippet",
                "type": "channel",
                "q": handle,
                "maxResults": "1",
                "key": self.api_key,
            }
        )
        url = f"{SEARCH_URL}?{params}"
        raw = self.http.get_bytes(url)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return None
        items = payload.get("items") or []
        if not items or not isinstance(items[0], dict):
            return None
        cid = items[0].get("id") or {}
        if isinstance(cid, dict):
            channel_id = str(cid.get("channelId") or "").strip()
            if channel_id:
                self._handle_to_id[handle.lower()] = channel_id
                return channel_id
        return None

    def channel_videos(
        self, channel_id: str, *, handle: str | None = None, max_results: int = 20
    ) -> list[YouTubeVideo]:
        """Recent videos via search.list (100 quota units). Prefer playlist_videos when possible."""
        params = urlencode(
            {
                "part": "snippet",
                "type": "video",
                "order": "date",
                "channelId": channel_id,
                "maxResults": str(max(1, min(int(max_results), 25))),
                "key": self.api_key,
            }
        )
        url = f"{SEARCH_URL}?{params}"
        raw = self.http.get_bytes(url)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return parse_search(payload, channel_handle=handle, channel_id=channel_id)

    def playlist_videos(
        self,
        playlist_id: str,
        *,
        handle: str | None = None,
        channel_id: str | None = None,
        max_results: int = 20,
        published_after_unix: float | None = None,
        page_sleep_sec: float = 0.0,
        paginate: bool = False,
    ) -> list[YouTubeVideo]:
        """Fetch uploads via playlistItems.list (1 quota unit/page, max 50/page).

        Uploads playlists are newest-first. When published_after_unix is set, stop
        once items fall before that cutoff. paginate=True walks all pages until
        exhausted or cutoff; otherwise only the first page (capped by max_results).
        """
        out: list[YouTubeVideo] = []
        page_token: str | None = None
        remaining = max(1, int(max_results)) if not paginate else 50
        while True:
            page_size = 50 if paginate else max(1, min(remaining, 50))
            q: dict[str, str] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": str(page_size),
                "key": self.api_key,
            }
            if page_token:
                q["pageToken"] = page_token
            url = f"{PLAYLIST_ITEMS_URL}?{urlencode(q)}"
            raw = self.http.get_bytes(url)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            page_vids, stop = parse_playlist_items(
                payload,
                channel_handle=handle,
                channel_id=channel_id,
                published_after_unix=published_after_unix,
            )
            out.extend(page_vids)
            if not paginate:
                return out[: max(1, int(max_results))]
            if stop:
                break
            if not isinstance(payload, dict):
                break
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                break
            if page_sleep_sec > 0:
                time.sleep(page_sleep_sec)
        return out

    def search(self, query: str, max_results: int = 5) -> list[YouTubeVideo]:
        """Broad keyword search — quota-heavy; only when keyword fallback allowed."""
        params = urlencode(
            {
                "part": "snippet",
                "type": "video",
                "order": "date",
                "maxResults": str(max(1, min(int(max_results), 10))),
                "q": query,
                "key": self.api_key,
            }
        )
        url = f"{SEARCH_URL}?{params}"
        raw = self.http.get_bytes(url)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return parse_search(payload)

    def fetch_channel_handles(
        self, handles: list[str], max_results: int = 20
    ) -> list[YouTubeVideo]:
        """Resolve each handle and pull recent videos (uploads playlist preferred)."""
        out: list[YouTubeVideo] = []
        for handle in handles:
            h = handle.strip().lstrip("@")
            if not h:
                continue
            try:
                info = self.resolve_channel(h)
            except Exception:
                log.exception(
                    "YouTube resolve handle failed",
                    extra={"data": {"handle": h}},
                )
                continue
            if not info:
                log.info(
                    "YouTube handle unresolved; skipping",
                    extra={"data": {"handle": h}},
                )
                continue
            try:
                if info.uploads_playlist_id:
                    vids = self.playlist_videos(
                        info.uploads_playlist_id,
                        handle=h,
                        channel_id=info.channel_id,
                        max_results=max_results,
                        paginate=False,
                    )
                else:
                    vids = self.channel_videos(
                        info.channel_id, handle=h, max_results=max_results
                    )
            except Exception:
                log.exception(
                    "YouTube channel videos failed",
                    extra={"data": {"handle": h, "channel_id": info.channel_id}},
                )
                continue
            out.extend(vids)
        return out

    def backfill_channel_handles(
        self,
        handles: list[str],
        *,
        published_after: str = DEFAULT_BACKFILL_SINCE,
        page_sleep_sec: float = 0.15,
    ) -> dict[str, list[YouTubeVideo]]:
        """Walk every upload since published_after for each handle via playlistItems.

        Quota: channels.list ~1/handle + playlistItems.list 1/page (50 videos).
        Far cheaper than search.list (100/page). Returns {handle: [videos]}.
        """
        since_unix = _parse_iso(published_after)
        by_handle: dict[str, list[YouTubeVideo]] = {}
        for handle in handles:
            h = handle.strip().lstrip("@")
            if not h:
                continue
            try:
                info = self.resolve_channel(h)
            except Exception:
                log.exception(
                    "YouTube backfill resolve failed",
                    extra={"data": {"handle": h}},
                )
                by_handle[h] = []
                continue
            if not info:
                log.info(
                    "YouTube backfill handle unresolved",
                    extra={"data": {"handle": h}},
                )
                by_handle[h] = []
                continue
            try:
                if info.uploads_playlist_id:
                    vids = self.playlist_videos(
                        info.uploads_playlist_id,
                        handle=h,
                        channel_id=info.channel_id,
                        published_after_unix=since_unix or None,
                        page_sleep_sec=page_sleep_sec,
                        paginate=True,
                    )
                else:
                    # Fallback: single search page with publishedAfter (100 units).
                    vids = self._search_channel_since(
                        info.channel_id,
                        handle=h,
                        published_after=published_after,
                    )
            except Exception:
                log.exception(
                    "YouTube backfill failed",
                    extra={"data": {"handle": h}},
                )
                vids = []
            by_handle[h] = vids
            log.info(
                "YouTube backfill channel done",
                extra={"data": {"handle": h, "videos": len(vids)}},
            )
        return by_handle

    def _search_channel_since(
        self, channel_id: str, *, handle: str | None, published_after: str
    ) -> list[YouTubeVideo]:
        """Paginated search.list fallback when uploads playlist unavailable."""
        out: list[YouTubeVideo] = []
        page_token: str | None = None
        while True:
            q: dict[str, str] = {
                "part": "snippet",
                "type": "video",
                "order": "date",
                "channelId": channel_id,
                "publishedAfter": published_after,
                "maxResults": "50",
                "key": self.api_key,
            }
            if page_token:
                q["pageToken"] = page_token
            url = f"{SEARCH_URL}?{urlencode(q)}"
            raw = self.http.get_bytes(url)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            out.extend(
                parse_search(payload, channel_handle=handle, channel_id=channel_id)
            )
            if not isinstance(payload, dict):
                break
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                break
            time.sleep(0.2)
        return out


def parse_channel_id(payload: Any) -> str | None:
    info = parse_channel_info(payload)
    return info.channel_id if info else None


def parse_channel_info(payload: Any) -> ChannelInfo | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items") or []
    if not items or not isinstance(items[0], dict):
        return None
    item = items[0]
    channel_id = str(item.get("id") or "").strip()
    if not channel_id:
        return None
    uploads = None
    details = item.get("contentDetails") or {}
    if isinstance(details, dict):
        related = details.get("relatedPlaylists") or {}
        if isinstance(related, dict):
            uploads = str(related.get("uploads") or "").strip() or None
    return ChannelInfo(channel_id=channel_id, uploads_playlist_id=uploads)


def parse_search(
    payload: Any,
    *,
    channel_handle: str | None = None,
    channel_id: str | None = None,
) -> list[YouTubeVideo]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items") or []
    out: list[YouTubeVideo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        vid = item.get("id") or {}
        video_id = ""
        if isinstance(vid, dict):
            video_id = str(vid.get("videoId") or "").strip()
        if not video_id:
            continue
        snip = item.get("snippet") or {}
        if not isinstance(snip, dict):
            snip = {}
        published = str(snip.get("publishedAt") or "")
        ch_id = channel_id or str(snip.get("channelId") or "") or None
        out.append(
            YouTubeVideo(
                video_id=video_id,
                title=str(snip.get("title") or ""),
                description=str(snip.get("description") or ""),
                published_at_unix=_parse_iso(published),
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel_handle=channel_handle,
                channel_id=ch_id,
                published_at_iso=published or None,
            )
        )
    return out


def parse_playlist_items(
    payload: Any,
    *,
    channel_handle: str | None = None,
    channel_id: str | None = None,
    published_after_unix: float | None = None,
) -> tuple[list[YouTubeVideo], bool]:
    """Parse one playlistItems page. Returns (videos, stop_pagination).

    stop_pagination is True when an item older than published_after_unix is seen
    (uploads are newest-first, so older pages can be skipped).
    """
    if not isinstance(payload, dict):
        return [], True
    items = payload.get("items") or []
    out: list[YouTubeVideo] = []
    stop = False
    for item in items:
        if not isinstance(item, dict):
            continue
        snip = item.get("snippet") or {}
        if not isinstance(snip, dict):
            snip = {}
        details = item.get("contentDetails") or {}
        if not isinstance(details, dict):
            details = {}
        video_id = str(details.get("videoId") or "").strip()
        if not video_id:
            res = snip.get("resourceId") or {}
            if isinstance(res, dict):
                video_id = str(res.get("videoId") or "").strip()
        if not video_id:
            continue
        published = str(
            details.get("videoPublishedAt") or snip.get("publishedAt") or ""
        )
        published_unix = _parse_iso(published)
        if published_after_unix is not None and published_unix:
            if published_unix < published_after_unix:
                stop = True
                break
        ch_id = channel_id or str(snip.get("channelId") or "") or None
        out.append(
            YouTubeVideo(
                video_id=video_id,
                title=str(snip.get("title") or ""),
                description=str(snip.get("description") or ""),
                published_at_unix=published_unix,
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel_handle=channel_handle,
                channel_id=ch_id,
                published_at_iso=published or None,
            )
        )
    return out, stop


def parse_handles(raw: str | None) -> list[str]:
    """Parse comma-separated handles. None → defaults; empty string → no handles."""
    if raw is None:
        return list(DEFAULT_CHANNEL_HANDLES)
    text = raw.strip()
    if not text:
        return []
    out: list[str] = []
    for part in text.split(","):
        h = part.strip().lstrip("@")
        if h:
            out.append(h)
    return out


def _parse_iso(value: str) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0
