"""Per-product pause after consecutive closed losers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from snowball.engine import Engine
from snowball.paper import PaperLedger
from snowball.state import AppState
from tests.conftest import FakeMarket

FLAT = [100.0] * 60
GOLDEN = [100.0] * 50 + [200.0]
DEATH = [200.0] * 50 + [1.0]
PAIRS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


def test_pause_after_three_losses(ledger: PaperLedger) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        pos, _ = ledger.open_buy(
            "BTC-USD", 100.0, 100.0, 0.0, 0.0, f"buy{i}", ts=now, strategy="sma_15m"
        )
        fill = ledger.close_position(pos.id, 90.0, 0.0, 0.0, "loss", ts=now + timedelta(minutes=i + 1))
        paused = ledger.record_closed_trade_for_pause(
            "BTC-USD",
            (fill.price - 100.0) * fill.qty,
            now=now + timedelta(minutes=i + 1),
            enabled=True,
            loss_threshold=3,
            pause_hours=24.0,
        )
        if i < 2:
            assert paused is None
            assert not ledger.is_pair_paused("BTC-USD", now + timedelta(minutes=i + 1))
        else:
            assert paused is not None
            assert ledger.is_pair_paused("BTC-USD", now + timedelta(minutes=i + 1))


def test_win_resets_streak(ledger: PaperLedger) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    for i in range(2):
        pos, _ = ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, f"l{i}", ts=now)
        fill = ledger.close_position(pos.id, 90.0, 0.0, 0.0, "loss", ts=now)
        ledger.record_closed_trade_for_pause(
            "BTC-USD", -10.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    pos, _ = ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, "win", ts=now)
    ledger.close_position(pos.id, 110.0, 0.0, 0.0, "win", ts=now)
    ledger.record_closed_trade_for_pause(
        "BTC-USD", 10.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
    )
    assert ledger.consecutive_losses("BTC-USD") == 0
    assert not ledger.is_pair_paused("BTC-USD", now)


def test_clear_pair_pause(ledger: PaperLedger) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    for _ in range(3):
        pos, _ = ledger.open_buy("ETH-USD", 100.0, 100.0, 0.0, 0.0, "x", ts=now)
        ledger.close_position(pos.id, 80.0, 0.0, 0.0, "loss", ts=now)
        ledger.record_closed_trade_for_pause(
            "ETH-USD", -20.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    assert ledger.is_pair_paused("ETH-USD", now)
    assert ledger.clear_pair_pause("ETH-USD") is True
    assert not ledger.is_pair_paused("ETH-USD", now)


def test_engine_blocks_entry_while_paused(app_state: AppState, tmp_path, monkeypatch) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "entry_cooldown_seconds": 0,
            "pair_pause_enabled": True,
            "pair_pause_losses": 3,
            "trend_filter_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("snowball.engine.utcnow", lambda: now)
    for _ in range(3):
        pos, _ = app_state.ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, "x", ts=now)
        app_state.ledger.close_position(pos.id, 80.0, 0.0, 0.0, "loss", ts=now)
        app_state.ledger.record_closed_trade_for_pause(
            "BTC-USD", -20.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    assert app_state.ledger.is_pair_paused("BTC-USD", now)

    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 0


def test_clear_file_clears_pause(app_state: AppState, tmp_path, monkeypatch) -> None:
    clear_path = tmp_path / "PAIR_PAUSE_CLEAR"
    clear_path.write_text("BTC-USD\n", encoding="utf-8")
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "entry_cooldown_seconds": 0,
            "pair_pause_enabled": True,
            "pair_pause_clear_file": clear_path,
            "trend_filter_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("snowball.engine.utcnow", lambda: now)
    for _ in range(3):
        pos, _ = app_state.ledger.open_buy("BTC-USD", 100.0, 100.0, 0.0, 0.0, "x", ts=now)
        app_state.ledger.close_position(pos.id, 80.0, 0.0, 0.0, "loss", ts=now)
        app_state.ledger.record_closed_trade_for_pause(
            "BTC-USD", -20.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(GOLDEN)},
    )
    market.last["BTC-USD"] = 200.0
    Engine(app_state, market).tick()
    assert not clear_path.exists()
    assert not app_state.ledger.is_pair_paused("BTC-USD")
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_exits_still_allowed_while_paused(app_state: AppState, monkeypatch) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_15m",
            "pair_pause_enabled": True,
            "slippage_bps": 0.0,
        }
    )
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("snowball.engine.utcnow", lambda: now)
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", ts=now, strategy="sma_15m"
    )
    for _ in range(3):
        pos, _ = app_state.ledger.open_buy("SOL-USD", 100.0, 50.0, 0.0, 0.0, "x", ts=now)
        app_state.ledger.close_position(pos.id, 80.0, 0.0, 0.0, "loss", ts=now)
        app_state.ledger.record_closed_trade_for_pause(
            "BTC-USD", -20.0, now=now, enabled=True, loss_threshold=3, pause_hours=24
        )
    assert app_state.ledger.is_pair_paused("BTC-USD", now)
    market = FakeMarket(
        {p: list(FLAT) for p in PAIRS},
        last={p: 100.0 for p in PAIRS},
        ohlcv={("BTC-USD", "15m"): list(DEATH)},
    )
    market.last["BTC-USD"] = 105.0  # strategy exit still needs >=5% TP while paused
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 0
