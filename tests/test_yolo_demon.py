from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from snowball.config import Settings
from snowball.halt import halt_active
from snowball.snapshot import build_snapshot
from snowball.state import AppState
from snowball.yolo_demon.poller import (
    BACKFILL_META_KEY,
    WATCH_TICKER,
    YoloDemonSidecar,
    encode_backfill_meta,
    parse_backfill_completed,
)
from snowball.yolo_demon.score import score_ticker
from snowball.yolo_demon.store import YoloStore
from snowball.yolo_demon.tickers import extract_tickers, is_crypto
from snowball.yolo_demon.youtube import (
    parse_channel_id,
    parse_channel_info,
    parse_handles,
    parse_playlist_items,
    parse_search,
)
from snowball.yolo_demon.x_api import parse_recent_search


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
T6 = NOW.timestamp() - 2 * 3600


YT_CHANNELS = {
    "thetradingfraternity": {
        "items": [
            {
                "id": "UCtf111",
                "snippet": {"title": "The Trading Fraternity"},
                "contentDetails": {
                    "relatedPlaylists": {"uploads": "UUtf111"}
                },
            }
        ]
    },
    "thestockmarket": {
        "items": [
            {
                "id": "UCsm222",
                "snippet": {"title": "The Stock Market"},
                "contentDetails": {
                    "relatedPlaylists": {"uploads": "UUsm222"}
                },
            }
        ]
    },
    "elliotrades_official": {
        "items": [
            {
                "id": "UCel333",
                "snippet": {"title": "Ellio Trades Official"},
                "contentDetails": {
                    "relatedPlaylists": {"uploads": "UUel333"}
                },
            }
        ]
    },
}

# Recent playlist pages (also used for backfill first page)
YT_PLAYLIST = {
    "UUtf111": {
        "items": [
            {
                "contentDetails": {
                    "videoId": "vidTF1",
                    "videoPublishedAt": "2026-09-02T10:00:00Z",
                },
                "snippet": {
                    "title": "$TSLA breakout watch",
                    "description": "Also NVDA levels",
                    "publishedAt": "2026-09-02T10:00:00Z",
                    "channelId": "UCtf111",
                },
            },
            {
                "contentDetails": {
                    "videoId": "vidTF_noticker",
                    "videoPublishedAt": "2026-09-02T08:30:00Z",
                },
                "snippet": {
                    "title": "Market open recap — no cashtags here",
                    "description": "just vibes",
                    "publishedAt": "2026-09-02T08:30:00Z",
                    "channelId": "UCtf111",
                },
            },
        ]
    },
    "UUsm222": {
        "items": [
            {
                "contentDetails": {
                    "videoId": "vidSM1",
                    "videoPublishedAt": "2026-09-02T09:00:00Z",
                },
                "snippet": {
                    "title": "SPY and QQQ outlook",
                    "description": "market open",
                    "publishedAt": "2026-09-02T09:00:00Z",
                    "channelId": "UCsm222",
                },
            }
        ]
    },
    "UUel333": {
        "items": [
            {
                "contentDetails": {
                    "videoId": "vidEL1",
                    "videoPublishedAt": "2026-03-15T14:00:00Z",
                },
                "snippet": {
                    "title": "$NVDA Ellio levels",
                    "description": "watchlist",
                    "publishedAt": "2026-03-15T14:00:00Z",
                    "channelId": "UCel333",
                },
            }
        ]
    },
}

# Extra page for backfill pagination on TTF
YT_PLAYLIST_PAGE2 = {
    "UUtf111": {
        "items": [
            {
                "contentDetails": {
                    "videoId": "vidTF_old",
                    "videoPublishedAt": "2026-02-01T12:00:00Z",
                },
                "snippet": {
                    "title": "February levels $AMD",
                    "description": "",
                    "publishedAt": "2026-02-01T12:00:00Z",
                    "channelId": "UCtf111",
                },
            },
            {
                "contentDetails": {
                    "videoId": "vidTF_too_old",
                    "videoPublishedAt": "2025-12-01T12:00:00Z",
                },
                "snippet": {
                    "title": "Before backfill window",
                    "description": "",
                    "publishedAt": "2025-12-01T12:00:00Z",
                    "channelId": "UCtf111",
                },
            },
        ]
    }
}

