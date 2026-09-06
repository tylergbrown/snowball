"""STOCK PAPER lane — isolated book, Yahoo marks, HOT universe."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.dashboard import create_app
from snowball.engine import Engine, build_state
from snowball.models import PairSnapshot, Ticker
from snowball.paper import PaperLedger
from snowball.snapshot import build_snapshot
from snowball.state import AppState
from snowball.stocks.engine import StockPaperEngine, attach_stock_lane
from snowball.stocks.universe import (
    CHIP_AI,
    STATIC_CORE,
    TOP25_QQQ,
    TOP25_SPY,
    build_stock_universe,
    normalize_symbol,
    static_universe,
)


def test_static_universe_includes_index_tops_and_core() -> None:
    u = static_universe()
    for sym in STATIC_CORE:
        assert sym in u
    for sym in CHIP_AI:
        assert sym in u
    for sym in TOP25_SPY:
        assert normalize_symbol(sym) in u
    for sym in TOP25_QQQ:
        assert normalize_symbol(sym) in u
    # Deduped
    assert len(u) == len(set(u))
    # BRK-B normalized
    assert "BRK-B" in u
    assert "BRK.B" not in u


def test_build_universe_caps_active() -> None:
    meta = build_stock_universe(None, max_dynamic=5, max_active=40)
    assert len(meta["active"]) <= 40
    assert meta["active"] == meta["symbols"][:40]
    assert "SPY" in meta["static"]
    assert meta["sources"]["spy_top25"].startswith("stockanalysis.com")


def test_stock_mode_live_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    s = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        stock_enabled=True,
        stock_mode="live",
        stock_sqlite_path=tmp_path / "stocks.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
    )
    with pytest.raises(LiveTradingRefused):
        s.assert_stock_paper_only()


class FakeYahoo:
    mark_source = "yahoo_paper"

    def __init__(self) -> None:
        # Enough bars for SMA 20/50 crossover into ENTER on last bar
        self.closes = [100.0] * 50 + [200.0]

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = self.closes[-limit:]
        step = {"5m": 300_000.0, "15m": 900_000.0, "1d": 86_400_000.0}.get(timeframe, 900_000.0)
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c, c, c, 1.0] for i, c in enumerate(closes)]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=200.0,
            bid=None,
            ask=None,
            ts=datetime.now(timezone.utc),
        )


def test_stock_paper_entry_isolated_from_crypto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="paper",
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_strategies="sma_15m",
        stock_max_active=8,
        stock_dynamic_max=0,
        stock_poll_seconds=0.05,
        products="BTC-USD",
        strategies="sma_15m",
    )
    crypto_ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    state = AppState(settings=settings, ledger=crypto_ledger)
    state.pairs["BTC-USD"] = PairSnapshot(product="BTC-USD", max_open=5)
    attach_stock_lane(state)
    assert state.stock_ledger is not None
    assert state.stock_ledger is not crypto_ledger
    assert state.stock_ledger.cash_usd() == settings.stock_bankroll_usd
    # Force a tiny active universe for the fake market
    state.stock_universe_active = ["AAPL", "MSFT"]
    for p in state.stock_universe_active:
        state.stock_pairs[p] = PairSnapshot(product=p, max_open=settings.stock_max_positions)

    engine = StockPaperEngine(state, market=FakeYahoo())  # type: ignore[arg-type]
    engine.tick()
    assert state.stock_ledger.open_count("AAPL") == 1
    assert state.stock_ledger.open_count("MSFT") == 1
    # Crypto book untouched
    assert crypto_ledger.open_count("BTC-USD") == 0
    assert crypto_ledger.cash_usd() == settings.bankroll_usd


def test_api_stocks_and_dashboard_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="paper",
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_dynamic_max=0,
        stock_max_active=10,
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_stock_lane(state)
    from fastapi.testclient import TestClient

    client = TestClient(create_app(state))
    r = client.get("/api/stocks")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "paper"
    assert body["mark_source"] == "yahoo_paper"
    assert "SPY" in body["universe"]["active"] or "SPY" in body["universe"]["all"]
    page = client.get("/")
    assert b"Stock Paper" in page.content
    assert b"yahoo" in page.content.lower() or b"PAPER ONLY" in page.content
    snap = build_snapshot(state)
    assert "stocks" in snap
    assert snap["stocks"]["enabled"] is True


def test_crypto_live_flags_do_not_enable_stock_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOCK risk context stays paper even when crypto MODE=live."""
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="live",
        live_enabled=True,
        coinbase_api_key="organizations/x/apiKeys/y",
        coinbase_api_secret="-----BEGIN EC PRIVATE KEY-----\nM\n-----END EC PRIVATE KEY-----\n",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="paper",
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_dynamic_max=0,
        stock_max_active=5,
        stock_strategies="sma_15m",
    )
    # Don't call build_state (would construct live broker). Attach stock only.
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_stock_lane(state)
    state.stock_universe_active = ["AAPL"]
    state.stock_pairs["AAPL"] = PairSnapshot(product="AAPL", max_open=5)
    engine = StockPaperEngine(state, market=FakeYahoo())  # type: ignore[arg-type]
    engine.tick()
    assert state.stock_ledger is not None
    assert state.stock_ledger.open_count("AAPL") == 1
