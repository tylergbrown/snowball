"""Future Trader — paper Coinbase perps lane; isolated book; never live."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.dashboard import create_app
from snowball.futures.engine import FuturesPaperEngine, attach_futures_lane
from snowball.futures.market import (
    DEFAULT_FUTURES_PRODUCTS,
    normalize_futures_product,
    to_futures_ccxt_symbol,
)
from snowball.gates import strategy_exit_allowed
from snowball.models import PairSnapshot, Position, Signal, Ticker
from snowball.paper import PaperLedger
from snowball.snapshot import build_snapshot
from snowball.state import AppState
from snowball.strategy import crossover_signal, donchian_breakout_signal


def test_product_symbol_mapping() -> None:
    assert normalize_futures_product("spy-perp-intx") == "SPY-PERP-INTX"
    assert normalize_futures_product("QQQ") == "QQQ-PERP-INTX"
    assert to_futures_ccxt_symbol("SPY-PERP-INTX") == "SPY/USDC:USDC"
    assert to_futures_ccxt_symbol("QQQ-PERP-INTX") == "QQQ/USDC:USDC"
    assert DEFAULT_FUTURES_PRODUCTS == ("SPY-PERP-INTX", "QQQ-PERP-INTX")


def test_sma_1d_signal_on_synthetic_daily() -> None:
    closes = [100.0] * 50 + [200.0]
    assert crossover_signal(closes, 20, 50) is Signal.ENTER
    closes_exit = [200.0] * 50 + [50.0]
    assert crossover_signal(closes_exit, 20, 50) is Signal.EXIT


def test_donchian_1d_signal_on_synthetic_daily() -> None:
    n = 22
    highs = [10.0] * (n - 1) + [100.0]
    lows = [10.0] * n
    closes = [10.0] * (n - 1) + [11.0]
    assert donchian_breakout_signal(closes, highs, lows) is Signal.ENTER


def test_futures_mode_live_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    monkeypatch.delenv("FUTURES_LIVE_ENABLED", raising=False)
    s = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        futures_enabled=True,
        futures_mode="live",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "futures.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s.assert_futures_paper_only()
    assert s.futures_live_orders_permitted() is False


def test_futures_live_enabled_alone_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    monkeypatch.delenv("FUTURES_LIVE_ENABLED", raising=False)
    s = Settings(
        _env_file=None,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=True,
        futures_sqlite_path=tmp_path / "futures.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s.assert_futures_paper_only()


def test_never_sell_red_blocks_red_exit() -> None:
    lot = Position(
        id=1,
        product="SPY-PERP-INTX",
        side="long",
        qty=0.1,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        status="open",
        strategy="sma_1d",
    )
    ok, reason = strategy_exit_allowed(
        lot, mark=95.0, min_take_profit_pct=0.05, never_sell_red=True
    )
    assert ok is False
    assert reason == "never_sell_red"
    ok2, _ = strategy_exit_allowed(
        lot, mark=106.0, min_take_profit_pct=0.05, never_sell_red=True
    )
    assert ok2 is True


class FakeFuturesMarket:
    mark_source = "coinbase_perp"

    def __init__(self) -> None:
        self.closes = [100.0] * 50 + [200.0]
        self.last = 200.0

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = self.closes[-limit:]
        step = 86_400_000.0
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(closes)]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=self.last,
            bid=self.last - 0.1,
            ask=self.last + 0.1,
            ts=datetime.now(timezone.utc),
        )


def test_paper_open_close_respects_never_sell_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = Settings(
        _env_file=None,
        mode="live",  # crypto live must not enable futures live
        live_enabled=True,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "futures.db",
        futures_strategies="sma_1d",
        futures_products="SPY-PERP-INTX,QQQ-PERP-INTX",
        futures_poll_seconds=0.05,
        never_sell_red=True,
        min_take_profit_pct=0.05,
        entry_cooldown_1d_seconds=0,
        trend_filter_enabled=False,
        slippage_bps=0.0,
    )
    crypto_ledger = PaperLedger(settings.sqlite_path, settings.bankroll_usd)
    state = AppState(settings=settings, ledger=crypto_ledger)
    attach_futures_lane(state)
    assert state.futures_ledger is not None
    assert state.futures_ledger is not crypto_ledger
    assert state.futures_ledger.cash_usd() == settings.futures_bankroll_usd

    market = FakeFuturesMarket()
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    engine.tick()
    assert state.futures_ledger.open_count("SPY-PERP-INTX") == 1
    assert state.futures_ledger.open_count("QQQ-PERP-INTX") == 1
    assert crypto_ledger.open_count("BTC-USD") == 0

    # Force EXIT signal while underwater — must not close (never sell red)
    lot = state.futures_ledger.open_positions("SPY-PERP-INTX")[0]
    snap = state.futures_pairs["SPY-PERP-INTX"]
    snap.signal_1d = Signal.EXIT.value
    snap.signal = Signal.EXIT.value
    market.last = lot.entry_price * 0.9  # red
    market.closes = [200.0] * 50 + [50.0]  # death cross series
    engine.tick()
    assert state.futures_ledger.open_count("SPY-PERP-INTX") == 1

    # Green enough (≥5%) + EXIT → close
    market.last = lot.entry_price * 1.06
    snap.last = market.last
    snap.signal_1d = Signal.EXIT.value
    snap.sma_fast_1d = 90.0
    snap.sma_slow_1d = 100.0  # death cross state for display
    engine._act_on_strategy(
        product="SPY-PERP-INTX",
        strategy_id="sma_1d",
        now=datetime.now(timezone.utc),
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={"SPY-PERP-INTX": market.last, "QQQ-PERP-INTX": market.last},
    )
    assert state.futures_ledger.open_count("SPY-PERP-INTX") == 0


def test_futures_live_orders_refused_when_mode_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = Settings(
        _env_file=None,
        mode="live",
        live_enabled=True,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "futures.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
    )
    assert settings.futures_live_orders_permitted() is False
    settings.assert_futures_paper_only()  # must not raise
    # Engine open path raises if somehow mode flipped mid-flight
    settings2 = settings.model_copy(update={"futures_mode": "live"})
    state = AppState(
        settings=settings2, ledger=PaperLedger(tmp_path / "c2.db", 1000.0)
    )
    state.futures_ledger = PaperLedger(tmp_path / "f2.db", 1000.0)
    state.futures_pairs["SPY-PERP-INTX"] = PairSnapshot(
        product="SPY-PERP-INTX", last=100.0, max_open=5
    )
    engine = FuturesPaperEngine(state, market=FakeFuturesMarket())  # type: ignore[arg-type]
    with pytest.raises(LiveTradingRefused):
        engine._open_lot(
            "SPY-PERP-INTX",
            {"SPY-PERP-INTX": 100.0},
            reason="sma_1d:enter",
            now=datetime.now(timezone.utc),
            strategy="sma_1d",
        )


def test_futures_engine_does_not_import_live_broker() -> None:
    """AST check: futures package must not import snowball.live / LiveBroker."""
    root = Path(__file__).resolve().parents[1] / "snowball" / "futures"
    for py in root.glob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live" not in alias.name.split("."), py.name
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "snowball.live", py.name
                assert not mod.startswith("snowball.live."), py.name
                for alias in node.names:
                    assert alias.name != "LiveBroker", py.name


def test_api_futures_and_dashboard_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=True,
        futures_mode="paper",
        futures_sqlite_path=tmp_path / "futures.db",
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    from fastapi.testclient import TestClient

    client = TestClient(create_app(state))
    r = client.get("/api/futures")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "paper"
    assert body["mark_source"] == "coinbase_perp"
    assert "SPY-PERP-INTX" in body["products"]
    assert "QQQ-PERP-INTX" in body["products"]
    page = client.get("/")
    assert b"Future Trader" in page.content
    snap = build_snapshot(state)
    assert "futures" in snap
    assert snap["futures"]["enabled"] is True


def test_futures_defaults_daily_only_not_on_crypto() -> None:
    s = Settings(_env_file=None)
    assert "sma_1d" in s.futures_strategy_list
    assert "donchian_1d" in s.futures_strategy_list
    assert "ema_15m" not in s.futures_strategy_list
    assert "sma_1d" not in s.strategy_list
    assert "donchian_1d" not in s.strategy_list
    assert s.futures_mode == "paper"
    assert s.futures_live_enabled is False
    assert s.futures_live_orders_permitted() is False