X_SEARCH = {
    "data": [
        {
            "id": "tweet1",
            "text": "$BTC pumping with $ETH",
            "created_at": "2026-09-02T11:00:00Z",
        },
        {
            "id": "tweet2",
            "text": "$TSLA calls printing",
            "created_at": "2026-09-02T11:05:00Z",
        },
    ]
}


class FakeHttp:
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.keyword_searches = 0
        self.playlist_pages: dict[str, int] = {}

    def get_bytes(self, url: str, *, headers=None, timeout: float = 20) -> bytes:
        self.gets.append(url)
        if "old.reddit" in url or url.endswith(".html"):
            raise AssertionError("must not scrape HTML")
        u = urlparse(url)
        qs = parse_qs(u.query)
        host_path = f"{u.netloc}{u.path}"

        if "api.stocktwits.com" in host_path:
            raise AssertionError("StockTwits must not be called")

        if "googleapis.com/youtube/v3/channels" in host_path:
            handle = (qs.get("forHandle") or [""])[0].lower()
            return json.dumps(YT_CHANNELS.get(handle, {"items": []})).encode()

        if "googleapis.com/youtube/v3/playlistItems" in host_path:
            pid = (qs.get("playlistId") or [""])[0]
            token = (qs.get("pageToken") or [""])[0]
            self.playlist_pages[pid] = self.playlist_pages.get(pid, 0) + 1
            if not token:
                payload = dict(YT_PLAYLIST.get(pid, {"items": []}))
                # Offer page 2 for TTF during backfill pagination
                if pid == "UUtf111" and "UUtf111" in YT_PLAYLIST_PAGE2:
                    payload = {**payload, "nextPageToken": "p2"}
                return json.dumps(payload).encode()
            if token == "p2":
                return json.dumps(YT_PLAYLIST_PAGE2.get(pid, {"items": []})).encode()
            return json.dumps({"items": []}).encode()

        if "googleapis.com/youtube/v3/search" in host_path:
            channel_id = (qs.get("channelId") or [""])[0]
            typ = (qs.get("type") or [""])[0]
            if channel_id:
                # legacy search path — should be rare when playlists work
                return json.dumps({"items": []}).encode()
            if typ == "channel":
                return json.dumps({"items": []}).encode()
            self.keyword_searches += 1
            return json.dumps(
                {
                    "items": [
                        {
                            "id": {"videoId": "vidKW1"},
                            "snippet": {
                                "title": "$AMD keyword hit",
                                "description": "noise",
                                "publishedAt": "2026-09-02T08:00:00Z",
                            },
                        },
                        {
                            "id": {"videoId": "vidKW_empty"},
                            "snippet": {
                                "title": "no tickers in this keyword hit",
                                "description": "noise",
                                "publishedAt": "2026-09-02T07:00:00Z",
                            },
                        },
                    ]
                }
            ).encode()

        if "api.x.com" in host_path or "api.twitter.com" in host_path:
            return json.dumps(X_SEARCH).encode()

        raise AssertionError(f"unexpected GET {url}")

    def post_bytes(self, url: str, data: bytes, *, headers=None, timeout: float = 20) -> bytes:
        self.posts.append(url)
        raise AssertionError(f"Yolo Demon must not POST: {url}")



def _mark_backfill_done(store: YoloStore, handles: list[str], since: str = "2026-01-01T00:00:00Z") -> None:
    store.set_meta(BACKFILL_META_KEY, encode_backfill_meta(since, handles))


def test_extract_tickers_cashtags_and_known() -> None:
    got = extract_tickers("$TSLA to the moon", "also NVDA and SPY but not A or I or THE")
    assert "TSLA" in got
    assert "NVDA" in got
    assert "SPY" in got
    assert "A" not in got
    assert "I" not in got
    assert "THE" not in got
    assert is_crypto("BTC")
    assert "BTC" in extract_tickers("I like BTC and $DOGE")


