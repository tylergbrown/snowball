from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
        self._init_transcript_schema()

    def _init_transcript_schema(self) -> None:
        """2026-09-30: transcript text + status per video (ok/none/blocked/error)."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS yolo_transcripts (
                video_id TEXT PRIMARY KEY,
                channel_handle TEXT,
                status TEXT NOT NULL,
                language TEXT,
                is_generated INTEGER,
                chars INTEGER NOT NULL DEFAULT 0,
                text TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                tickers_json TEXT NOT NULL DEFAULT '[]',
                ideas_json TEXT NOT NULL DEFAULT '[]',
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_yolo_transcripts_retry
                ON yolo_transcripts(status, next_retry_at);
            """
        )
        # 2026-10-03 fallback chain (api -> yt-dlp subs -> Whisper): which method produced the
        # text, Whisper queue state, video duration, and whether ``text`` is the full transcript.
        cols = self._table_cols("yolo_transcripts")
        for name, ddl in (("method", "TEXT"), ("whisper_state", "TEXT"), ("duration_sec", "REAL"),
                          ("full_text", "INTEGER"), ("whisper_after", "TEXT")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE yolo_transcripts ADD COLUMN {name} {ddl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_yolo_transcripts_whisper ON yolo_transcripts(whisper_state)"
        )
        self._conn.commit()

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
        merged = [t.upper() for t in (tickers or [])]
        with self._lock:
            # keep transcript-derived tickers when the poll re-upserts title tickers
            for t in self._transcript_tickers(video_id):
                if t not in merged:
                    merged.append(t)
            tickers_json = json.dumps(merged)
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

    # --- transcripts (2026-09-30) ------------------------------------------

    def _transcript_tickers(self, video_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT tickers_json FROM yolo_transcripts WHERE video_id = ? AND status = 'ok'",
            (video_id,),
        ).fetchone()
        if not row:
            return []
        try:
            v = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            return []
        return [str(t).upper() for t in v] if isinstance(v, list) else []

    def transcript_tickers(self, video_id: str) -> list[str]:
        with self._lock:
            return self._transcript_tickers(video_id)

    def save_transcript(
        self,
        *,
        video_id: str,
        channel_handle: str | None,
        status: str,
        text: str | None,
        full_chars: int,
        language: str | None,
        is_generated: bool | None,
        error: str | None,
        attempts: int,
        next_retry_at: str | None,
        ideas: list[dict[str, Any]],
        fetched_at: datetime,
        method: str | None = None,
        whisper_state: str | None = None,
        duration_sec: float | None = None,
    ) -> None:
        tickers = [str(i.get("ticker")).upper() for i in ideas if i.get("ticker")]
        full = None
        if text is not None:
            full = int(len(text) >= int(full_chars))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO yolo_transcripts
                    (video_id, channel_handle, status, language, is_generated, chars, text, error,
                     attempts, next_retry_at, tickers_json, ideas_json, fetched_at,
                     method, whisper_state, duration_sec, full_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    channel_handle=excluded.channel_handle, status=excluded.status,
                    language=excluded.language, is_generated=excluded.is_generated,
                    chars=excluded.chars,
                    text=COALESCE(excluded.text, yolo_transcripts.text),
                    error=excluded.error, attempts=excluded.attempts,
                    next_retry_at=excluded.next_retry_at,
                    tickers_json=excluded.tickers_json, ideas_json=excluded.ideas_json,
                    fetched_at=excluded.fetched_at,
                    method=COALESCE(excluded.method, yolo_transcripts.method),
                    whisper_state=excluded.whisper_state,
                    duration_sec=COALESCE(excluded.duration_sec, yolo_transcripts.duration_sec),
                    full_text=COALESCE(excluded.full_text, yolo_transcripts.full_text)
                """,
                (
                    video_id, channel_handle, status, language,
                    None if is_generated is None else int(bool(is_generated)),
                    int(full_chars), text, error, int(attempts), next_retry_at,
                    json.dumps(tickers), json.dumps(ideas), fetched_at.isoformat(),
                    method, whisper_state, duration_sec, full,
                ),
            )
            self._conn.commit()

    def defer_refill(self, video_id: str, next_retry_at: str, note: str | None) -> None:
        """A full-text re-fetch of an already-ok (trimmed) transcript failed: keep the ok row,
        try again later. Never downgrades an ok transcript."""
        with self._lock:
            self._conn.execute(
                "UPDATE yolo_transcripts SET next_retry_at = ?, error = ? WHERE video_id = ? AND status = 'ok'",
                (next_retry_at, (note or "")[:300] or None, video_id),
            )
            self._conn.commit()

    def whisper_next(self, limit: int = 1, now: datetime | None = None, long_sec: float = 7200.0,
                     fresh_hours: float = 48.0) -> list[dict[str, Any]]:
        """Whisper queue, newest first in three tiers: (1) published in the last ``fresh_hours``
        (fresh signal, any length), (2) normal-length videos, (3) long livestreams
        (> ``long_sec``). Rows waiting on ``whisper_after`` are skipped."""
        now = now or utcnow()
        fresh = (now - timedelta(hours=float(fresh_hours))).isoformat()[:19]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.video_id, t.channel_handle, t.attempts, t.duration_sec, v.published_at
                FROM yolo_transcripts t LEFT JOIN yolo_videos v ON v.video_id = t.video_id
                WHERE t.whisper_state = 'queued' AND t.status != 'ok'
                  AND (t.whisper_after IS NULL OR t.whisper_after <= ?)
                ORDER BY (substr(COALESCE(v.published_at, ''), 1, 19) >= ?) DESC,
                         (COALESCE(t.duration_sec, 0) > ?) ASC,
                         COALESCE(v.published_at, '') DESC
                LIMIT ?
                """,
                (now.isoformat(), fresh, float(long_sec), max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_whisper_state(self, video_id: str, state: str, error: str | None = None,
                          after: str | None = None, duration_sec: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE yolo_transcripts SET whisper_state = ?, error = COALESCE(?, error),
                          whisper_after = ?, duration_sec = COALESCE(?, duration_sec)
                   WHERE video_id = ?""",
                (state, None if error is None else error[:300], after, duration_sec, video_id))
            self._conn.commit()

    def queue_whisper(self, *, video_id: str, channel_handle: str | None, error: str,
                      fetched_at: datetime) -> None:
        """Caption methods all paused: hand a not-yet-transcribed video straight to Whisper."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO yolo_transcripts (video_id, channel_handle, status, chars, error, attempts,
                                              next_retry_at, fetched_at, whisper_state)
                VALUES (?, ?, 'blocked', 0, ?, 0, NULL, ?, 'queued')
                ON CONFLICT(video_id) DO UPDATE SET
                    error = excluded.error, next_retry_at = NULL, whisper_state = 'queued'
                WHERE yolo_transcripts.status != 'ok'
                """,
                (video_id, channel_handle, error[:300], fetched_at.isoformat()),
            )
            self._conn.commit()

    def set_attempts(self, video_id: str, attempts: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE yolo_transcripts SET attempts = ? WHERE video_id = ?",
                               (int(attempts), video_id))
            self._conn.commit()

    def transcript_method_counts(self) -> dict[str, Any]:
        with self._lock:
            ok = self._conn.execute(
                """SELECT COALESCE(method, 'api') AS m, COUNT(*) AS n,
                          SUM(CASE WHEN COALESCE(full_text, 0) = 1 OR chars <= length(COALESCE(text, ''))
                                   THEN 1 ELSE 0 END) AS full_n
                   FROM yolo_transcripts WHERE status = 'ok' GROUP BY m"""
            ).fetchall()
            wq = self._conn.execute(
                "SELECT whisper_state AS s, COUNT(*) AS n FROM yolo_transcripts "
                "WHERE whisper_state IS NOT NULL GROUP BY s"
            ).fetchall()
        return {
            "ok_by_method": {str(r["m"]): int(r["n"]) for r in ok},
            "ok_full_text": sum(int(r["full_n"] or 0) for r in ok),
            "whisper": {str(r["s"]): int(r["n"]) for r in wq},
        }

    def transcripts_due(self, limit: int = 20, now: datetime | None = None,
                        refill_trimmed: bool = False) -> list[dict[str, Any]]:
        """Due videos newest first (never fetched + due retries); then, when ``refill_trimmed``,
        ok transcripts stored trimmed (pre-2026-10-03 40k cap) for a full-text re-fetch."""
        now = now or utcnow()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT v.video_id, v.channel_handle, v.published_at, t.status, t.attempts
                FROM yolo_videos v
                LEFT JOIN yolo_transcripts t ON t.video_id = v.video_id
                WHERE t.video_id IS NULL
                   OR (t.status != 'ok' AND t.next_retry_at IS NOT NULL AND t.next_retry_at <= ?)
                   OR (? AND t.status = 'ok' AND COALESCE(t.full_text, 0) = 0
                       AND t.chars > length(COALESCE(t.text, ''))
                       AND (t.next_retry_at IS NULL OR t.next_retry_at <= ?))
                ORDER BY (COALESCE(t.status, '') = 'ok') ASC, COALESCE(v.published_at, '') DESC
                LIMIT ?
                """,
                (now.isoformat(), 1 if refill_trimmed else 0, now.isoformat(), max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in rows]

    def transcript_status_counts(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT COALESCE(v.channel_handle, '?') AS h, COALESCE(t.status, 'pending') AS s,
                       COUNT(*) AS n
                FROM yolo_videos v LEFT JOIN yolo_transcripts t ON t.video_id = v.video_id
                GROUP BY h, s
                """
            ).fetchall()
        out: dict[str, Any] = {"total": {}}
        for r in rows:
            out.setdefault(str(r["h"]), {})[str(r["s"])] = int(r["n"])
            out["total"][str(r["s"])] = out["total"].get(str(r["s"]), 0) + int(r["n"])
        return out

    def transcript_ideas(self, video_id: str) -> list[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT ideas_json FROM yolo_transcripts WHERE video_id = ?", (video_id,)
            ).fetchone()
        try:
            v = json.loads(row[0]) if row else []
        except (TypeError, ValueError):
            v = []
        return v if isinstance(v, list) else []

    def merge_transcript_tickers(
        self, video_id: str, tickers: list[str], ideas: dict[str, Any], now: datetime
    ) -> int:
        """Union transcript tickers into yolo_videos + same-schema youtube mentions.

        Mentions only for videos inside the 36h mention window (older ones would be
        pruned anyway). One mention per (video, ticker): no double counting.
        Returns number of NEW mentions.
        """
        if not tickers:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM yolo_videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            if row is None:
                return 0
            try:
                cur = json.loads(row["tickers_json"] or "[]")
            except (TypeError, ValueError):
                cur = []
            merged = [str(t).upper() for t in cur] if isinstance(cur, list) else []
            for t in tickers:
                if t.upper() not in merged:
                    merged.append(t.upper())
            self._conn.execute(
                "UPDATE yolo_videos SET tickers_json = ? WHERE video_id = ?",
                (json.dumps(merged), video_id),
            )
            self._conn.commit()
            published = _parse_ts(row["published_at"].replace("Z", "+00:00")) if row["published_at"] else None
        if published is None or (now - published).total_seconds() > 36 * 3600:
            return 0
        handle = row["channel_handle"] or "unknown"
        new = 0
        for t in tickers:
            idea = ideas.get(t)
            stance = getattr(idea, "stance", None) or "neutral"
            title = f"@{handle} [transcript {stance}]: {row['title'] or ''}"[:300]
            if self.upsert_mention(
                ticker=t, post_id=f"yt-{video_id}", created_utc=published.timestamp(),
                ups=0, comments=0, title=title, url=row["url"], subreddit=handle,
                fetched_at=now, source="youtube",
            ):
                new += 1
        with self._lock:
            # real tickers found: drop the WATCH placeholder for this video
            self._conn.execute(
                "DELETE FROM yolo_mentions WHERE post_id = ? AND ticker = 'WATCH'",
                (f"yt-{video_id}",),
            )
            self._conn.commit()
        return new

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


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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
