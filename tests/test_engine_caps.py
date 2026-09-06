from snowball.engine import Engine
from snowball.models import utcnow
from snowball.state import AppState


def test_third_lot_on_same_pair_blocked(app_state: AppState) -> None:
    app_state.settings = app_state.settings.model_copy(
        update={
            "entry_cooldown_seconds": 0,
            "strategies": "sma_15m",
            "slippage_bps": 0.0,
            "scale_in_min_profit_pct": 0.005,
            "trend_filter_enabled": True,
            "max_positions_per_pair": 2,
        }
    )
    from tests.conftest import FakeMarket

    golden = [100.0] * 50 + [200.0]
    flat = [100.0] * 60
    market = FakeMarket(
        {
            "BTC-USD": golden,
            "SOL-USD": flat,
            "ETH-USD": flat,
            "DOGE-USD": flat,
        },
        last={"BTC-USD": 200.0, "SOL-USD": 100.0, "ETH-USD": 100.0, "DOGE-USD": 100.0},
    )
    engine = Engine(app_state, market)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    market.last["BTC-USD"] = 202.0  # green enough for scale-in
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2
    market.last["BTC-USD"] = 204.0  # still below 5% TP from ~200 entry
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 2
    assert len(app_state.ledger.open_positions()) == 2


def test_daily_kill_flattens_and_blocks(app_state: AppState) -> None:
    from tests.conftest import FakeMarket

    flat = [100.0] * 60
    market = FakeMarket(
        {
            "BTC-USD": flat,
            "SOL-USD": flat,
            "ETH-USD": flat,
            "DOGE-USD": flat,
        },
        last={"BTC-USD": 100.0, "SOL-USD": 100.0, "ETH-USD": 100.0, "DOGE-USD": 100.0},
    )
    engine = Engine(app_state, market)
    engine.tick()
    # Snapshot start-of-day equity at ~1000, then a lot that gaps down.
    app_state.ledger.open_buy(
        "BTC-USD",
        fill_px=100.0,
        notional_usd=100.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="seed",
    )
    market.last["BTC-USD"] = 70.0
    engine.tick()
    assert app_state.ledger.open_positions() == []
    assert app_state.ledger.is_daily_killed(utcnow())
    market.closes["BTC-USD"] = [100.0] * 50 + [200.0]
    market.last["BTC-USD"] = 200.0
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 0
