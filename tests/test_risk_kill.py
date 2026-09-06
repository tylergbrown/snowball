from datetime import datetime, timedelta, timezone

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.halt import halt_active, write_halt
from snowball.live import LiveBroker, make_broker
from snowball.risk import RiskContext, allow_entry, allow_exit, daily_loss_breached


def _ctx(**kwargs: object) -> RiskContext:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    base = dict(
        now=now,
        trading_enabled=True,
        halt_active=False,
        daily_killed=False,
        open_count_for_pair=0,
        last_entry_at=None,
        cash_usd=1000.0,
        requested_notional=100.0,
        max_positions_per_pair=2,
        max_position_notional_usd=100.0,
        entry_cooldown=timedelta(seconds=900),
        mode="paper",
        live_enabled=False,
    )
    base.update(kwargs)
    return RiskContext(**base)  # type: ignore[arg-type]


def test_default_settings_cannot_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    s = Settings(_env_file=None)
    assert s.mode == "paper"
    assert s.live_enabled is False
    assert s.live_orders_permitted() is False
    assert make_broker(s) is None
    with pytest.raises(LiveTradingRefused):
        LiveBroker(s)


def test_mode_live_without_flag_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    s = Settings(_env_file=None, mode="live", live_enabled=False)
    assert s.live_orders_permitted() is False
    with pytest.raises(LiveTradingRefused):
        make_broker(s)
    with pytest.raises(LiveTradingRefused):
        s.assert_not_accidentally_live()


def test_halt_file_blocks_entry_and_exit(tmp_settings: Settings) -> None:
    assert halt_active(tmp_settings.halt_file) is False
    write_halt(tmp_settings.halt_file)
    assert halt_active(tmp_settings.halt_file) is True
    ok, reason = allow_entry(_ctx(halt_active=True))
    assert not ok and reason == "halt_file"
    ok, reason = allow_exit(_ctx(halt_active=True))
    assert not ok and reason == "halt_file"


def test_trading_disabled_blocks_orders() -> None:
    ok, reason = allow_entry(_ctx(trading_enabled=False))
    assert not ok and reason == "trading_disabled"
    ok, reason = allow_exit(_ctx(trading_enabled=False))
    assert not ok and reason == "trading_disabled"


def test_daily_loss_blocks_entry_allows_flatten_exit() -> None:
    assert daily_loss_breached(974.0, 1000.0, 25.0) is True
    assert daily_loss_breached(976.0, 1000.0, 25.0) is False
    ok, reason = allow_entry(_ctx(daily_killed=True))
    assert not ok and reason == "daily_loss_kill"
    ok, reason = allow_exit(_ctx(daily_killed=True))
    assert ok and reason == "ok"


def test_max_two_positions_per_pair() -> None:
    ok, reason = allow_entry(_ctx(open_count_for_pair=2))
    assert not ok and reason == "max_positions_per_pair"
    ok, _ = allow_entry(_ctx(open_count_for_pair=1))
    assert ok


def test_notional_cap() -> None:
    ok, reason = allow_entry(_ctx(requested_notional=100.01))
    assert not ok and reason == "notional_cap"
    ok, _ = allow_entry(_ctx(requested_notional=100.0))
    assert ok


def test_cooldown() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ok, reason = allow_entry(
        _ctx(now=now, last_entry_at=now - timedelta(seconds=100), open_count_for_pair=1)
    )
    assert not ok and reason == "cooldown"
    ok, _ = allow_entry(
        _ctx(now=now, last_entry_at=now - timedelta(seconds=901), open_count_for_pair=1)
    )
    assert ok


def test_insufficient_cash() -> None:
    ok, reason = allow_entry(_ctx(cash_usd=99.0, requested_notional=100.0))
    assert not ok and reason == "insufficient_cash"


def test_live_mode_without_flag_blocked_in_risk() -> None:
    ok, reason = allow_entry(_ctx(mode="live", live_enabled=False))
    assert not ok and reason == "live_not_enabled"


def test_default_strategies_are_15m_and_5m(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGIES", raising=False)
    s = Settings(_env_file=None)
    assert s.strategy_list == ["sma_15m", "sma_5m"]
    assert s.entry_cooldown_5m_seconds == 300
    assert s.ohlcv_fetch_limit >= s.sma_slow + 1


def test_unknown_strategy_errors_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.delenv("STRATEGIES", raising=False)
    with pytest.raises(ValidationError, match="Unknown STRATEGIES"):
        Settings(_env_file=None, strategies="sma_15m,bogus")


def test_empty_strategies_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.delenv("STRATEGIES", raising=False)
    with pytest.raises(ValidationError, match="at least one"):
        Settings(_env_file=None, strategies="  ,  ")
