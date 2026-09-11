from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.clerk.http import HttpResponse
from snowball.clerk.index import filings_from_zip, parse_fd_xml, ptr_rows
from snowball.clerk.parse import parse_ptr_text, row_hash
from snowball.clerk.poller import PLACES_ORDERS, ClerkSidecar, clerk_db_path
from snowball.clerk.store import ClerkStore
from snowball.clerk.watchlist import is_watchlist
from snowball.config import Settings

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, routes: dict[str, HttpResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, *, headers=None, timeout: float = 60.0) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        if url not in self.routes:
            return HttpResponse(404, {}, b"", url)
        return self.routes[url]


def _zip_bytes(xml: bytes, name: str = "2026FD.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, xml)
    return buf.getvalue()


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "snowball.db",
        heartbeat_path=tmp_path / "heartbeat",
        stock_enabled=False,
        stock_sqlite_path=tmp_path / "snowball_stocks.db",
        clerk_sqlite_path=tmp_path / "snowball_clerk.db",
        clerk_years="2026",
        clerk_pdf_cap=25,
        clerk_pdf_delay_seconds=0,
        **kwargs,
    )


def test_parse_index_keeps_p_and_skips_non_p() -> None:
    xml = (FIX / "clerk_2026FD.xml").read_bytes()
    rows = parse_fd_xml(xml, default_year=2026)
    assert len(rows) == 5
    ptrs = ptr_rows(rows)
    types = {r["filing_type"] for r in ptrs}
    assert types == {"P"}
    ids = {r["doc_id"] for r in ptrs}
    assert ids == {"20035143", "8222222"}
    assert "20030001" not in ids
    assert "20039999" not in ids
    pelosi = next(r for r in ptrs if r["doc_id"] == "20035143")
    assert pelosi["last"] == "Pelosi"
    assert pelosi["first"] == "Nancy"
    assert pelosi["state_dst"] == "CA11"
    assert pelosi["prefix"] == "Hon."
    assert pelosi["pdf_url"].endswith("/ptr-pdfs/2026/20035143.pdf")
    # BOM-tolerant path used by the zip reader
    zipped = filings_from_zip(_zip_bytes(b"\xef\xbb\xbf" + xml), 2026)
    assert len(ptr_rows(zipped)) == 2


def test_parse_purchase_row_and_missing_ticker() -> None:
    text = (FIX / "clerk_ptr_sample.txt").read_text(encoding="utf-8")
    parsed = parse_ptr_text(text, doc_id="20035143", pdf_url="https://example.test/20035143.pdf")
    assert parsed["member"] == "Hon. Nancy Pelosi"
    assert parsed["district"] == "CA11"
    assert parsed["signature_date"] == "07/03/2026"
    assert parsed["parse_status"] == "parsed"
    rows = parsed["transactions"]
    assert len(rows) == 3

    buy = rows[0]
    assert buy["owner"] == "SP"
    assert buy["ticker"] == "EXPL"
    assert buy["asset_code"] == "ST"
    assert buy["tx_type"] == "P"
    assert buy["tx_date"] == "06/26/2026"
    assert buy["notification_date"] == "07/02/2026"
    assert buy["amount_range"] == "$1,001 - $15,000"
    assert "Example Widget" in buy["asset_name"]
    assert "Purchased shares" in buy["description"]
    assert buy["doc_id"] == "20035143"
    assert "snippet" in buy and "EXPL" in buy["snippet"]

    no_ticker = rows[1]
    assert no_ticker["ticker"] is None
    assert "Private Family Trust" in no_ticker["asset_name"]
    assert no_ticker["asset_code"] == "ST"
    assert no_ticker["tx_type"] == "S"
    assert no_ticker["amount_range"] == "$15,001 - $50,000"

    option = rows[2]
    assert option["owner"] == "JT"
    assert option["ticker"] == "WXYZ"
    assert option["asset_code"] == "OP"
    assert option["tx_type"] == "P"

    digest = row_hash(
        "20035143",
        buy["owner"],
        buy["asset_name"],
        buy["ticker"],
        buy["asset_code"],
        buy["tx_type"],
        buy["tx_date"],
        buy["notification_date"],
        buy["amount_range"],
    )
    assert digest == row_hash(
        "20035143",
        "SP",
        buy["asset_name"],
        "EXPL",
        "ST",
        "P",
        "06/26/2026",
        "07/02/2026",
        "$1,001 - $15,000",
    )


