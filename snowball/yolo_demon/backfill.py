"""One-shot priority-channel YouTube backfill CLI.

  python -m snowball.yolo_demon.backfill
  python -m snowball.yolo_demon.backfill --force

Uses Settings (env / .env). Prefer letting the sidecar auto-backfill on first
poll; this CLI is for manual catch-up without starting the full bot loop.

`--force` clears per-handle backfill state so every configured handle re-runs.
Without `--force`, only handles not yet completed for YOLO_YOUTUBE_BACKFILL_SINCE
are walked (newly added handles do not require --force).
"""

from __future__ import annotations

import logging
import sys

from snowball.config import Settings
from snowball.logging_setup import setup_logging
from snowball.models import utcnow
from snowball.yolo_demon.http import UrlLibHttp
from snowball.yolo_demon.poller import (
    BACKFILL_COUNTS_META,
    BACKFILL_META_KEY,
    YoloDemonSidecar,
    normalize_handle,
    parse_backfill_completed,
)
from snowball.yolo_demon.store import YoloStore
from snowball.yolo_demon.youtube import DEFAULT_BACKFILL_SINCE, YouTubeClient, parse_handles

log = logging.getLogger("snowball.yolo_demon")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    force = "--force" in argv
    settings = Settings()
    setup_logging(getattr(settings, "log_level", "INFO"))
    if not settings.youtube_api_key.strip():
        log.error("YOUTUBE_API_KEY unset; cannot backfill")
        return 2
    since = (settings.yolo_youtube_backfill_since or DEFAULT_BACKFILL_SINCE).strip()
    handles = parse_handles(settings.youtube_channel_handles)
    if not handles:
        log.error("No YOUTUBE_CHANNEL_HANDLES configured")
        return 2
    store = YoloStore(settings.sqlite_path)
    if force:
        store.set_meta(BACKFILL_META_KEY, "")
        store.set_meta(BACKFILL_COUNTS_META, "")
    completed = parse_backfill_completed(
        store.get_meta(BACKFILL_META_KEY),
        since,
        store.get_meta(BACKFILL_COUNTS_META),
    )
    missing = [h for h in handles if normalize_handle(h) not in completed]
    if not missing:
        log.info(
            "Backfill already complete for all handles",
            extra={
                "data": {
                    "since": since,
                    "completed": sorted(completed),
                    "counts": store.get_meta(BACKFILL_COUNTS_META),
                }
            },
        )
        print(
            f"already_done since={since} completed={sorted(completed)} "
            f"counts={store.get_meta(BACKFILL_COUNTS_META)}"
        )
        return 0
    http = UrlLibHttp(user_agent="SnowballYoloDemon/1.0")
    yt = YouTubeClient(settings.youtube_api_key, http)
    sidecar = YoloDemonSidecar(settings, store, http=http, youtube=yt)
    # Force path through _maybe_backfill by clearing attempted flag
    sidecar._backfill_attempted = False
    n = sidecar._maybe_backfill(handles, utcnow())
    counts = store.get_meta(BACKFILL_COUNTS_META)
    flag = store.get_meta(BACKFILL_META_KEY)
    print(f"backfill_upserted={n} counts={counts} flag={flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
