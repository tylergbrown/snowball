from __future__ import annotations

import ast
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from snowball.config import Settings
from snowball.earnings.calendar import filter_watchlist, map_session, parse_calendar_payload
from snowball.earnings.http import HttpResponse
from snowball.earnings.momentum import parse_yahoo_closes, post_reaction, pre_momentum
from snowball.earnings.poller import PLACES_ORDERS, EarningsScout, earnings_db_path
from snowball.earnings.store import EarningsStore
from snowball.earnings.watchlist import earnings_watchlist

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, routes: dict[str, HttpResponse]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, *, headers=None, timeout: float = 20.0) -> HttpResponse:
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        for key, resp in self.routes.items():
            if key and key in url:
                return resp
        return HttpResponse(404, {}, b"{}", url)


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
        futures_enabled=False,
        futures_sqlite_path=tmp_path / "snowball_futures.db",
        clerk_enabled=False,
        clerk_sqlite_path=tmp_path / "snowball_clerk.db",
        earnings_enabled=True,
        earnings_poll_seconds=14400,
        earnings_sqlite_path=tmp_path / "snowball_earnings.db",
        **kwargs,
    )


def test_map_session() -> None:
    assert map_session("time-pre-market") == "BMO"
    assert map_session("time-after-hours") == "AMC"
    assert map_session("time-not-supplied") == "UNK"
    assert map_session("weird") == "UNK"


def test_parse_and_filter_watchlist() -> None:
    payload = json.loads((FIX / "nasdaq_earnings_sample.json").read_text())
    events = parse_calendar_payload(payload, report_date="2026-09-10")
    assert {e["ticker"] for e in events} >= {"NVDA", "AAPL", "MSFT", "ORCL", "ZZZZ"}
    nvda = next(e for e in events if e["ticker"] == "NVDA")
    assert nvda["session"] == "AMC"
    assert nvda["eps_forecast"] == pytest.approx(0.75)
    aapl = next(e for e in events if e["ticker"] == "AAPL")
    assert aapl["session"] == "BMO"

    watch = {"NVDA", "AAPL", "MSFT"}
    kept = filter_watchlist(events, watch)
    assert {e["ticker"] for e in kept} == {"NVDA", "AAPL", "MSFT"}
    assert "ORCL" not in {e["ticker"] for e in kept}
    assert "ZZZZ" not in {e["ticker"] for e in kept}


def test_watchlist_includes_static_universe() -> None:
    wl = earnings_watchlist()
    assert "NVDA" in wl and "AAPL" in wl and "MSFT" in wl


def test_pre_momentum_math() -> None:
    payload = json.loads((FIX / "yahoo_chart_aapl_sample.json").read_text())
    closes = parse_yahoo_closes(payload)
    assert len(closes) == 30
    snap = pre_momentum(closes)
    # last vs close 5 sessions earlier (closes[-6])
    assert snap["ret_5d"] == pytest.approx((closes[-1][1] / closes[-6][1]) - 1.0)
    assert snap["ret_20d"] == pytest.approx((closes[-1][1] / closes[-21][1]) - 1.0)
    assert snap["pre_momentum"] == "up"


def test_post_reaction_bmo_and_amc() -> None:
    # Build synthetic closes around report day 2026-09-08
    days = [
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 3),
        date(2026, 9, 4),
        date(2026, 9, 5),
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 12),
        date(2026, 9, 15),
        date(2026, 9, 16),
    ]
    px = [100, 101, 102, 103, 104, 110, 112, 111, 113, 115, 120, 121]
    closes = list(zip(days, px))
    bmo = post_reaction(closes, report_date=date(2026, 9, 8), session="BMO")
    # anchor = Sep 5 close 104; ret_0d = 110/104-1
    assert bmo["anchor_close"] == 104
    assert bmo["ret_0d"] == pytest.approx((110 / 104) - 1)
    assert bmo["ret_1d"] == pytest.approx((112 / 104) - 1)

    amc = post_reaction(closes, report_date=date(2026, 9, 8), session="AMC")
    # anchor = Sep 8 close 110; ret_0d = next day 112/110-1
    assert amc["anchor_close"] == 110
    assert amc["ret_0d"] == pytest.approx((112 / 110) - 1)
    assert amc["ret_1d"] == pytest.approx((111 / 110) - 1)


def test_places_orders_false_and_no_live_imports() -> None:
    assert PLACES_ORDERS is False
    assert EarningsScout.places_orders is False
    root = Path(__file__).resolve().parents[1] / "snowball" / "earnings"
    forbidden = ("snowball.live", "snowball.halt", "create_order", "place_order", "ccxt")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live" not in alias.name
                    assert "halt" not in alias.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in {"snowball.live", "snowball.halt"}
                assert not node.module.startswith("ccxt")
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in ("create_order", "place_order"):
                assert token not in text


def test_upsert_and_poll_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = EarningsStore(earnings_db_path(settings))
    cal = json.loads((FIX / "nasdaq_earnings_sample.json").read_text())
    yahoo = (FIX / "yahoo_chart_aapl_sample.json").read_bytes()
    # Serve same calendar for every date query; yahoo for any chart URL
    routes: dict[str, HttpResponse] = {}
    for off in range(0, 11):
        d = date(2026, 9, 10 + off) if off < 21 else date(2026, 9, 10)
        # Actually timedelta
        from datetime import timedelta

        day = date(2026, 9, 10) + timedelta(days=off)
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
        routes[url] = HttpResponse(200, {}, json.dumps(cal).encode(), url)
    routes["https://query1.finance.yahoo.com/v8/finance/chart/"] = HttpResponse(
        200, {}, yahoo, "yahoo"
    )

    http = FakeHttp(routes)
    scout = EarningsScout(
        settings,
        store,
        http=http,
        now=lambda: NOW,
        sleep=lambda _s: None,
        watchlist={"NVDA", "AAPL", "MSFT"},
    )
    assert scout.places_orders is False
    stats = scout.poll_once(force=True)
    assert stats["upserted"] >= 3
    upcoming = store.upcoming(from_date="2026-09-10", to_date="2026-09-20")
    tickers = {e["ticker"] for e in upcoming}
    assert "NVDA" in tickers and "AAPL" in tickers and "MSFT" in tickers
    assert "ZZZZ" not in tickers
    aapl_rows = [e for e in upcoming if e["ticker"] == "AAPL"]
    assert aapl_rows
    assert aapl_rows[0].get("pre_momentum") in {"up", "down", "flat"}
    store.close()
