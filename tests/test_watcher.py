
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from snowball.config import Settings
from snowball.halt import halt_active
from snowball.watcher.halt import apply_watcher_halt, matching_halt_event, watcher_halt_flag
from snowball.watcher.poller import WatcherSidecar
from snowball.watcher.rss import parse_rss
from snowball.watcher.store import ResearchEvent, WatcherStore, event_fingerprint
from snowball.watcher.tagger import tag_text, te_event_tags
from snowball.watcher.te import parse_calendar_rows

SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260901a.htm</link>
      <pubDate>Tue, 01 Sep 2026 18:00:00 GMT</pubDate>
      <description>Federal Open Market Committee statement</description>
    </item>
    <item>
      <title>Consumer Price Index</title>
      <link>https://www.federalreserve.gov/cpi</link>
      <pubDate>Mon, 31 Aug 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

TE_ROWS = [
    {
        "Event": "Fed Interest Rate Decision",
        "Date": "2026-09-17T18:00:00",
        "Country": "United States",
        "Importance": 3,
        "Category": "Interest Rate",
        "URL": "https://tradingeconomics.com/united-states/interest-rate",
    },
    {
        "Event": "CPI YoY",
        "Date": "2026-09-10T12:30:00",
        "Country": "United States",
        "Importance": 3,
        "Category": "Inflation Rate",
        "URL": "https://tradingeconomics.com/united-states/inflation-cpi",
    },
]


class FakeHttp:
    def __init__(self, get_map: dict[str, bytes | str] | None = None) -> None:
        self.get_map = get_map or {}
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.default_get: bytes | str | Exception | None = SAMPLE_RSS

    def _lookup(self, url: str) -> bytes:
        for key, val in self.get_map.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val.encode() if isinstance(val, str) else val
        if isinstance(self.default_get, Exception):
            raise self.default_get
        if self.default_get is None:
            raise RuntimeError(f"unexpected GET {url}")
        return self.default_get.encode() if isinstance(self.default_get, str) else self.default_get

    def get_bytes(self, url: str, *, headers=None, timeout: float = 20) -> bytes:
        self.gets.append(url)
        return self._lookup(url)

    def post_bytes(self, url: str, data: bytes, *, headers=None, timeout: float = 20) -> bytes:
        self.posts.append(url)
        raise AssertionError("The Watcher must not POST")


def test_rss_parse_upsert_and_duplicate_fingerprint(tmp_path: Path) -> None:
    items = parse_rss(SAMPLE_RSS)
    assert len(items) == 2
    assert items[0].title == "FOMC statement"
    store = WatcherStore(tmp_path / "snowball.db")
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    ev = ResearchEvent(
        source="fed",
        kind="press",
        title=items[0].title,
        url=items[0].url,
        published_at=items[0].published_at,
        country="united states",
        importance=None,
        tags=tag_text(items[0].title, items[0].summary),
        raw_json=items[0].raw,
        fetched_at=now,
        fingerprint=event_fingerprint("fed", items[0].url, items[0].title, items[0].published_at),
    )
    assert store.upsert(ev) is True
    assert store.upsert(ev) is False
    assert store.count() == 1
    assert "fomc" in store.latest_press(5)[0].tags


def test_te_calendar_parse_to_events() -> None:
    events = parse_calendar_rows(TE_ROWS)
    assert len(events) == 2
    fed = events[0]
    assert fed.source == "tradingeconomics"
    assert fed.kind == "calendar"
    assert fed.importance == 3
    assert "rate_decision" in fed.tags or "fomc" in fed.tags
    assert "cpi" in events[1].tags


