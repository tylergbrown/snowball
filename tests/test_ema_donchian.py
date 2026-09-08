from snowball.config import Settings
from snowball.models import PairSnapshot, Signal
from snowball.strategy import (
    DONCHIAN_1D,
    EMA_15M,
    donchian_breakout_signal,
    ema_crossover_signal,
    signal_for_strategy,
)


def test_ema_hold_with_short_history() -> None:
    assert ema_crossover_signal([100.0] * 10) is Signal.HOLD
    assert ema_crossover_signal([100.0] * 26) is Signal.HOLD


def test_ema_enter_on_fast_cross_above_slow() -> None:
    # Flat window: both EMAs equal. Last jump lifts the 12 faster than the 26.
    closes = [100.0] * 40 + [200.0]
    assert ema_crossover_signal(closes) is Signal.ENTER


def test_ema_exit_on_fast_cross_below_slow() -> None:
    closes = [200.0] * 40 + [50.0]
    assert ema_crossover_signal(closes) is Signal.EXIT


def test_donchian_hold_with_short_history() -> None:
    closes = [10.0] * 15
    assert donchian_breakout_signal(closes) is Signal.HOLD


def test_donchian_enter_excludes_current_bar_from_channel() -> None:
    # 21 prior bars at 10, then a close through that prior high.
    # Current high is 100 and must not be part of the 20-day channel.
    n = 22
    highs = [10.0] * (n - 1) + [100.0]
    lows = [10.0] * n
    closes = [10.0] * (n - 1) + [11.0]
    assert donchian_breakout_signal(closes, highs, lows) is Signal.ENTER
    # Close that does not clear the prior 20-day high stays HOLD,
    # even though the current bar's own high is extreme.
    no_break = [10.0] * (n - 1) + [10.0]
    assert donchian_breakout_signal(no_break, highs, lows) is Signal.HOLD


def test_donchian_exit_excludes_current_bar_from_channel() -> None:
    n = 22
    highs = [12.0] * n
    lows = [10.0] * (n - 1) + [1.0]
    closes = [12.0] * (n - 1) + [9.0]
    assert donchian_breakout_signal(closes, highs, lows) is Signal.EXIT


def test_signal_for_strategy_reads_ema_and_donchian_fields() -> None:
    ema_snap = PairSnapshot(
        product="AAPL",
        last=110.0,
        ema_fast_15m=12.0,
        ema_slow_15m=11.0,
        signal_ema_15m=Signal.ENTER.value,
    )
    sig, up = signal_for_strategy(ema_snap, EMA_15M)
    assert sig is Signal.ENTER
    assert up is True

    don_snap = PairSnapshot(
        product="AAPL",
        last=105.0,
        donchian_high_1d=100.0,
        donchian_low_1d=90.0,
        signal_donchian_1d=Signal.HOLD.value,
    )
    sig, up = signal_for_strategy(don_snap, DONCHIAN_1D)
    assert sig is Signal.HOLD
    assert up is True


def test_stock_defaults_enable_new_strategies_crypto_does_not() -> None:
    s = Settings(_env_file=None)
    assert s.strategy_list == ["sma_15m", "sma_5m"]
    assert "ema_15m" in s.stock_strategy_list
    assert "donchian_1d" in s.stock_strategy_list
    assert s.cooldown_seconds_for("ema_15m") == s.entry_cooldown_seconds
    assert s.cooldown_seconds_for("donchian_1d") == s.entry_cooldown_1d_seconds
    assert s.cooldown_seconds_for("sma_15m") == s.entry_cooldown_seconds
    assert s.cooldown_seconds_for("sma_1d") == s.entry_cooldown_1d_seconds