def test_parse_youtube_and_handles() -> None:
    assert parse_handles(None) == [
        "thetradingfraternity",
        "thestockmarket",
        "elliotrades_official",
    ]
    assert parse_handles("thetradingfraternity,thestockmarket") == [
        "thetradingfraternity",
        "thestockmarket",
    ]
    assert parse_handles("") == []
    assert parse_channel_id(YT_CHANNELS["thetradingfraternity"]) == "UCtf111"
    info = parse_channel_info(YT_CHANNELS["thetradingfraternity"])
    assert info and info.uploads_playlist_id == "UUtf111"
    vids, stop = parse_playlist_items(
        YT_PLAYLIST["UUtf111"],
        channel_handle="thetradingfraternity",
        channel_id="UCtf111",
    )
    assert len(vids) == 2
    assert vids[0].channel_handle == "thetradingfraternity"
    assert "TSLA" in vids[0].title
    assert stop is False


def test_parse_x_search() -> None:
    tweets = parse_recent_search(X_SEARCH)
    assert len(tweets) == 2
    assert tweets[0].tweet_id == "tweet1"


def test_poll_youtube_channels(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    store = YoloStore(tmp_path / "y.db")
    # Mark backfill done so this test only covers the recent poll path.
    _mark_backfill_done(store, ["thetradingfraternity", "thestockmarket"])
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
            "youtube_channel_handles": "thetradingfraternity,thestockmarket",
            "youtube_allow_keyword_search": False,
            "youtube_priority_max_results": 20,
            "x_bearer_token": "",
            "x_enabled": False,
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["youtube"] >= 1
    assert http.keyword_searches == 0
    ideas = {(i.source, i.ticker): i for i in store.top_ideas(50)}
    assert ("youtube", "TSLA") in ideas
    yt = ideas[("youtube", "TSLA")]
    assert yt.sample_title and "@thetradingfraternity" in yt.sample_title
    assert yt.url and "youtube.com" in yt.url
    # Priority video without ticker still lands in yolo_videos + WATCH mention
    vids = {v["video_id"]: v for v in store.recent_videos(50)}
    assert "vidTF_noticker" in vids
    assert vids["vidTF_noticker"]["tickers"] == []
    assert ("youtube", WATCH_TICKER) in ideas
    # dedupe second poll
    stats2 = sidecar.poll_once()
    assert stats2["youtube"] == 0


def test_priority_video_without_ticker_in_dashboard_payload(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_state: AppState
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    store = YoloStore(tmp_path / "y2.db")
    _mark_backfill_done(store, ["thetradingfraternity", "thestockmarket"])
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
            "youtube_channel_handles": "thetradingfraternity,thestockmarket",
            "youtube_allow_keyword_search": False,
            "x_enabled": False,
            "x_bearer_token": "",
        }
    )
    sidecar = YoloDemonSidecar(settings, store, http=FakeHttp())
    sidecar.poll_once()
    app_state.settings = settings
    app_state.yolo = store
    app_state.yolo_sidecar = sidecar
    snap = build_snapshot(app_state)
    y = snap["yolo_demon"]
    assert "priority_videos" in y
    ids = {v["video_id"] for v in y["priority_videos"]}
    assert "vidTF_noticker" in ids
    assert "vidTF1" in ids
    # ticker still extracted when present
    tf1 = next(v for v in y["priority_videos"] if v["video_id"] == "vidTF1")
    assert "TSLA" in tf1["tickers"]