def test_watchlist_matches_pelosi_loosely() -> None:
    assert is_watchlist("Pelosi", "Nancy", "CA11")
    assert is_watchlist("Pelosi", "Nancy", None)
    assert not is_watchlist("Pelosi", "Nancy", "CA12")
    assert is_watchlist("McCaul", "Michael T.")
    assert is_watchlist("DelBene", "Suzan K.")
    assert is_watchlist("Kelly", "Michael")
    assert is_watchlist("Morrison", "Kelly Louise")
    assert not is_watchlist("Smith", "Ada", "TX01")


def test_poll_stores_watchlist_purchase_and_skips_scan_and_non_p(tmp_path: Path) -> None:
    xml = (FIX / "clerk_2026FD.xml").read_bytes()
    sample = (FIX / "clerk_ptr_sample.txt").read_text(encoding="utf-8")
    pdf_url = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035143.pdf"
    index_url = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip"
    http = FakeHttp(
        {
            index_url: HttpResponse(
                200,
                {"ETag": '"abc"', "Last-Modified": "Mon, 07 Sep 2026 13:00:13 GMT"},
                _zip_bytes(xml),
                index_url,
            ),
            pdf_url: HttpResponse(200, {}, b"%PDF-1.4 fake", pdf_url),
        }
    )
    settings = _settings(tmp_path)
    store = ClerkStore(clerk_db_path(settings))
    sidecar = ClerkSidecar(
        settings,
        store,
        http=http,
        extract_text=lambda _data: sample,
        sleep=lambda _s: None,
        now=lambda: NOW,
    )
    stats = sidecar.poll_once(force=True)
    assert stats["ptr"] == 2
    assert stats["pdfs"] == 1
    assert stats["transactions"] == 3
    assert stats["scanned_skip"] >= 1
    assert PLACES_ORDERS is False
    assert sidecar.places_orders is False

    called = [url for url, _headers in http.calls]
    assert pdf_url in called
    assert not any("8222222" in url for url in called)
    assert not any("20030001" in url for url in called)

    rows = store.recent_watchlist(10)
    purchase = next(r for r in rows if r["tx_type"] == "P" and r["ticker"] == "EXPL")
    assert purchase["owner"] == "SP"
    assert purchase["pdf_url"] == pdf_url
    assert purchase["watchlist"] is True

    scanned = store.get_filing("8222222")
    assert scanned is not None
    assert scanned["parse_status"] == "scanned_skip"
    assert scanned["pdf_url"].endswith("/8222222.pdf")
    assert scanned["watchlist"] == 1

    # Dedup: same poll content again does not insert another purchase row.
    stats2 = sidecar.poll_once(force=True)
    assert stats2["transactions"] == 0
    assert store.counts()["transactions"] == 3

    # Conditional GET after first successful zip cache.
    http.routes[index_url] = HttpResponse(304, {}, b"", index_url)
    stats3 = sidecar.poll_once(force=True)
    assert stats3["cached_zip"] == 1
    assert stats3["pdfs"] == 0
    store.close()


def test_clerk_never_imports_live_or_broker() -> None:
    root = Path(__file__).resolve().parents[1] / "snowball" / "clerk"
    text = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    for banned in (
        "snowball.live",
        "snowball.paper",
        "snowball.halt",
        "ccxt",
        "write_halt",
        "create_order",
        "LiveBroker",
        "PaperLedger",
    ):
        assert banned not in text
    assert "places_orders" in text


def test_empty_text_is_scanned_skip() -> None:
    parsed = parse_ptr_text("   \n", doc_id="20000001")
    assert parsed["parse_status"] == "scanned_skip"
    assert parsed["transactions"] == []


def test_clerk_db_path_not_crypto_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = clerk_db_path(settings)
    assert path.name == "snowball_clerk.db"
    assert path != settings.sqlite_path
    assert path != settings.stock_sqlite_path
    with pytest.raises(ValueError):
        ClerkStore(tmp_path / "snowball.db")