def test_missing_te_key_skips_cleanly(tmp_settings: Settings, tmp_path: Path) -> None:
    store = WatcherStore(tmp_path / "w.db")
    http = FakeHttp()
    http.default_get = SAMPLE_RSS
    tmp_settings = tmp_settings.model_copy(
        update={"tradingeconomics_api_key": "", "fred_api_key": "", "watcher_halt_around_fomc": False}
    )
    sidecar = WatcherSidecar(tmp_settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["te_new"] == 0
    assert not any("tradingeconomics.com" in u for u in http.gets)
    assert store.count() >= 1  # RSS still ingested


def test_fomc_window_halt_flag(tmp_path: Path) -> None:
    halt = tmp_path / "HALT"
    flag = watcher_halt_flag(halt)
    now = datetime(2026, 9, 17, 18, 5, tzinfo=timezone.utc)
    ev = ResearchEvent(
        source="tradingeconomics",
        kind="calendar",
        title="Fed Interest Rate Decision",
        url="https://example.test/fed",
        published_at=datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
        country="united states",
        importance=3,
        tags=["fomc", "rate_decision"],
        raw_json={},
        fetched_at=now,
        fingerprint="abc",
    )
    match = matching_halt_event([ev], now, 30, 15)
    assert match is ev
    assert apply_watcher_halt(halt, True, reason="test") == "wrote"
    assert halt_active(halt)
    assert flag.exists()
    # Window ended: The Watcher clears only its own halt
    assert apply_watcher_halt(halt, False) == "cleared"
    assert not halt.exists()
    assert not flag.exists()

    # User HALT is not claimed or deleted
    halt.write_text("user\n", encoding="utf-8")
    assert apply_watcher_halt(halt, True) == "user_halt"
    assert not flag.exists()
    assert apply_watcher_halt(halt, False) == "idle"
    assert halt.exists()
    halt.unlink()


def test_watcher_never_calls_live_broker_or_create_order(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("order")
        raise AssertionError("create_order")

    monkeypatch.setattr("snowball.live.LiveBroker.create_market_order", boom)
    monkeypatch.setattr("snowball.paper.PaperLedger.open_buy", boom)
    store = WatcherStore(tmp_path / "w.db")
    http = FakeHttp()
    sidecar = WatcherSidecar(tmp_settings, store, http=http)
    sidecar.poll_once()
    assert called == []
    assert http.posts == []
    import snowball.watcher.poller as poller_mod

    assert not hasattr(poller_mod, "create_order")
    assert "snowball.live" not in poller_mod.__dict__.get("__name__", "")
    import snowball.watcher.poller as m
    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "create_order" not in src
    assert "open_buy" not in src
    assert "LiveBroker" not in src


def test_one_feed_failure_does_not_raise(tmp_settings: Settings, tmp_path: Path) -> None:
    store = WatcherStore(tmp_path / "w.db")
    http = FakeHttp(get_map={"press_monetary": RuntimeError("net down")})
    sidecar = WatcherSidecar(tmp_settings, store, http=http)
    stats = sidecar.poll_once()  # must not raise
    assert stats["rss_errors"] >= 1
    assert store.count() >= 1


def test_te_event_tags_conservative() -> None:
    tags = te_event_tags("Fed Interest Rate Decision", "Interest Rate")
    assert "rate_decision" in tags
    assert tag_text("World Bank approves loan") == ["worldbank"]


def test_fedwatch_official_source_in_catalog() -> None:
    from snowball.watcher.feeds import CME_FEDWATCH_TOOL_URL, WATCHER_OFFICIAL_SOURCES

    assert "cmegroup.com" in CME_FEDWATCH_TOOL_URL
    assert "cme-fedwatch-tool" in CME_FEDWATCH_TOOL_URL
    assert any(s.source == "cme_fedwatch" for s in WATCHER_OFFICIAL_SOURCES)
    src = next(s for s in WATCHER_OFFICIAL_SOURCES if s.source == "cme_fedwatch")
    assert src.url == CME_FEDWATCH_TOOL_URL
    assert "cme-fedwatch" in src.fetch_path


def test_fedwatch_research_event_attribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from snowball.watcher.fedwatch import research_event_from_summary, SOURCE
    from snowball.watcher.feeds import CME_FEDWATCH_TOOL_URL

    summarized = {
        "next_meeting_date": "2026-09-16",
        "p_hold": 0.55,
        "p_hike": 0.05,
        "p_cut": 0.40,
        "current_target": "4.25-4.50",
    }
    ev = research_event_from_summary(summarized)
    assert ev.source == SOURCE
    assert ev.kind == "fedwatch_probs"
    assert ev.url == CME_FEDWATCH_TOOL_URL
    assert "fedwatch" in ev.tags
    assert ev.raw_json["source_attribution"]["url"] == CME_FEDWATCH_TOOL_URL
    store = WatcherStore(tmp_path / "w.db")
    assert store.upsert(ev) is True
    # Duplicate same fingerprint title/day → no second insert when identical
    assert store.upsert(ev) is False


def test_watcher_poll_includes_fedwatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from snowball.watcher.fedwatch import research_event_from_summary

    settings = Settings(
        _env_file=None,
        watcher_enabled=True,
        watcher_poll_seconds=30,
        watcher_halt_around_fomc=False,
        tradingeconomics_api_key="",
        fred_api_key="",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
    )
    store = WatcherStore(tmp_path / "watcher.db")
    http = FakeHttp()
    sidecar = WatcherSidecar(settings, store, http=http)

    def fake_poll(st):
        summarized = {
            "next_meeting_date": "2026-09-16",
            "p_hold": 0.6,
            "p_hike": 0.1,
            "p_cut": 0.3,
        }
        ev = research_event_from_summary(summarized)
        return 1 if st.upsert(ev) else 0

    monkeypatch.setattr(
        "snowball.watcher.poller.poll_fedwatch_into_store", fake_poll
    )
    # Avoid real RSS network — empty map raises; set default empty channel
    http.default_get = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>"""
    stats = sidecar.poll_once()
    assert "fedwatch_new" in stats
    assert stats["fedwatch_new"] >= 1
