"""Optional FRED rate snapshots for The Watcher. Skip if no API key."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from snowball.models import utcnow
from snowball.watcher.feeds import FRED_OBS_URL, FRED_SERIES
from snowball.watcher.http import HttpClient, fetch_text
from snowball.watcher.rss import parse_datetime
from snowball.watcher.store import ResearchEvent, WatcherStore, event_fingerprint

log = logging.getLogger("snowball.watcher")


def parse_latest_observation(series_id: str, payload: dict[str, Any]) -> tuple[float | None, datetime | None, dict[str, Any]]:
    rows = payload.get("observations") or []
    if not isinstance(rows, list):
        return None, None, payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("value") or "")
        if raw in ("", "."):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        observed = parse_datetime(str(row.get("date") or "") or None)
        if observed is None and row.get("date"):
            try:
                observed = datetime.strptime(str(row["date"]), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                observed = None
        return value, observed, row
    return None, None, payload


def fetch_fred_series(client: HttpClient, api_key: str, store: WatcherStore) -> int:
    key = (api_key or "").strip()
    if not key:
        log.info("The Watcher: FRED_API_KEY unset; skipping FRED rates")
        return 0
    inserted = 0
    now = utcnow()
    for series_id in FRED_SERIES:
        url = FRED_OBS_URL.format(series_id=series_id, key=key)
        try:
            body = fetch_text(client, url)
            payload = json.loads(body)
        except Exception:
            log.exception("The Watcher: FRED fetch failed", extra={"data": {"series": series_id}})
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("error_code"):
            log.warning(
                "The Watcher: FRED error",
                extra={"data": {"series": series_id, "error": payload.get("error_message")}},
            )
            continue
        value, observed, raw = parse_latest_observation(series_id, payload)
        title = f"{series_id}={value}" if value is not None else series_id
        series_url = f"https://fred.stlouisfed.org/series/{series_id}"
        store.upsert_rate(series_id, value, observed, title, series_url, raw if isinstance(raw, dict) else {}, now)
        ev = ResearchEvent(
            source="fred",
            kind="rate",
            title=title,
            url=series_url,
            published_at=observed,
            country="united states",
            importance=None,
            tags=["rate"],
            raw_json={"series_id": series_id, "observation": raw},
            fetched_at=now,
            fingerprint=event_fingerprint("fred", series_url, title, observed),
        )
        if store.upsert(ev):
            inserted += 1
    return inserted
