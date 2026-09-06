"""Trading Economics calendar ingest for The Watcher. Skip if no API key."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from snowball.models import utcnow
from snowball.watcher.feeds import TE_CALENDAR_URL, TE_COUNTRIES
from snowball.watcher.http import HttpClient, fetch_text
from snowball.watcher.rss import parse_datetime
from snowball.watcher.store import ResearchEvent, event_fingerprint
from snowball.watcher.tagger import te_event_tags

log = logging.getLogger("snowball.watcher")


def _importance(row: dict[str, Any]) -> int | None:
    raw = row.get("Importance", row.get("importance"))
    if raw is None or raw == "":
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    if val < 1 or val > 3:
        return None
    return val


def parse_calendar_rows(rows: list[dict[str, Any]], fetched_at: datetime | None = None) -> list[ResearchEvent]:
    fetched_at = fetched_at or utcnow()
    events: list[ResearchEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("Event") or row.get("event") or "").strip()
        if not title:
            continue
        country = str(row.get("Country") or row.get("country") or "").strip().lower()
        url = str(row.get("URL") or row.get("url") or "").strip() or None
        published = parse_datetime(str(row.get("Date") or row.get("date") or "") or None)
        category = str(row.get("Category") or row.get("category") or "")
        tags = te_event_tags(title, category)
        fp = event_fingerprint("tradingeconomics", url, title, published)
        events.append(
            ResearchEvent(
                source="tradingeconomics",
                kind="calendar",
                title=title,
                url=url,
                published_at=published,
                country=country or None,
                importance=_importance(row),
                tags=tags,
                raw_json=row,
                fetched_at=fetched_at,
                fingerprint=fp,
            )
        )
    return events


def fetch_calendar(client: HttpClient, api_key: str) -> list[ResearchEvent]:
    key = (api_key or "").strip()
    if not key:
        log.info("The Watcher: TRADINGECONOMICS_API_KEY unset; skipping TE calendar")
        return []
    countries = ",".join(quote(c, safe="") for c in TE_COUNTRIES)
    url = TE_CALENDAR_URL.format(countries=countries, key=key)
    body = fetch_text(client, url)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.warning("The Watcher: TE calendar JSON parse failed")
        return []
    if isinstance(payload, dict) and payload.get("error"):
        log.warning("The Watcher: TE calendar error %s", payload.get("error"))
        return []
    if not isinstance(payload, list):
        log.warning("The Watcher: TE calendar unexpected payload type %s", type(payload).__name__)
        return []
    return parse_calendar_rows(payload)
