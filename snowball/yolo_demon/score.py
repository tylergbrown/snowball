from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from snowball.models import utcnow
from snowball.yolo_demon.store import YoloIdea, YoloStore
from snowball.yolo_demon.tickers import is_crypto


WINDOW = "6h/24h"


@dataclass
class _Agg:
    mentions_6h: int = 0
    mentions_24h: int = 0
    ups_6h: int = 0
    comments_6h: int = 0
    sample_title: str | None = None
    sample_url: str | None = None
    sample_ups: int = -1
    source: str = "unknown"


def score_ticker(mentions_6h: int, mentions_24h: int, upvotes_6h: int, comments_6h: int) -> float:
    expected_6h = max(mentions_24h * 6.0 / 24.0, 1.0)
    velocity = mentions_6h / expected_6h
    heat = math.log1p(max(0, upvotes_6h)) + 0.5 * math.log1p(max(0, comments_6h))
    return round(float(mentions_6h) * (1.0 + heat) * velocity, 4)


def recompute_ideas(store: YoloStore, now: datetime | None = None) -> list[YoloIdea]:
    """Aggregate mentions into per-(source, ticker) idea rows."""
    now = now or utcnow()
    t24 = now.timestamp() - 24 * 3600
    t6 = now.timestamp() - 6 * 3600
    rows = store.mentions_since(t24)
    aggs: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    for row in rows:
        ticker = str(row["ticker"]).upper()
        keys = row.keys()
        source = "unknown"
        if "source" in keys and row["source"]:
            source = str(row["source"]).strip().lower() or "unknown"
        created = float(row["created_utc"])
        ups = int(row["ups"] or 0)
        comments = int(row["comments"] or 0)
        agg = aggs[(source, ticker)]
        agg.source = source
        agg.mentions_24h += 1
        if created >= t6:
            agg.mentions_6h += 1
            agg.ups_6h += ups
            agg.comments_6h += comments
            if ups >= agg.sample_ups:
                agg.sample_ups = ups
                agg.sample_title = row["title"]
                agg.sample_url = row["url"]
        elif agg.sample_title is None:
            agg.sample_title = row["title"]
            agg.sample_url = row["url"]
            agg.sample_ups = ups
    ideas: list[YoloIdea] = []
    for (source, ticker), agg in aggs.items():
        if agg.mentions_24h < 1:
            continue
        idea = YoloIdea(
            ticker=ticker,
            score=score_ticker(agg.mentions_6h, agg.mentions_24h, agg.ups_6h, agg.comments_6h),
            window=WINDOW,
            sample_title=agg.sample_title,
            url=agg.sample_url,
            fetched_at=now,
            mentions_6h=agg.mentions_6h,
            mentions_24h=agg.mentions_24h,
            upvote_heat=float(agg.ups_6h),
            comment_heat=float(agg.comments_6h),
            is_crypto=is_crypto(ticker),
            source=source,
        )
        store.upsert_idea(idea)
        ideas.append(idea)
    ideas.sort(key=lambda i: i.score, reverse=True)
    return ideas
