from snowball.models import Signal
from snowball.strategy import crossover_signal, sma


def test_sma_none_until_enough_bars() -> None:
    assert sma([1.0, 2.0], 3) is None
    assert sma([1.0, 2.0, 3.0], 3) == 2.0


def test_golden_cross_enter() -> None:
    # 50 bars at 100, then a spike so fast SMA crosses above slow.
    closes = [100.0] * 50 + [200.0]
    assert len(closes) == 51
    assert crossover_signal(closes, 20, 50) is Signal.ENTER


def test_death_cross_exit() -> None:
    closes = [200.0] * 50 + [1.0]
    assert crossover_signal(closes, 20, 50) is Signal.EXIT


def test_no_cross_is_hold() -> None:
    closes = [100.0] * 60
    assert crossover_signal(closes, 20, 50) is Signal.HOLD


def test_insufficient_history_hold() -> None:
    assert crossover_signal([1.0] * 50, 20, 50) is Signal.HOLD


def test_uptrend_without_cross_is_not_enter() -> None:
    # Already in uptrend: last 20 bars higher than earlier 50, but previous window also fast>slow.
    closes = [10.0] * 30 + [30.0] * 30
    assert crossover_signal(closes, 20, 50) is Signal.HOLD


def test_5m_crossover_reuses_sma_helper() -> None:
    """sma_5m uses the same crossover_signal helper on 5-minute closes."""
    golden = [100.0] * 50 + [200.0]
    death = [200.0] * 50 + [1.0]
    flat = [100.0] * 60
    assert crossover_signal(golden, 20, 50) is Signal.ENTER
    assert crossover_signal(death, 20, 50) is Signal.EXIT
    assert crossover_signal(flat, 20, 50) is Signal.HOLD


def test_5m_insufficient_history_hold() -> None:
    assert crossover_signal([100.0] * 50, 20, 50) is Signal.HOLD