def test_keyword_search_still_requires_tickers(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    store = YoloStore(tmp_path / "ykw.db")
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
            "youtube_channel_handles": "",  # force keyword path
            "youtube_allow_keyword_search": True,
            "yolo_youtube_backfill_since": "",  # disable backfill
            "x_enabled": False,
            "x_bearer_token": "",
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert http.keyword_searches >= 1
    assert stats["youtube"] >= 1
    ideas = {(i.source, i.ticker): i for i in store.top_ideas(50)}
    assert ("youtube", "AMD") in ideas
    assert ("youtube", WATCH_TICKER) not in ideas
    assert store.recent_videos(10) == []  # keyword path does not write yolo_videos


def test_backfill_walks_playlist_and_sets_meta(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    # Skip sleep during backfill pagination
    monkeypatch.setattr("snowball.yolo_demon.youtube.time.sleep", lambda *_a, **_k: None)
    store = YoloStore(tmp_path / "ybf.db")
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
            "youtube_channel_handles": "thetradingfraternity,thestockmarket",
            "youtube_allow_keyword_search": False,
            "yolo_youtube_backfill_since": "2026-01-01T00:00:00Z",
            "x_enabled": False,
            "x_bearer_token": "",
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["backfill_videos"] >= 1
    completed = parse_backfill_completed(
        store.get_meta(BACKFILL_META_KEY), "2026-01-01T00:00:00Z"
    )
    assert completed == {"thetradingfraternity", "thestockmarket"}
    counts = store.get_meta("yolo_youtube_backfill_counts") or ""
    assert "thetradingfraternity=" in counts
    assert "thestockmarket=" in counts
    vids = {v["video_id"] for v in store.recent_videos(50)}
    assert "vidTF_old" in vids  # inside window
    assert "vidTF_too_old" not in vids  # before 2026-01-01
    assert "vidTF_noticker" in vids
    # Second poll must not re-walk backfill
    n_gets = len(http.gets)
    sidecar.poll_once()
    # May still do recent playlist polls, but not extra backfill pages for both channels
    assert parse_backfill_completed(
        store.get_meta(BACKFILL_META_KEY), "2026-01-01T00:00:00Z"
    ) == {"thetradingfraternity", "thestockmarket"}
    assert len(http.gets) >= n_gets  # recent poll still happens


def test_missing_keys_skip_cleanly(tmp_settings: Settings, tmp_path: Path) -> None:
    store = YoloStore(tmp_path / "y.db")
    http = FakeHttp()
    sidecar = YoloDemonSidecar(tmp_settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["posts"] == 0
    assert sidecar.disabled_reason == "no_sources_configured"
    assert http.gets == []
    assert http.posts == []
    assert store.top_ideas() == []


def test_x_enabled_false_skips_even_with_bearer(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    store = YoloStore(tmp_path / "y.db")
    settings = tmp_settings.model_copy(
        update={
            "x_bearer_token": "bearer_secret",
            "x_enabled": False,
            "youtube_api_key": "",
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["x"] == 0
    assert not any("api.x.com" in u or "api.twitter.com" in u for u in http.gets)
    assert sidecar.source_status.get("x") == "disabled_by_default"


def test_x_daily_cap_blocks_further_calls(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    store = YoloStore(tmp_path / "y.db")
    settings = tmp_settings.model_copy(
        update={
            "x_bearer_token": "bearer_secret",
            "x_enabled": True,
            "x_daily_max_reads": 2,
            "youtube_api_key": "",
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["x"] >= 1
    assert store.x_reads_today() == 2
    n_gets = len(http.gets)
    stats2 = sidecar.poll_once()
    assert stats2["x"] == 0
    assert len(http.gets) == n_gets
    assert "daily_cap" in (sidecar.source_status.get("x") or "")


def test_yolo_never_orders_or_writes_halt(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("order")
        raise AssertionError("order")

    monkeypatch.setattr("snowball.live.LiveBroker.create_market_order", boom)
    monkeypatch.setattr("snowball.paper.PaperLedger.open_buy", boom)
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    halt = tmp_settings.halt_file
    store = YoloStore(tmp_path / "y.db")
    store.set_meta(BACKFILL_META_KEY, "2026-01-01T00:00:00Z")
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
        }
    )
    sidecar = YoloDemonSidecar(settings, store, http=FakeHttp())
    sidecar.poll_once()
    assert called == []
    assert not halt_active(halt)
    src = Path(__file__).resolve().parents[1] / "snowball" / "yolo_demon" / "poller.py"
    text = src.read_text(encoding="utf-8")
    assert "create_order" not in text
    assert "write_halt" not in text
    assert "open_buy" not in text
    assert "oauth.reddit.com" not in text
    assert "REDDIT_" not in text
    assert "yolo_demon.reddit" not in text
    assert "StockTwitsClient" not in text
    assert "yolo_demon.stocktwits" not in text
    assert not (Path(__file__).resolve().parents[1] / "snowball" / "yolo_demon" / "stocktwits.py").exists()


def test_score_velocity() -> None:
    burst = score_ticker(10, 10, 100, 20)
    flat = score_ticker(3, 12, 100, 20)
    assert burst > flat


def test_source_column_migration(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE yolo_ideas (
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
            UNIQUE(ticker, window)
        );
        INSERT INTO yolo_ideas (ticker, score, window, fetched_at)
        VALUES ('TSLA', 1.0, '6h/24h', '2026-09-02T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    store = YoloStore(db)
    ideas = store.top_ideas()
    assert ideas
    assert ideas[0].source == "unknown"


def test_parse_backfill_completed_legacy_uses_counts() -> None:
    since = "2026-01-01T00:00:00Z"
    # Legacy scalar meta + counts → only listed handles are done
    got = parse_backfill_completed(
        since,
        since,
        "thetradingfraternity=3,thestockmarket=1",
    )
    assert got == {"thetradingfraternity", "thestockmarket"}
    # New handle not in counts → missing (caller backfills only that one)
    assert "elliotrades_official" not in got
    # JSON format
    meta = encode_backfill_meta(since, ["thetradingfraternity", "thestockmarket"])
    assert parse_backfill_completed(meta, since) == {
        "thetradingfraternity",
        "thestockmarket",
    }
    # Different since clears completed
    assert parse_backfill_completed(meta, "2025-01-01T00:00:00Z") == set()


def test_new_handle_backfills_without_force(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a handle should backfill only the new one (no full --force)."""
    monkeypatch.setattr("snowball.yolo_demon.poller.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.score.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.store.utcnow", lambda: NOW)
    monkeypatch.setattr("snowball.yolo_demon.youtube.time.sleep", lambda *_a, **_k: None)
    store = YoloStore(tmp_path / "ynew.db")
    # Simulate prior deploy: two handles already backfilled (legacy meta + counts)
    since = "2026-01-01T00:00:00Z"
    store.set_meta(BACKFILL_META_KEY, since)
    store.set_meta(
        "yolo_youtube_backfill_counts",
        "thestockmarket=1,thetradingfraternity=3",
    )
    settings = tmp_settings.model_copy(
        update={
            "youtube_api_key": "yt_key",
            "youtube_channel_handles": (
                "thetradingfraternity,thestockmarket,elliotrades_official"
            ),
            "youtube_allow_keyword_search": False,
            "yolo_youtube_backfill_since": since,
            "x_enabled": False,
            "x_bearer_token": "",
        }
    )
    http = FakeHttp()
    sidecar = YoloDemonSidecar(settings, store, http=http)
    stats = sidecar.poll_once()
    assert stats["backfill_videos"] >= 1
    completed = parse_backfill_completed(store.get_meta(BACKFILL_META_KEY), since)
    assert "elliotrades_official" in completed
    assert "thetradingfraternity" in completed
    assert "thestockmarket" in completed
    vids = {v["video_id"] for v in store.recent_videos(50)}
    assert "vidEL1" in vids
    # Channel resolves for ellio only during this backfill (old handles skipped)
    channel_gets = [u for u in http.gets if "youtube/v3/channels" in u]
    assert any("elliotrades_official" in u.lower() for u in channel_gets)
    assert not any("thetradingfraternity" in u.lower() for u in channel_gets)
    assert not any("thestockmarket" in u.lower() for u in channel_gets)
