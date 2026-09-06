"""X (Twitter) API v2 recent search for Yolo Demon. Pay-per-use; gated by X_ENABLED."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from snowball.yolo_demon.http import HttpClient

log = logging.getLogger("snowball.yolo_demon")

# Official host; api.twitter.com also works for v2.
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

DEFAULT_QUERY = "($BTC OR $ETH OR $TSLA OR $NVDA) -is:retweet lang:en"


@dataclass
class XTweet:
    tweet_id: str
    text: str
    created_at_unix: float
    url: str


class XClient:
    def __init__(self, bearer_token: str, http: HttpClient) -> None:
        self.bearer_token = (bearer_token or "").strip()
        self.http = http

    def configured(self) -> bool:
        return bool(self.bearer_token)

    def recent_search(self, query: str = DEFAULT_QUERY, max_results: int = 10) -> list[XTweet]:
        # API requires max_results between 10 and 100 for recent search.
        n = max(10, min(int(max_results), 100))
        params = urlencode(
            {
                "query": query,
                "max_results": str(n),
                "tweet.fields": "created_at,lang",
            }
        )
        url = f"{SEARCH_URL}?{params}"
        raw = self.http.get_bytes(
            url,
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return parse_recent_search(payload)


def parse_recent_search(payload: Any) -> list[XTweet]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or []
    out: list[XTweet] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip()
        if not tid:
            continue
        text = str(row.get("text") or "")
        created = str(row.get("created_at") or "")
        out.append(
            XTweet(
                tweet_id=tid,
                text=text,
                created_at_unix=_parse_iso(created),
                url=f"https://x.com/i/web/status/{tid}",
            )
        )
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
