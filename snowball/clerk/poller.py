"""The Clerk sidecar. Official House Clerk PTR logger. Never trades, never writes HALT."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from snowball.clerk.http import HttpClient, UrlLibHttp
from snowball.clerk.index import filings_from_zip, index_url, is_scanned_doc_id, ptr_pdf_url
from snowball.clerk.parse import parse_ptr_text, row_hash
from snowball.clerk.store import ClerkStore
from snowball.clerk.watchlist import is_watchlist, watch_rank, watchlist_labels
from snowball.config import Settings
from snowball.models import utcnow

log = logging.getLogger("snowball.clerk")

MIN_POLL_SECONDS = 21600.0
DEFAULT_PDF_CAP = 25
# Research-only. This sidecar has no order path.
PLACES_ORDERS = False


def clerk_db_path(settings: Settings) -> Path:
    """Isolated DB. Never the crypto ledger or stock paper ledger."""
    raw = Path(settings.clerk_sqlite_path)
    parent = Path(settings.sqlite_path).parent
    default_names = {"./data/snowball_clerk.db", "data/snowball_clerk.db"}
    if str(raw) in default_names or raw.name == "snowball_clerk.db":
        path = parent / "snowball_clerk.db"
    else:
        path = raw
    if path.name in {"snowball.db", "snowball_stocks.db"}:
        path = parent / "snowball_clerk.db"
    try:
        if path.resolve() == Path(settings.sqlite_path).resolve():
            path = parent / "snowball_clerk.db"
        elif path.resolve() == Path(settings.stock_sqlite_path).resolve():
            path = parent / "snowball_clerk.db"
    except OSError:
        pass
    return path


def pdftotext_bytes(data: bytes) -> str:
    if not data or not data.startswith(b"%PDF"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as handle:
        handle.write(data)
        handle.flush()
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", handle.name, "-"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _filing_sort_key(filing_date: str | None) -> tuple[int, int, int]:
    raw = (filing_date or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return (dt.year, dt.month, dt.day)
        except ValueError:
            continue
    return (0, 0, 0)


class ClerkSidecar:
    """Logs House PTR ideas/events only. Does not place orders or write HALT."""

    places_orders = False

    def __init__(
        self,
        settings: Settings,
        store: ClerkStore,
        http: HttpClient | None = None,
        running: Any = None,
        extract_text: Callable[[bytes], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = http or UrlLibHttp()
        self._running = running
        self.extract_text = extract_text or pdftotext_bytes
        self._sleep = sleep or time.sleep
        self._now = now or utcnow
        self.last_error: str | None = None
        self.last_poll_at: datetime | None = None
        self.last_counts: dict[str, int] = {}
        self.places_orders = False

    def _alive(self) -> bool:
        if self._running is None:
            return True
        return bool(getattr(self._running, "running", True))

    def poll_interval(self) -> float:
        return max(MIN_POLL_SECONDS, float(self.settings.clerk_poll_seconds))

    def pdf_cap(self) -> int:
        raw = int(getattr(self.settings, "clerk_pdf_cap", DEFAULT_PDF_CAP) or DEFAULT_PDF_CAP)
        return max(0, raw)

    def years(self) -> list[int]:
        raw = (getattr(self.settings, "clerk_years", "") or "").strip()
        if raw:
            out: list[int] = []
            for part in raw.split(","):
                part = part.strip()
                if part:
                    out.append(int(part))
            return out
        year = self._now().year
        return [year, year - 1]

    def _due(self) -> bool:
        last = self.store.get_meta("last_poll_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        age = (self._now() - dt).total_seconds()
        return age >= self.poll_interval()

    def poll_once(self, *, force: bool = False) -> dict[str, int]:
        if not force and not self._due():
            stats = {
                "skipped": 1,
                "filings": 0,
                "pdfs": 0,
                "transactions": 0,
                "scanned_skip": 0,
            }
            self.last_counts = stats
            last = self.store.get_meta("last_poll_at")
            if last:
                try:
                    self.last_poll_at = datetime.fromisoformat(last)
                except ValueError:
                    self.last_poll_at = None
            self.last_error = self.store.get_meta("last_error") or None
            log.info("Clerk poll skipped; interval not elapsed")
            return stats

        stats = {
            "skipped": 0,
            "years": 0,
            "filings": 0,
            "ptr": 0,
            "pdfs": 0,
            "transactions": 0,
            "scanned_skip": 0,
            "cached_zip": 0,
            "errors": 0,
        }
        errors: list[str] = []
        pending: list[dict[str, Any]] = []
        now = self._now()

        for year in self.years():
            if not self._alive():
                break
            try:
                zip_bytes, cached = self._fetch_zip(year)
                if zip_bytes is None:
                    continue
                if cached:
                    stats["cached_zip"] += 1
                stats["years"] += 1
                rows = filings_from_zip(zip_bytes, year)
            except Exception as exc:  # noqa: BLE001 — research poll must not kill the bot
                log.exception("Clerk index failed", extra={"data": {"year": year}})
                errors.append(f"{year}: {exc}")
                stats["errors"] += 1
                continue
            for row in rows:
                if (row.get("filing_type") or "").strip().upper() != "P":
                    continue
                stats["ptr"] += 1
                doc_id = str(row["doc_id"])
                watch = is_watchlist(row.get("last") or "", row.get("first") or "", row.get("state_dst"))
                rank = watch_rank(row.get("last") or "", row.get("first") or "", row.get("state_dst"))
                scanned = is_scanned_doc_id(doc_id)
                status = "scanned_skip" if scanned else "pending"
                year_s = str(row.get("year") or year)
                pdf_url = row.get("pdf_url") or ptr_pdf_url(year_s, doc_id)
                filing = {
                    "doc_id": doc_id,
                    "prefix": row.get("prefix"),
                    "last": row.get("last"),
                    "first": row.get("first"),
                    "suffix": row.get("suffix"),
                    "filing_type": "P",
                    "state_dst": row.get("state_dst"),
                    "year": year_s,
                    "filing_date": row.get("filing_date"),
                    "pdf_url": pdf_url,
                    "watchlist": watch,
                    "watch_rank": rank,
                    "parse_status": status,
                }
                self.store.upsert_filing(filing)
                stats["filings"] += 1
                stored = self.store.get_filing(doc_id) or {}
                done = (stored.get("parse_status") or "") in {
                    "parsed",
                    "parsed_no_rows",
                    "scanned_skip",
                }
                if scanned:
                    stats["scanned_skip"] += 1
                    continue
                if not done:
                    pending.append(filing)

        pdfs = self._download_pdfs(pending, stats, errors)
        stats["pdfs"] = pdfs

        self.last_poll_at = now
        self.last_counts = stats
        err = "; ".join(errors)[:500] if errors else ""
        self.last_error = err or None
        self.store.set_meta("last_poll_at", now.isoformat())
        self.store.set_meta("last_error", err)
        self.store.set_meta("last_pdfs", str(stats["pdfs"]))
        self.store.set_meta("last_transactions", str(stats["transactions"]))
        log.info(
            "Clerk poll complete",
            extra={"data": {**stats, "watchlist": watchlist_labels()[:3]}},
        )
        return stats

    def _fetch_zip(self, year: int) -> tuple[bytes | None, bool]:
        url = index_url(year)
        cache_path = self.store.cache_dir / f"{year}FD.zip"
        etag = self.store.get_meta(f"zip:{year}:etag")
        modified = self.store.get_meta(f"zip:{year}:last_modified")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        resp = self.http.get(url, headers=headers, timeout=60)
        if resp.status == 304 and cache_path.is_file():
            return cache_path.read_bytes(), True
        if resp.status == 304 and not cache_path.is_file():
            # Lost cache file; refetch without validators.
            resp = self.http.get(url, timeout=60)
        if resp.status != 200 or not resp.body:
            raise RuntimeError(f"index HTTP {resp.status}")
        if not resp.body.startswith(b"PK"):
            raise RuntimeError("index body is not a zip")
        cache_path.write_bytes(resp.body)
        new_etag = resp.header("ETag")
        new_lm = resp.header("Last-Modified")
        if new_etag:
            self.store.set_meta(f"zip:{year}:etag", new_etag)
        if new_lm:
            self.store.set_meta(f"zip:{year}:last_modified", new_lm)
        return resp.body, False

    def _download_pdfs(
        self,
        pending: list[dict[str, Any]],
        stats: dict[str, int],
        errors: list[str],
    ) -> int:
        cap = self.pdf_cap()
        if cap <= 0 or not pending:
            return 0
        ordered = sorted(
            pending,
            key=lambda f: (
                int(f.get("watch_rank", 1 if f.get("watchlist") else 2)),
                tuple(-n for n in _filing_sort_key(f.get("filing_date"))),
            ),
        )
        delay = float(getattr(self.settings, "clerk_pdf_delay_seconds", 0.75) or 0.0)
        downloaded = 0
        for filing in ordered:
            if downloaded >= cap or not self._alive():
                break
            doc_id = str(filing["doc_id"])
            pdf_url = filing.get("pdf_url") or ""
            if not pdf_url:
                continue
            try:
                text, used_cache = self._pdf_text(filing)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Clerk PDF fetch failed",
                    extra={"data": {"doc_id": doc_id, "error": str(exc)}},
                )
                self.store.mark_filing(doc_id, parse_status="error", last_error=str(exc)[:300])
                errors.append(f"{doc_id}: {exc}")
                stats["errors"] += 1
                continue
            if not used_cache:
                downloaded += 1
                if delay > 0 and downloaded < cap:
                    self._sleep(delay)
            self._ingest_text(filing, text, stats)
        return downloaded

    def _pdf_text(self, filing: dict[str, Any]) -> tuple[str, bool]:
        doc_id = str(filing["doc_id"])
        year = str(filing.get("year") or "")
        pdf_path = self.store.cache_dir / "pdfs" / year / f"{doc_id}.pdf"
        if pdf_path.is_file() and pdf_path.stat().st_size > 0:
            data = pdf_path.read_bytes()
            return self.extract_text(data), True
        resp = self.http.get(str(filing["pdf_url"]), timeout=60)
        if resp.status != 200 or not resp.body.startswith(b"%PDF"):
            raise RuntimeError(f"pdf HTTP {resp.status}")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(resp.body)
        return self.extract_text(resp.body), False

    def _ingest_text(self, filing: dict[str, Any], text: str, stats: dict[str, int]) -> None:
        doc_id = str(filing["doc_id"])
        pdf_url = filing.get("pdf_url") or ""
        if not (text or "").strip():
            self.store.mark_filing(
                doc_id,
                parse_status="scanned_skip",
                raw_text_snippet="",
                last_error=None,
            )
            stats["scanned_skip"] += 1
            return
        parsed = parse_ptr_text(text, doc_id=doc_id, pdf_url=pdf_url)
        member = parsed.get("member")
        if not member:
            prefix = (filing.get("prefix") or "").strip()
            name = " ".join(
                p for p in (prefix, filing.get("first") or "", filing.get("last") or "", filing.get("suffix") or "") if p
            )
            member = name or None
        district = parsed.get("district") or filing.get("state_dst")
        signature = parsed.get("signature_date")
        snippet = text.strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500]
        tx_rows = []
        for tx in parsed.get("transactions") or []:
            asset = tx.get("asset_name") or ""
            ticker = tx.get("ticker")
            amount = tx.get("amount_range") or ""
            digest = row_hash(
                doc_id,
                tx.get("owner") or "",
                asset,
                ticker,
                tx.get("asset_code"),
                tx.get("tx_type") or "",
                tx.get("tx_date") or "",
                tx.get("notification_date") or "",
                amount,
            )
            tx_rows.append(
                {
                    **tx,
                    "doc_id": doc_id,
                    "row_hash": digest,
                    "member": tx.get("member") or member,
                    "district": tx.get("district") or district,
                    "filing_date": filing.get("filing_date"),
                    "signature_date": tx.get("signature_date") or signature,
                    "pdf_url": pdf_url,
                    "watchlist": bool(filing.get("watchlist")),
                }
            )
        added = self.store.insert_transactions(tx_rows)
        stats["transactions"] += added
        status = parsed.get("parse_status") or ("parsed" if tx_rows else "parsed_no_rows")
        if status == "scanned_skip":
            status = "parsed_no_rows"
        self.store.mark_filing(
            doc_id,
            parse_status=status,
            member=member,
            district=district,
            signature_date=signature,
            raw_text_snippet=snippet,
            last_error=None,
        )

    def run_forever(self) -> None:
        interval = self.poll_interval()
        log.info(
            "The Clerk loop start",
            extra={
                "data": {
                    "poll_seconds": interval,
                    "places_orders": False,
                    "source": "disclosures-clerk.house.gov",
                }
            },
        )
        while self._alive():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("Clerk tick failed (trading loop unaffected)")
                self.last_error = "tick failed"
            elapsed = time.monotonic() - started
            remaining = interval - elapsed
            deadline = time.monotonic() + max(1.0, remaining)
            while self._alive() and time.monotonic() < deadline:
                self._sleep(0.5)
        log.info("The Clerk stopped")
