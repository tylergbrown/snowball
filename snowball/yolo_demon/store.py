from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowball.models import utcnow


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class YoloIdea:
    ticker: str
    score: float
    window: str
    sample_title: str | None
    url: str | None
    fetched_at: datetime
    mentions_6h: int = 0
    mentions_24h: int = 0
    upvote_heat: float = 0.0
    comment_heat: float = 0.0
    is_crypto: bool = False
    source: str = "unknown"
    id: int | None = None


class YoloStore:
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
            CREATE TABLE IF NOT EXISTS yolo_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                post_id TEXT NOT NULL,
                created_utc REAL NOT NULL,
                ups INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                url TEXT,
                subreddit TEXT,
                source TEXT NOT NULL DEFAULT 'unknown',
                fetched_at TEXT NOT NULL,
                UNIQUE(source, ticker, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_yolo_mentions_created
                ON yolo_mentions(ticker, created_utc);
            CREATE TABLE IF NOT EXISTS yolo_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                score REAL NOT NULL,
                window TEXT NOT NULL,
                sample_title TEXT,
                url TEXT,
                fetched_at TEXT NOT NULL,
                mentions_6h INTEGER NOT NULL DEFAULT 0,
                mentions_24h INTEGER NOT NULL DEFAULT 0,
                upvote_heat REAL NOT NULL DEFAULT 0,
                comment_heat REAL NOT NULL DEFAULT 0,
                is_crypto INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'unknown',
                UNIQUE(source, ticker, window)
            );
            CREATE INDEX IF NOT EXISTS idx_yolo_ideas_score ON yolo_ideas(score DESC);
            CREATE TABLE IF NOT EXISTS yolo_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS yolo_videos (
                video_id TEXT PRIMARY KEY,
                channel_handle TEXT,
                title TEXT,
                url TEXT,
                published_at TEXT,
                tickers_json TEXT NOT NULL DEFAULT '[]',
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_yolo_videos_published
                ON yolo_videos(published_at DESC);
            """
        )
        self._conn.commit()
        self._migrate_source_columns()

    def _table_cols(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}

    def _migrate_source_columns(self) -> None:
        """Add source column / rebuild unique keys for DBs created before multi-source."""
        mention_cols = self._table_cols("yolo_mentions")
        if "source" not in mention_cols:
            self._conn.execute(
                "ALTER TABLE yolo_mentions ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
            )
            self._rebuild_mentions_unique()
        else:
            # Ensure UNIQUE(source, ticker, post_id) even if table was created with old schema
            # that only had UNIQUE(ticker, post_id) via CREATE IF NOT EXISTS skip.
            self._ensure_mentions_unique()

        idea_cols = self._table_cols("yolo_ideas")
        if "source" not in idea_cols:
            self._conn.execute(
                "ALTER TABLE yolo_ideas ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
            )
            self._rebuild_ideas_unique()
        else:
            self._ensure_ideas_unique()
        self._conn.commit()

    def _ensure_mentions_unique(self) -> None:
        # Detect legacy UNIQUE(ticker, post_id) by trying to inspect indexes is brittle;
        # rebuild only if old-style unique index name exists without source.
        idxs = list(self._conn.execute("PRAGMA index_list(yolo_mentions)").fetchall())
        needs = True
        for idx in idxs:
            name = str(idx[1])
            cols = [
                str(r[2])
                for r in self._conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            if cols == ["source", "ticker", "post_id"]:
                needs = False
                break
        if needs:
            self._rebuild_mentions_unique()

    def _ensure_ideas_unique(self) -> None:
        idxs = list(self._conn.execute("PRAGMA index_list(yolo_ideas)").fetchall())
        needs = True
        for idx in idxs:
            name = str(idx[1])
            cols = [
                str(r[2])
                for r in self._conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            if cols == ["source", "ticker", "window"]:
                needs = False
                break
        if needs:
            self._rebuild_ideas_unique()

    def _rebuild_mentions_unique(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS yolo_mentions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                post_id TEXT NOT NULL,
                created_utc REAL NOT NULL,
                ups INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                url TEXT,
                subreddit TEXT,
                source TEXT NOT NULL DEFAULT 'unknown',
                fetched_at TEXT NOT NULL,
                UNIQUE(source, ticker, post_id)
            );
            INSERT OR IGNORE INTO yolo_mentions_new
                (id, ticker, post_id, created_utc, ups, comments, title, url, subreddit, source, fetched_at)
            SELECT id, ticker, post_id, created_utc, ups, comments, title, url, subreddit,
                   COALESCE(NULLIF(source, ''), 'unknown'), fetched_at
            FROM yolo_mentions;
            DROP TABLE yolo_mentions;
            ALTER TABLE yolo_mentions_new RENAME TO yolo_mentions;
            CREATE INDEX IF NOT EXISTS idx_yolo_mentions_created
                ON yolo_mentions(ticker, created_utc);
            """
        )

    def _rebuild_ideas_unique(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS yolo_ideas_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                score REAL NOT NULL,
                window TEXT NOT NULL,
                sample_title TEXT,
                url TEXT,
                fetched_at TEXT NOT NULL,
                mentions_6h INTEGER NOT NULL DEFAULT 0,
                mentions_24h INTEGER NOT NULL DEFAULT 0,
                upvote_heat REAL NOT NULL DEFAULT 0,
                comment_heat REAL NOT NULL DEFAULT 0,
                is_crypto INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'unknown',
                UNIQUE(source, ticker, window)
            );
            INSERT OR IGNORE INTO yolo_ideas_new
                (id, ticker, score, window, sample_title, url, fetched_at,
                 mentions_6h, mentions_24h, upvote_heat, comment_heat, is_crypto, source)
            SELECT id, ticker, score, window, sample_title, url, fetched_at,
                   mentions_6h, mentions_24h, upvote_heat, comment_heat, is_crypto,
                   COALESCE(NULLIF(source, ''), 'unknown')
            FROM yolo_ideas;
            DROP TABLE yolo_ideas;
            ALTER TABLE yolo_ideas_new RENAME TO yolo_ideas;
            CREATE INDEX IF NOT EXISTS idx_yolo_ideas_score ON yolo_ideas(score DESC);
            """
        )

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO yolo_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM yolo_meta WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row["value"])

    def x_reads_today(self, utc_day: str | None = None) -> int:
        day = utc_day or utcnow().strftime("%Y-%m-%d")
        raw = self.get_meta(f"x_reads:{day}")
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def add_x_reads(self, n: int, utc_day: str | None = None) -> int:
        """Increment X posts-read counter for UTC day; return new total."""
        day = utc_day or utcnow().strftime("%Y-%m-%d")
        key = f"x_reads:{day}"
        with self._lock:
            cur = self.x_reads_today(day)
            nxt = cur + max(0, int(n))
            self._conn.execute(
                """
                INSERT INTO yolo_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(nxt)),
            )
            self._conn.commit()
            return nxt

    def upsert_mention(
        self,
        ticker: str,
        post_id: str,
        created_utc: float,
        ups: int,
        comments: int,
        title: str | None,
        url: str | None,
        subreddit: str,
        fetched_at: datetime | None = None,
        source: str = "unknown",
    ) -> bool:
        fetched_at = fetched_at or utcnow()
        source = (source or "unknown").strip().lower()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO yolo_mentions
                    (ticker, post_id, created_utc, ups, comments, title, url, subreddit, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker.upper(),
                    post_id,
                    float(created_utc),
                    int(ups),
                    int(comments),
                    title,
                    url,
                    subreddit,
                    source,
                    fetched_at.isoformat(),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def prune_mentions(self, older_than_hours: int = 36) -> None:
        cutoff = utcnow().timestamp() - older_than_hours * 3600
        with self._lock:
            self._conn.execute("DELETE FROM yolo_mentions WHERE created_utc < ?", (cutoff,))
            self._conn.commit()

    def upsert_idea(self, idea: YoloIdea) -> None:
        source = (idea.source or "unknown").strip().lower()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO yolo_ideas
                    (ticker, score, window, sample_title, url, fetched_at,
                     mentions_6h, mentions_24h, upvote_heat, comment_heat, is_crypto, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, ticker, window) DO UPDATE SET
                    score=excluded.score,
                    sample_title=excluded.sample_title,
                    url=excluded.url,
                    fetched_at=excluded.fetched_at,
                    mentions_6h=excluded.mentions_6h,
                    mentions_24h=excluded.mentions_24h,
                    upvote_heat=excluded.upvote_heat,
                    comment_heat=excluded.comment_heat,
                    is_crypto=excluded.is_crypto
                """,
                (
                    idea.ticker.upper(),
                    float(idea.score),
                    idea.window,
                    idea.sample_title,
                    idea.url,
                    idea.fetched_at.isoformat(),
                    idea.mentions_6h,
                    idea.mentions_24h,
                    idea.upvote_heat,
                    idea.comment_heat,
                    1 if idea.is_crypto else 0,
                    source,
                ),
            )
            self._conn.commit()

    def top_ideas(self, limit: int = 20, window: str = "6h/24h") -> list[YoloIdea]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM yolo_ideas
                WHERE window = ?
                ORDER BY score DESC, mentions_6h DESC
                LIMIT ?
                """,
                (window, limit),
            ).fetchall()
            return [self._idea(r) for r in rows]

    def source_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT source, COUNT(*) AS n FROM yolo_ideas
                GROUP BY source
                """
            ).fetchall()
            return {str(r["source"]): int(r["n"]) for r in rows}

    def mentions_since(self, cutoff_unix: float) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM yolo_mentions WHERE created_utc >= ?",
                    (cutoff_unix,),
                ).fetchall()
            )

    @staticmethod
    def _idea(row: sqlite3.Row) -> YoloIdea:
        keys = row.keys()
        source = str(row["source"]) if "source" in keys and row["source"] else "unknown"
        return YoloIdea(
            id=int(row["id"]),
            ticker=str(row["ticker"]),
            score=float(row["score"]),
            window=str(row["window"]),
            sample_title=str(row["sample_title"]) if row["sample_title"] else None,
            url=str(row["url"]) if row["url"] else None,
            fetched_at=_parse_ts(row["fetched_at"]) or utcnow(),
            mentions_6h=int(row["mentions_6h"]),
            mentions_24h=int(row["mentions_24h"]),
            upvote_heat=float(row["upvote_heat"]),
            comment_heat=float(row["comment_heat"]),
            is_crypto=bool(row["is_crypto"]),
            source=source,
        )


    def upsert_video(
        self,
        video_id: str,
        channel_handle: str | None,
        title: str | None,
        url: str | None,
        published_at: str | None,
        tickers: list[str] | None = None,
        fetched_at: datetime | None = None,
    ) -> bool:
        """Insert or refresh a priority-channel watch video. Returns True if newly inserted."""
        fetched_at = fetched_at or utcnow()
        tickers_json = json.dumps([t.upper() for t in (tickers or [])])
        with self._lock:
            existing = self._conn.execute(
                "SELECT video_id FROM yolo_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO yolo_videos
                    (video_id, channel_handle, title, url, published_at, tickers_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    channel_handle=excluded.channel_handle,
                    title=excluded.title,
                    url=excluded.url,
                    published_at=excluded.published_at,
                    tickers_json=excluded.tickers_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    video_id,
                    channel_handle,
                    title,
                    url,
                    published_at,
                    tickers_json,
                    fetched_at.isoformat(),
                ),
            )
            self._conn.commit()
            return existing is None

    def recent_videos(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM yolo_videos
                ORDER BY COALESCE(published_at, '') DESC, fetched_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [self._video_dict(r) for r in rows]

    @staticmethod
    def _video_dict(row: sqlite3.Row) -> dict[str, Any]:
        raw = row["tickers_json"] if "tickers_json" in row.keys() else "[]"
        try:
            tickers = json.loads(raw or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            tickers = []
        if not isinstance(tickers, list):
            tickers = []
        return {
            "video_id": str(row["video_id"]),
            "channel_handle": str(row["channel_handle"]) if row["channel_handle"] else None,
            "title": str(row["title"]) if row["title"] else None,
            "url": str(row["url"]) if row["url"] else None,
            "published_at": str(row["published_at"]) if row["published_at"] else None,
            "tickers": [str(t).upper() for t in tickers],
            "fetched_at": str(row["fetched_at"]) if row["fetched_at"] else None,
        }


def idea_to_dict(idea: YoloIdea) -> dict[str, Any]:
    return {
        "id": idea.id,
        "ticker": idea.ticker,
        "score": idea.score,
        "window": idea.window,
        "sample_title": idea.sample_title,
        "url": idea.url,
        "fetched_at": idea.fetched_at.isoformat() if idea.fetched_at else None,
        "mentions_6h": idea.mentions_6h,
        "mentions_24h": idea.mentions_24h,
        "upvote_heat": idea.upvote_heat,
        "comment_heat": idea.comment_heat,
        "is_crypto": idea.is_crypto,
        "source": idea.source,
    }
