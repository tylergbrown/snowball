"""RSI / Bollinger math, cross signals, entry filters, exit gates."""

from __future__ import annotations

from datetime import datetime, timezone

from snowball.config import Settings
from snowball.gates import (
    indicator_filters_allow,
    strategy_exit_allowed,
)
from snowball.models import PairSnapshot, Position, Signal
from snowball.strategy import (
    BB_15M,
    BB_1D,
    RSI_15M,
    RSI_1D,
    bb_cross_signal,
    bollinger_bands,
    rsi,
    rsi_cross_signal,
    signal_for_strategy,
)


def test_rsi_none_until_enough_bars() -> None:
    assert rsi([1.0] * 14, 14) is None
    # period+1 closes → first RSI
    series = [100.0] * 15
    assert rsi(series, 14) == 50.0  # flat → no gains/losses → 50


def test_rsi_wilder_rises_on_rally() -> None:
    # Long flat then strong up move → RSI high
    closes = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0]
    v = rsi(closes, 14)
    assert v is not None and v > 70.0


def test_rsi_wilder_falls_on_selloff() -> None:
    closes = [100.0] * 20 + [99.0, 98.0, 97.0, 96.0, 95.0]
    v = rsi(closes, 14)
    assert v is not None and v < 30.0


def test_bollinger_mid_is_sma20_and_bands_symmetric() -> None:
    closes = [float(i) for i in range(1, 25)]
    upper, mid, lower = bollinger_bands(closes, 20, 2.0)
    assert mid is not None and upper is not None and lower is not None
    assert mid == sum(closes[-20:]) / 20.0
    assert abs((upper - mid) - (mid - lower)) < 1e-9
    assert upper > mid > lower


def test_rsi_enter_cross_up_from_oversold() -> None:
    # Build oversold, then bounce so RSI crosses up through 30.
    down = [100.0]
    px = 100.0
    for _ in range(30):
        px *= 0.97
        down.append(px)
    # Bounce
    bounce = list(down)
    for _ in range(8):
        px *= 1.04
        bounce.append(px)
    # Find a window where prior RSI <=30 and current >30
    found = False
    for n in range(16, len(bounce) + 1):
        if rsi_cross_signal(bounce[:n]) is Signal.ENTER:
            found = True
            break
    assert found, "expected an RSI ENTER cross on bounce from oversold"


def test_rsi_exit_cross_down_from_overbought() -> None:
    up = [100.0]
    px = 100.0
    for _ in range(30):
        px *= 1.03
        up.append(px)
    fade = list(up)
    for _ in range(8):
        px *= 0.96
        fade.append(px)
    found = False
    for n in range(16, len(fade) + 1):
        if rsi_cross_signal(fade[:n]) is Signal.EXIT:
            found = True
            break
    assert found, "expected an RSI EXIT cross from overbought"


def test_bb_enter_cross_up_from_lower() -> None:
    # Flat then spike down below band, then recover above lower.
    base = [100.0] * 25
    dipped = base + [80.0]  # far below → close at/below lower
    # Next bar recovers toward mid
    recovered = dipped + [95.0]
    assert bb_cross_signal(dipped) in (Signal.HOLD, Signal.EXIT, Signal.ENTER)
    # Ensure ENTER on recovery from below/at lower
    assert bb_cross_signal(recovered) is Signal.ENTER


def test_bb_exit_when_close_at_or_above_upper() -> None:
    base = [100.0] * 25
    spiked = base + [150.0]
    assert bb_cross_signal(spiked) is Signal.EXIT


def test_filter_blocks_rsi_overbought() -> None:
    ok, reason = indicator_filters_allow(
        last=100.0, rsi=70.0, bb_upper=110.0, bb_mid=100.0, enabled=True
    )
    assert ok is False and reason == "indicator_rsi_overbought"
    ok, reason = indicator_filters_allow(
        last=100.0, rsi=69.9, bb_upper=110.0, bb_mid=100.0, enabled=True
    )
    assert ok is True


def test_filter_blocks_above_bb_upper() -> None:
    ok, reason = indicator_filters_allow(
        last=111.0, rsi=50.0, bb_upper=110.0, bb_mid=100.0, enabled=True
    )
    assert ok is False and reason == "indicator_above_bb_upper"
    # At mid / lower half allowed
    ok, _ = indicator_filters_allow(
        last=100.0, rsi=50.0, bb_upper=110.0, bb_mid=100.0, enabled=True
    )
    assert ok is True


def test_filter_disabled_passes() -> None:
    ok, reason = indicator_filters_allow(
        last=200.0, rsi=90.0, bb_upper=110.0, enabled=False
    )
    assert ok is True and reason == "ok"


def test_exit_gates_still_refuse_red() -> None:
    lot = Position(
        id=1,
        product="BTC-USD",
        side="long",
        qty=1.0,
        entry_price=100.0,
        notional_usd=100.0,
        opened_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        status="open",
        strategy="rsi_15m",
    )
    ok, reason = strategy_exit_allowed(
        lot, 99.0, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is False and reason == "never_sell_red"
    ok, reason = strategy_exit_allowed(
        lot, 106.5, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is False and reason == "below_take_profit"
    ok, reason = strategy_exit_allowed(
        lot, 107.0, min_take_profit_pct=0.06, never_sell_red=True, fee_buffer_pct=0.01
    )
    assert ok is True


def test_signal_for_strategy_rsi_bb_fields() -> None:
    snap = PairSnapshot(
        product="BTC-USD",
        last=100.0,
        rsi_15m=45.0,
        signal_rsi_15m=Signal.ENTER.value,
        bb_upper_15m=110.0,
        bb_mid_15m=100.0,
        bb_lower_15m=90.0,
        signal_bb_15m=Signal.HOLD.value,
    )
    sig, up = signal_for_strategy(snap, RSI_15M)
    assert sig is Signal.ENTER and up is True
    sig, up = signal_for_strategy(snap, BB_15M)
    assert sig is Signal.HOLD and up is True


def test_defaults_enable_rsi_bb_on_crypto_and_stock_not_futures() -> None:
    s = Settings(_env_file=None)
    assert "rsi_15m" in s.strategy_list
    assert "bb_15m" in s.strategy_list
    assert "rsi_1d" in s.strategy_list
    assert "bb_1d" in s.strategy_list
    assert "rsi_15m" in s.stock_strategy_list
    assert "bb_15m" in s.stock_strategy_list
    assert "rsi_1d" in s.stock_strategy_list
    assert "bb_1d" in s.stock_strategy_list
    assert s.futures_strategy_list == ["session_day", "momentum_15m"]
    assert "rsi_15m" not in s.futures_strategy_list
    assert "bb_15m" not in s.futures_strategy_list
    assert s.indicator_filters_enabled is True
    assert RSI_15M and BB_15M and RSI_1D and BB_1D
