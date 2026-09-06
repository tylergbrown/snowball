from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from snowball.models import utcnow


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def event_fingerprint(source: str, url: str | None, title: str, published_at: datetime | None) -> str:
    pub = published_at.isoformat() if published_at else ""
    raw = f"{source}|{url or ''}|{title}|{pub}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ResearchEvent:
    source: str
    kind: str
    title: str
    url: str | None
    published_at: datetime | None
    country: str | None
    importance: int | None
    tags: list[str]
    raw_json: dict[str, Any]
    fetched_at: datetime
    fingerprint: str
    id: int | None = None


class WatcherStore:
    """Sqlite event log. Shared bus for The Watcher and a future stock bot."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                published_at TEXT,
                country TEXT,
                importance INTEGER,
                tags TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                fetched_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_research_events_published
                ON research_events(published_at);
            CREATE INDEX IF NOT EXISTS idx_research_events_kind
                ON research_events(kind, published_at);
            CREATE INDEX IF NOT EXISTS idx_research_events_source
                ON research_events(source);
            CREATE TABLE IF NOT EXISTS research_rates (
                series_id TEXT PRIMARY KEY,
                value REAL,
                observed_at TEXT,
                title TEXT,
                url TEXT,
                raw_json TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watcher_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watcher_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM watcher_meta WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row["value"])

    def upsert(self, event: ResearchEvent) -> bool:
        """Insert if fingerprint is new. Returns True if inserted."""
        tags = json.dumps(list(event.tags), separators=(",", ":"))
        raw = json.dumps(event.raw_json, default=str, separators=(",", ":"))
        pub = event.published_at.isoformat() if event.published_at else None
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO research_events
                    (source, kind, title, url, published_at, country, importance,
                     tags, raw_json, fetched_at, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.source,
                    event.kind,
                    event.title,
                    event.url,
                    pub,
                    event.country,
                    event.importance,
                    tags,
                    raw,
                    event.fetched_at.isoformat(),
                    event.fingerprint,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def upsert_many(self, events: Iterable[ResearchEvent]) -> int:
        n = 0
        for ev in events:
            if self.upsert(ev):
                n += 1
        return n

    def upcoming_calendar(self, now: datetime | None = None, hours: int = 48) -> list[ResearchEvent]:
        now = now or utcnow()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=hours)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM research_events
                WHERE kind = 'calendar'
                  AND published_at IS NOT NULL
                  AND published_at >= ?
                  AND published_at <= ?
                ORDER BY published_at ASC
                """,
                (start, end),
            ).fetchall()
            return [self._event(r) for r in rows]

    def latest_press(self, limit: int = 20) -> list[ResearchEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM research_events
                WHERE kind = 'press'
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._event(r) for r in rows]

    def high_importance_rate_events(self) -> list[ResearchEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM research_events
                WHERE kind = 'calendar'
                  AND importance = 3
                """
            ).fetchall()
            return [self._event(r) for r in rows]

    def upsert_rate(
        self,
        series_id: str,
        value: float | None,
        observed_at: datetime | None,
        title: str,
        url: str,
        raw: dict[str, Any],
        fetched_at: datetime | None = None,
    ) -> None:
        fetched_at = fetched_at or utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO research_rates
                    (series_id, value, observed_at, title, url, raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    value=excluded.value,
                    observed_at=excluded.observed_at,
                    title=excluded.title,
                    url=excluded.url,
                    raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    series_id,
                    value,
                    observed_at.isoformat() if observed_at else None,
                    title,
                    url,
                    json.dumps(raw, default=str),
                    fetched_at.isoformat(),
                ),
            )
            self._conn.commit()

    def rates(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM research_rates ORDER BY series_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM research_events").fetchone()
            return int(row["n"])

    @staticmethod
    def _event(row: sqlite3.Row) -> ResearchEvent:
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(tags, list):
            tags = []
        if not isinstance(raw, dict):
            raw = {"value": raw}
        return ResearchEvent(
            id=int(row["id"]),
            source=str(row["source"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            url=str(row["url"]) if row["url"] else None,
            published_at=_parse_ts(row["published_at"]),
            country=str(row["country"]) if row["country"] else None,
            importance=int(row["importance"]) if row["importance"] is not None else None,
            tags=[str(t) for t in tags],
            raw_json=raw,
            fetched_at=_parse_ts(row["fetched_at"]) or utcnow(),
            fingerprint=str(row["fingerprint"]),
        )


def event_to_dict(ev: ResearchEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "source": ev.source,
        "kind": ev.kind,
        "title": ev.title,
        "url": ev.url,
        "published_at": ev.published_at.isoformat() if ev.published_at else None,
        "country": ev.country,
        "importance": ev.importance,
        "tags": list(ev.tags),
        "fetched_at": ev.fetched_at.isoformat() if ev.fetched_at else None,
        "fingerprint": ev.fingerprint,
    }
