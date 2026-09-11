"""Own sqlite for The Clerk. Never opens the crypto or stock paper ledgers."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowball.models import utcnow

FORBIDDEN_DB_NAMES = {"snowball.db", "snowball_stocks.db"}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ClerkStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        if self._path.name in FORBIDDEN_DB_NAMES:
            raise ValueError(
                f"The Clerk refuses ledger database {self._path.name}; "
                "use data/snowball_clerk.db"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self._path.parent / "clerk_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clerk_filings (
                doc_id TEXT PRIMARY KEY,
                prefix TEXT,
                last TEXT,
                first TEXT,
                suffix TEXT,
                filing_type TEXT,
                state_dst TEXT,
                year TEXT,
                filing_date TEXT,
                pdf_url TEXT,
                watchlist INTEGER NOT NULL DEFAULT 0,
                parse_status TEXT,
                member TEXT,
                district TEXT,
                signature_date TEXT,
                raw_text_snippet TEXT,
                fetched_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_clerk_filings_watch
                ON clerk_filings(watchlist, filing_date);
            CREATE TABLE IF NOT EXISTS clerk_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                member TEXT,
                district TEXT,
                owner TEXT,
                asset_name TEXT,
                ticker TEXT,
                asset_code TEXT,
                tx_type TEXT,
                partial INTEGER NOT NULL DEFAULT 0,
                tx_date TEXT,
                notification_date TEXT,
                amount_range TEXT,
                description TEXT,
                filing_date TEXT,
                signature_date TEXT,
                pdf_url TEXT,
                watchlist INTEGER NOT NULL DEFAULT 0,
                snippet TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(doc_id, row_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_clerk_tx_watch
                ON clerk_transactions(watchlist, tx_date);
            CREATE TABLE IF NOT EXISTS clerk_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM clerk_meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO clerk_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def get_filing(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clerk_filings WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_filing(self, row: dict[str, Any]) -> None:
        """Insert or refresh an index row. Do not clobber a finished parse_status."""
        now = utcnow().isoformat()
        doc_id = str(row["doc_id"])
        with self._lock:
            existing = self._conn.execute(
                "SELECT parse_status FROM clerk_filings WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            status = row.get("parse_status") or "pending"
            snippet = row.get("raw_text_snippet")
            signature = row.get("signature_date")
            member = row.get("member")
            district = row.get("district")
            last_error = row.get("last_error")
            if existing is not None:
                keep = existing["parse_status"] or ""
                finished = {"parsed", "parsed_no_rows", "scanned_skip"}
                if keep in finished and status in {"pending", "scanned_skip"}:
                    # Never downgrade a finished parse on a later index refresh.
                    status = keep
                    snippet = None
                    signature = None
                    member = None
                    district = None
                    last_error = None
                self._conn.execute(
                    """
                    UPDATE clerk_filings SET
                        prefix=?,
                        last=?,
                        first=?,
                        suffix=?,
                        filing_type=?,
                        state_dst=?,
                        year=?,
                        filing_date=?,
                        pdf_url=?,
                        watchlist=?,
                        parse_status=?,
                        member=COALESCE(?, member),
                        district=COALESCE(?, district),
                        signature_date=COALESCE(?, signature_date),
                        raw_text_snippet=COALESCE(?, raw_text_snippet),
                        last_error=COALESCE(?, last_error)
                    WHERE doc_id=?
                    """,
                    (
                        row.get("prefix"),
                        row.get("last"),
                        row.get("first"),
                        row.get("suffix"),
                        row.get("filing_type") or "P",
                        row.get("state_dst"),
                        row.get("year"),
                        row.get("filing_date"),
                        row.get("pdf_url"),
                        1 if row.get("watchlist") else 0,
                        status,
                        member,
                        district,
                        signature,
                        snippet,
                        last_error,
                        doc_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO clerk_filings (
                        doc_id, prefix, last, first, suffix, filing_type, state_dst,
                        year, filing_date, pdf_url, watchlist, parse_status,
                        member, district, signature_date, raw_text_snippet,
                        fetched_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        row.get("prefix"),
                        row.get("last"),
                        row.get("first"),
                        row.get("suffix"),
                        row.get("filing_type") or "P",
                        row.get("state_dst"),
                        row.get("year"),
                        row.get("filing_date"),
                        row.get("pdf_url"),
                        1 if row.get("watchlist") else 0,
                        status,
                        member,
                        district,
                        signature,
                        snippet,
                        now,
                        last_error,
                    ),
                )
            self._conn.commit()

    def mark_filing(
        self,
        doc_id: str,
        *,
        parse_status: str,
        member: str | None = None,
        district: str | None = None,
        signature_date: str | None = None,
        raw_text_snippet: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE clerk_filings SET
                    parse_status=?,
                    member=COALESCE(?, member),
                    district=COALESCE(?, district),
                    signature_date=COALESCE(?, signature_date),
                    raw_text_snippet=COALESCE(?, raw_text_snippet),
                    fetched_at=?,
                    last_error=?
                WHERE doc_id=?
                """,
                (
                    parse_status,
                    member,
                    district,
                    signature_date,
                    raw_text_snippet,
                    now,
                    last_error,
                    doc_id,
                ),
            )
            self._conn.commit()

    def insert_transactions(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow().isoformat()
        added = 0
        with self._lock:
            for row in rows:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO clerk_transactions (
                        doc_id, row_hash, member, district, owner, asset_name, ticker,
                        asset_code, tx_type, partial, tx_date, notification_date,
                        amount_range, description, filing_date, signature_date,
                        pdf_url, watchlist, snippet, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("doc_id"),
                        row.get("row_hash"),
                        row.get("member"),
                        row.get("district"),
                        row.get("owner"),
                        row.get("asset_name"),
                        row.get("ticker"),
                        row.get("asset_code"),
                        row.get("tx_type"),
                        1 if row.get("partial") else 0,
                        row.get("tx_date"),
                        row.get("notification_date"),
                        row.get("amount_range"),
                        row.get("description"),
                        row.get("filing_date"),
                        row.get("signature_date"),
                        row.get("pdf_url"),
                        1 if row.get("watchlist") else 0,
                        row.get("snippet"),
                        now,
                    ),
                )
                added += int(cur.rowcount or 0)
            self._conn.commit()
        return added

    def recent_watchlist(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM clerk_transactions
                WHERE watchlist = 1
                ORDER BY id DESC
                LIMIT 500
                """
            ).fetchall()
        parsed = [tx_to_dict(dict(r)) for r in rows]

        def _key(row: dict[str, Any]) -> tuple[int, int, int, int]:
            raw = str(row.get("tx_date") or row.get("filing_date") or "")
            y = m = d = 0
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    y, m, d = dt.year, dt.month, dt.day
                    break
                except ValueError:
                    continue
            return (y, m, d, int(row.get("id") or 0))

        parsed.sort(key=_key, reverse=True)
        return parsed[: int(limit)]

    def counts(self) -> dict[str, int]:
        with self._lock:
            filings = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM clerk_filings").fetchone()["n"]
            )
            txns = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM clerk_transactions").fetchone()["n"]
            )
            w_filings = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM clerk_filings WHERE watchlist = 1"
                ).fetchone()["n"]
            )
            w_tx = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM clerk_transactions WHERE watchlist = 1"
                ).fetchone()["n"]
            )
            scanned = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM clerk_filings WHERE parse_status = 'scanned_skip'"
                ).fetchone()["n"]
            )
            return {
                "filings": filings,
                "transactions": txns,
                "watchlist_filings": w_filings,
                "watchlist_transactions": w_tx,
                "scanned_skip": scanned,
            }


def tx_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "doc_id": row.get("doc_id"),
        "member": row.get("member"),
        "district": row.get("district"),
        "owner": row.get("owner"),
        "asset_name": row.get("asset_name"),
        "ticker": row.get("ticker"),
        "asset_code": row.get("asset_code"),
        "tx_type": row.get("tx_type"),
        "partial": bool(row.get("partial")),
        "tx_date": row.get("tx_date"),
        "notification_date": row.get("notification_date"),
        "amount_range": row.get("amount_range"),
        "description": row.get("description"),
        "filing_date": row.get("filing_date"),
        "signature_date": row.get("signature_date"),
        "pdf_url": row.get("pdf_url"),
        "watchlist": bool(row.get("watchlist")),
        "snippet": row.get("snippet"),
    }


def parse_meta_ts(value: str | None) -> datetime | None:
    return _parse_ts(value)
