"""Never-sell-red + min-TP floor; death-cross full exit; fade single-lot scale-out."""

from __future__ import annotations

from snowball.engine import Engine
from snowball.halt import write_halt
from snowball.state import AppState
from tests.conftest import FakeMarket

DEATH = [200.0] * 50 + [1.0]
FLAT = [100.0] * 60
# Uptrend HOLD: SMA20=120, SMA50=108 (prev also fast>slow → no cross).
UPTREND = [100.0] * 40 + [120.0] * 21
PAIRS = ("BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD")


def _cfg(app_state: AppState, **extra: object) -> None:
    update = {
        "strategies": "sma_15m,sma_5m",
        "entry_cooldown_seconds": 0,
        "entry_cooldown_5m_seconds": 0,
        "slippage_bps": 0.0,
        "min_take_profit_pct": 0.05,
        "sma_min_take_profit_pct": 0.05,
        "fee_buffer_pct": 0.0,
        "never_sell_red": True,
        "daily_loss_kill_usd": 25.0,
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


def test_15m_red_death_cross_holds(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_15m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    market = _market(
        last={"BTC-USD": 90.0},
        ohlcv={("BTC-USD", "15m"): list(DEATH)},
    )
    Engine(app_state, market).tick()
    lots = app_state.ledger.open_positions("BTC-USD")
    assert len(lots) == 1
    assert lots[0].strategy == "sma_15m"


def test_15m_plus_3pct_death_cross_holds(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_15m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    market = _market(
        last={"BTC-USD": 103.0},
        ohlcv={("BTC-USD", "15m"): list(DEATH)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_15m_plus_5pct_without_fade_or_exit_holds(app_state: AppState) -> None:
    """Green >=5% alone does NOT sell — need fade or death cross."""
    _cfg(app_state, strategies="sma_15m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    # Flat book: last=105 > sma_fast=sma_slow=100 → not fading, no EXIT.
    market = _market(
        last={"BTC-USD": 105.0},
        ohlcv={("BTC-USD", "15m"): list(FLAT)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_15m_plus_5pct_death_cross_exits_all(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_15m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed2", strategy="sma_15m"
    )
    market = _market(
        last={"BTC-USD": 105.0},
        ohlcv={("BTC-USD", "15m"): list(DEATH)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_positions("BTC-USD") == []
    fills = [f for f in app_state.ledger.recent_fills(limit=20) if f.side == "sell"]
    assert len(fills) == 2
    assert all(f.reason == "sma_15m:exit" for f in fills)


def test_15m_fade_plus_5pct_closes_one_lot(app_state: AppState) -> None:
    """Fade + green floor → close ONE lot with :fade (highest unrealized %)."""
    _cfg(app_state, strategies="sma_15m")
    pos_lo, _ = app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed_lo", strategy="sma_15m"
    )
    pos_hi, _ = app_state.ledger.open_buy(
        "BTC-USD", 102.0, 100.0, 0.0, 0.0, "seed_hi", strategy="sma_15m"
    )
    # SMA20=120, SMA50=108; last=112 → fading; both lots green >=5% vs mark.
    # Lower entry (100) has higher unrealized % → closed.
    market = _market(
        last={"BTC-USD": 112.0},
        ohlcv={("BTC-USD", "15m"): list(UPTREND)},
    )
    Engine(app_state, market).tick()
    open_lots = app_state.ledger.open_positions("BTC-USD")
    assert len(open_lots) == 1
    assert open_lots[0].id == pos_hi.id
    sells = [f for f in app_state.ledger.recent_fills(limit=20) if f.side == "sell"]
    assert len(sells) == 1
    assert sells[0].reason == "sma_15m:fade"
    assert sells[0].position_id == pos_lo.id


def test_15m_fade_below_5pct_holds(app_state: AppState) -> None:
    # Mark just above entry: still fading, below 5% floor, and not green enough to scale-in.
    _cfg(app_state, strategies="sma_15m", scale_in_min_profit_pct=0.05)
    app_state.ledger.open_buy(
        "BTC-USD", 110.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    # last=112: fading (108 < 112 < 120) but only ~+1.8% → below floor; no sell.
    market = _market(
        last={"BTC-USD": 112.0},
        ohlcv={("BTC-USD", "15m"): list(UPTREND)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    assert not any(f.side == "sell" for f in app_state.ledger.recent_fills(limit=20))


def test_5m_red_death_cross_holds(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_5m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_5m"
    )
    market = _market(
        last={"BTC-USD": 90.0},
        ohlcv={("BTC-USD", "5m"): list(DEATH)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_5m_plus_5pct_without_fade_or_exit_holds(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_5m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_5m"
    )
    market = _market(
        last={"BTC-USD": 105.0},
        ohlcv={("BTC-USD", "5m"): list(FLAT)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_5m_fade_plus_5pct_closes_one(app_state: AppState) -> None:
    _cfg(app_state, strategies="sma_5m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_5m"
    )
    market = _market(
        last={"BTC-USD": 112.0},
        ohlcv={("BTC-USD", "5m"): list(UPTREND)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_positions("BTC-USD") == []
    sells = [f for f in app_state.ledger.recent_fills(limit=10) if f.side == "sell"]
    assert len(sells) == 1
    assert sells[0].reason == "sma_5m:fade"


def test_halt_flatten_holds_red_15m(app_state: AppState) -> None:
    """NEVER_SELL_RED (+ emergency) is absolute — HALT does not sell underwater lots."""
    _cfg(app_state, strategies="sma_15m")
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    write_halt(app_state.settings.halt_file)
    market = _market(
        last={"BTC-USD": 80.0},
        ohlcv={("BTC-USD", "15m"): list(FLAT)},
    )
    Engine(app_state, market).tick()
    assert app_state.ledger.open_count("BTC-USD") == 1


def test_daily_loss_kill_holds_red_15m(app_state: AppState) -> None:
    """Daily-loss kill blocks new entries but does not sell red when never_sell_red holds."""
    _cfg(app_state, strategies="sma_15m", daily_loss_kill_usd=25.0)
    market = _market(
        last={"BTC-USD": 100.0},
        ohlcv={("BTC-USD", "15m"): list(FLAT)},
    )
    engine = Engine(app_state, market)
    engine.tick()  # establish day equity ~ bankroll
    app_state.ledger.open_buy(
        "BTC-USD", 100.0, 100.0, 0.0, 0.0, "seed", strategy="sma_15m"
    )
    market.last["BTC-USD"] = 70.0  # -30 unrealized => daily kill
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
