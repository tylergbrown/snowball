from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.config import Settings
from snowball.models import Ticker
from snowball.paper import PaperLedger
from snowball.state import AppState


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    return Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "snowball.db",
        heartbeat_path=tmp_path / "heartbeat",
        dashboard_host="127.0.0.1",
        dashboard_port=8080,
        poll_seconds=0.05,
        products="BTC-USD,SOL-USD,ETH-USD,DOGE-USD",
        stock_enabled=False,
        futures_enabled=False,
        # Isolation tests use synthetic spikes; keep RSI/BB lean-on filters off
        # unless a test explicitly enables them. Production default remains true.
        indicator_filters_enabled=False,
        # Keep strategy list narrow for legacy engine tests.
        strategies="sma_15m,sma_5m",
    )


@pytest.fixture
def ledger(tmp_settings: Settings) -> PaperLedger:
    return PaperLedger(tmp_settings.sqlite_path, tmp_settings.bankroll_usd)


@pytest.fixture
def app_state(tmp_settings: Settings, ledger: PaperLedger) -> AppState:
    from snowball.models import PairSnapshot

    from snowball.watcher.store import WatcherStore
    from snowball.yolo_demon.store import YoloStore

    state = AppState(settings=tmp_settings, ledger=ledger)
    for product in tmp_settings.product_list:
        state.pairs[product] = PairSnapshot(
            product=product, max_open=tmp_settings.max_positions_per_pair
        )
    state.watcher = WatcherStore(tmp_settings.sqlite_path)
    state.yolo = YoloStore(tmp_settings.sqlite_path)
    return state


def make_ticker(product: str, last: float) -> Ticker:
    return Ticker(
        product=product,
        last=last,
        bid=last * 0.9999,
        ask=last * 1.0001,
        ts=datetime.now(timezone.utc),
    )


class FakeMarket:
    def __init__(
        self,
        closes: dict[str, list[float]],
        last: dict[str, float] | None = None,
        ohlcv: dict[tuple[str, str], list[float]] | None = None,
    ) -> None:
        self.closes = closes
        self.last = last or {k: v[-1] for k, v in closes.items()}
        self.ohlcv = ohlcv or {}

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        series = self.ohlcv.get((product, timeframe), self.closes[product])
        closes = series[-limit:]
        rows: list[list[float]] = []
        base_ts = 1_700_000_000_000.0
        step = 300_000.0 if timeframe == "5m" else 900_000.0
        for i, c in enumerate(closes):
            rows.append([base_ts + i * step, c, c, c, c, 1.0])
        return rows

    def fetch_ticker(self, product: str) -> Ticker:
        return make_ticker(product, self.last[product])
