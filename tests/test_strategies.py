from datetime import datetime, timedelta, timezone

from snowball.engine import Engine
from snowball.state import AppState
from tests.conftest import FakeMarket

GOLDEN = [100.0] * 50 + [200.0]
DEATH = [200.0] * 50 + [1.0]
FLAT = [100.0] * 60
PAIRS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


def _both_enabled(app_state: AppState, **extra: object) -> None:
    update = {
        "strategies": "sma_15m,sma_5m",
        "entry_cooldown_seconds": 0,
        "entry_cooldown_5m_seconds": 0,
        # Isolation tests use a clean 5% floor (no fee buffer) so strategy
        # scoping stays independent of production 6%+1% settings.
        "min_take_profit_pct": 0.05,
        "fee_buffer_pct": 0.0,
    }
    update.update(extra)
    app_state.settings = app_state.settings.model_copy(update=update)


def _market(
    *,
    last: dict[str, float] | None = None,
    ohlcv: dict[tuple[str, str], list[float]] | None = None,
) -> FakeMarket:
    closes = {p: list(FLAT) for p in PAIRS}
    lasts = {p: 100.0 for p in PAIRS}
    if last:
        lasts.update(last)
    return FakeMarket(closes, last=lasts, ohlcv=ohlcv)


def test_5m_exit_does_not_close_15m_lots(app_state: AppState) -> None:
    _both_enabled(app_state)
    # Distinct entries so only 5m reaches 5% TP at mark 105 (15m ~2.9%).
    app_state.ledger.open_buy(
        "BTC-USD", 102.0, 100.0, 0.0, 0.0, "seed_15m", strategy="sma_15m"
    )
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed_5m", strategy="sma_5m"
    )
    assert app_state.ledger.open_count("BTC-USD") == 2

    market = _market(
        last={"BTC-USD": 105.0},
        ohlcv={
            ("BTC-USD", "15m"): list(FLAT),
            ("BTC-USD", "5m"): list(DEATH),
        },
    )
    Engine(app_state, market).tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 1
    assert lots[0].strategy == "sma_15m"


def test_15m_exit_does_not_close_5m_lots(app_state: AppState) -> None:
    _both_enabled(app_state)
    # Distinct entries so only 15m reaches 5% TP at mark 105 (5m ~2.9%).
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed_15m", strategy="sma_15m"
    )
    app_state.ledger.open_buy(
        "BTC-USD", 102.0, 100.0, 0.0, 0.0, "seed_5m", strategy="sma_5m"
    )
    market = _market(
        last={"BTC-USD": 105.0},
        ohlcv={
            ("BTC-USD", "15m"): list(DEATH),
            ("BTC-USD", "5m"): list(FLAT),
        },
    )
    Engine(app_state, market).tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 1
    assert lots[0].strategy == "sma_5m"


def test_pair_cap_is_two_across_strategies(app_state: AppState) -> None:
    _both_enabled(app_state)
    market = _market(
        last={"BTC-USD": 200.0},
        ohlcv={
            ("BTC-USD", "15m"): list(GOLDEN),
            ("BTC-USD", "5m"): list(GOLDEN),
        },
    )
    engine = Engine(app_state, market)
    engine.tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 2
    assert {lot.strategy for lot in lots} == {"sma_15m", "sma_5m"}
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2


def test_cooldown_is_per_product_and_strategy(app_state: AppState) -> None:
    _both_enabled(
        app_state,
        entry_cooldown_seconds=900,
        entry_cooldown_5m_seconds=300,
    )
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    # Entry near mark so 15m stays below 5% TP while 5m golden-cross enters.
    app_state.ledger.open_buy(
        "BTC-USD",
        196.0,
        100.0,
        0.0,
        0.0,
        "seed_15m",
        ts=now,
        strategy="sma_15m",
    )
    market = _market(
        last={"BTC-USD": 200.0},
        ohlcv={
            ("BTC-USD", "15m"): list(FLAT),
            ("BTC-USD", "5m"): list(GOLDEN),
        },
    )
    Engine(app_state, market).tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 2
    assert {lot.strategy for lot in lots} == {"sma_15m", "sma_5m"}


def test_5m_cooldown_is_one_candle_not_15m(app_state: AppState, monkeypatch) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "strategies": "sma_5m",
            "entry_cooldown_seconds": 900,
            "entry_cooldown_5m_seconds": 300,
            "slippage_bps": 0.0,
            "scale_in_min_profit_pct": 0.005,
            "trend_filter_enabled": True,
        }
    )
    t0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    class _Clock:
        def __init__(self) -> None:
            self.t = t0

        def __call__(self) -> datetime:
            return self.t

    clock = _Clock()
    monkeypatch.setattr("snowball.engine.utcnow", clock)

    market = _market(
        last={"BTC-USD": 200.0},
        ohlcv={("BTC-USD", "5m"): list(GOLDEN)},
    )
    engine = Engine(app_state, market)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    clock.t = t0 + timedelta(seconds=299)
    market.last["BTC-USD"] = 202.0
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    clock.t = t0 + timedelta(seconds=301)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2
    lots = app_state.ledger.open_positions("BTC-USD")
    assert all(lot.strategy == "sma_5m" for lot in lots)


def test_sma_5m_enter_on_5m_golden_not_15m(app_state: AppState) -> None:
    _both_enabled(app_state)
    market = _market(
        last={"BTC-USD": 200.0},
        ohlcv={
            ("BTC-USD", "15m"): list(FLAT),
            ("BTC-USD", "5m"): list(GOLDEN),
        },
    )
    Engine(app_state, market).tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 1
    assert lots[0].strategy == "sma_5m"
