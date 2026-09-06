"""Unit tests for scale-in gate and trend filter."""

from __future__ import annotations

from datetime import datetime, timezone

from snowball.engine import Engine
from snowball.gates import momentum_fading, scale_in_allowed, trend_filter_allows
from snowball.models import Position
from snowball.state import AppState
from tests.conftest import FakeMarket

GOLDEN = [100.0] * 50 + [200.0]
FLAT = [100.0] * 60
PAIRS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


def _lot(entry: float, pid: int = 1) -> Position:
    return Position(
        id=pid,
        product="BTC-USD",
        side="long",
        qty=1.0,
        entry_price=entry,
        notional_usd=100.0,
        opened_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        status="open",
        strategy="sma_15m",
    )


def test_scale_in_blocked_when_not_green() -> None:
    ok, reason = scale_in_allowed([_lot(100.0)], mark=100.4, min_profit_pct=0.005)
    assert ok is False
    assert reason == "scale_in_not_green"


def test_scale_in_allowed_when_green() -> None:
    ok, reason = scale_in_allowed([_lot(100.0)], mark=100.6, min_profit_pct=0.005)
    assert ok is True
    assert reason == "ok"


def test_scale_in_no_lots_is_ok() -> None:
    ok, reason = scale_in_allowed([], mark=100.0, min_profit_pct=0.005)
    assert ok is True


def test_trend_filter_blocks_below_sma() -> None:
    ok, reason = trend_filter_allows(99.0, 100.0, enabled=True)
    assert ok is False
    assert reason == "trend_below_sma_slow"


def test_trend_filter_blocks_missing_sma() -> None:
    ok, reason = trend_filter_allows(100.0, None, enabled=True)
    assert ok is False
    assert reason == "trend_sma_missing"


def test_trend_filter_allows_above() -> None:
    ok, reason = trend_filter_allows(101.0, 100.0, enabled=True)
    assert ok is True


def test_trend_filter_disabled_passes() -> None:
    ok, reason = trend_filter_allows(50.0, None, enabled=False)
    assert ok is True


def test_engine_blocks_scale_in_when_flat(app_state: AppState) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "entry_cooldown_seconds": 0,
            "scale_in_min_profit_pct": 0.005,
            "trend_filter_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    engine = Engine(app_state, market)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    # Same mark as entry → not green enough for scale-in
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_engine_allows_scale_in_when_green(app_state: AppState) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "entry_cooldown_seconds": 0,
            "scale_in_min_profit_pct": 0.005,
            "trend_filter_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    engine = Engine(app_state, market)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    market.last["BTC-USD"] = 202.0  # > 200 * 1.005
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2


def test_engine_trend_filter_blocks_enter_below_sma(app_state: AppState) -> None:
    """Golden cross on closes but last forced below SMA slow → blocked."""
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "entry_cooldown_seconds": 0,
            "trend_filter_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    # SMA50 of GOLDEN ≈ 102; set last below that
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 50.0
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 0


def test_momentum_fading_true_between_smas() -> None:
    assert momentum_fading(last=110.0, sma_fast=120.0, sma_slow=100.0) is True


def test_momentum_fading_false_above_fast() -> None:
    assert momentum_fading(last=125.0, sma_fast=120.0, sma_slow=100.0) is False


def test_momentum_fading_false_at_or_below_slow() -> None:
    assert momentum_fading(last=100.0, sma_fast=120.0, sma_slow=100.0) is False
    assert momentum_fading(last=99.0, sma_fast=120.0, sma_slow=100.0) is False


def test_momentum_fading_false_on_missing() -> None:
    assert momentum_fading(last=None, sma_fast=120.0, sma_slow=100.0) is False
    assert momentum_fading(last=110.0, sma_fast=None, sma_slow=100.0) is False

