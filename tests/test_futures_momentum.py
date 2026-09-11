"""Future Trader momentum_15m + session_day coexistence tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from snowball.config import Settings
from snowball.futures.engine import FuturesPaperEngine, attach_futures_lane
from snowball.futures.momentum import momentum_breakout, parse_ohlcv_ohlc
from snowball.futures.session import entry_allowed_for_settings, in_entry_preferred_window
from snowball.models import PairSnapshot, Ticker
from snowball.paper import PaperLedger
from snowball.state import AppState

ET = ZoneInfo("America/New_York")
US5 = "US5-19DEC30-CDE"
TEK = "TEK-19DEC30-CDE"


def _et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _ohlcv_rows(bars, *, timeframe: str = "15m"):
    step = {"5m": 300_000.0, "15m": 900_000.0}.get(timeframe, 900_000.0)
    base = 1_700_000_000_000.0
    return [[base + i * step, o, h, l, c, 1.0] for i, (o, h, l, c) in enumerate(bars)]


def _breakout_bars(*, last: float = 210.0, lookback: int = 8):
    """Flat then green breakout above prior high."""
    prior = last - 5.0
    bars = [(prior, prior + 0.5, prior - 0.5, prior)] * lookback
    # green impulse clearing prior high
    bars.append((prior + 0.2, last, prior, last))
    return bars


def _no_breakout_bars(*, last: float = 200.0, lookback: int = 8):
    bars = [(last, last + 0.5, last - 0.5, last)] * (lookback + 1)
    return bars


class FakeMomMarket:
    mark_source = "coinbase_perp"

    def __init__(self, *, last: float, bars_15m):
        self.last = last
        self.bars_15m = bars_15m
        self.account_value = 10_000.0

    def fetch_ohlcv(self, product, timeframe, limit):
        if timeframe in ("15m", "5m"):
            rows = _ohlcv_rows(self.bars_15m, timeframe=timeframe)
        else:
            rows = _ohlcv_rows([(self.last, self.last, self.last, self.last)] * 60)
        return rows[-limit:]

    def fetch_ticker(self, product):
        return Ticker(
            product=product,
            last=self.last,
            bid=self.last - 0.1,
            ask=self.last + 0.1,
            ts=datetime.now(timezone.utc),
        )

    def fetch_account_value_usd(self, *, crypto_marks=None):
        return float(self.account_value)

    def fetch_bba(self, product):
        return self.last - 0.1, self.last + 0.1


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        futures_enabled=True,
        futures_mode="paper",
        futures_live_enabled=False,
        futures_sqlite_path=tmp_path / "futures.db",
        futures_strategies="session_day,momentum_15m",
        futures_products=f"{US5},{TEK}",
        futures_poll_seconds=0.05,
        futures_max_positions=1,
        futures_max_notional_usd=500.0,
        futures_account_budget_pct=0.30,
        futures_bankroll_usd=1000.0,
        futures_momentum_timeframe="15m",
        futures_momentum_lookback_bars=8,
        futures_momentum_min_pct=0.003,
        futures_momentum_take_profit_pct=0.008,
        futures_momentum_stall_exit_enabled=True,
        futures_momentum_stall_lookback_bars=4,
        futures_momentum_stall_exit_pct=0.004,
        never_sell_red=True,
        min_take_profit_pct=0.05,
        sma_min_take_profit_pct=0.05,
        slippage_bps=0.0,
        pair_pause_enabled=False,
        ohlcv_fetch_limit=100,
    )
    base.update(kwargs)
    return Settings(**base)


def test_momentum_breakout_detector():
    bars = _breakout_bars(last=210.0)
    opens, highs, lows, closes = zip(*bars)
    assert momentum_breakout(highs, lows, closes, opens, lookback=8, min_momentum_pct=0.003)
    bars2 = _no_breakout_bars()
    o, h, l, c = zip(*bars2)
    assert not momentum_breakout(h, l, c, o, lookback=8, min_momentum_pct=0.003)


def test_session_preferred_only_when_momentum_enabled(tmp_path: Path):
    s = _settings(tmp_path)
    assert s.futures_session_entry_preferred_only() is True
    # Midday — session entry blocked (preferred-only)
    midday = _et(2026, 9, 11, 12, 0).astimezone(timezone.utc)
    assert entry_allowed_for_settings(midday, s) is False
    # Preferred window — allowed
    open_win = _et(2026, 9, 11, 9, 26).astimezone(timezone.utc)
    assert entry_allowed_for_settings(open_win, s) is True
    # Momentum off → late catch-up OK
    s2 = _settings(tmp_path, futures_strategies="session_day", futures_sqlite_path=tmp_path / "f2.db")
    assert s2.futures_session_entry_preferred_only() is False
    assert entry_allowed_for_settings(midday, s2) is True


def test_momentum_entry_and_no_double_long_with_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    bars = _breakout_bars(last=210.0)
    market = FakeMomMarket(last=210.0, bars_15m=bars)
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    engine._last_budget = {
        "account_value_usd": 1000.0,
        "budget_usd": 300.0,
        "per_index_usd": 150.0,
        "per_leg_notional_usd": 500.0,
    }
    for product in (US5, TEK):
        state.futures_pairs[product] = PairSnapshot(
            product=product, last=210.0, bid=209.9, ask=210.1, max_open=1
        )

    # Midday cash hours — momentum can enter
    midday = _et(2026, 9, 11, 12, 0).astimezone(timezone.utc)
    marks = {US5: 210.0, TEK: 210.0}
    engine._act_momentum(
        product=US5,
        now=midday,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks=marks,
    )
    assert state.futures_ledger.open_count(US5) == 1
    lot = state.futures_ledger.open_positions(US5)[0]
    assert lot.strategy == "momentum_15m"

    # Session cannot add a second lot same product
    engine._act_session(
        product=US5,
        now=_et(2026, 9, 11, 9, 26).astimezone(timezone.utc),
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks=marks,
    )
    assert state.futures_ledger.open_count(US5) == 1

    # Second momentum entry blocked
    engine._act_momentum(
        product=US5,
        now=midday + timedelta(minutes=30),
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks=marks,
    )
    assert state.futures_ledger.open_count(US5) == 1


def test_momentum_take_profit_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _settings(tmp_path, futures_momentum_take_profit_pct=0.008)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    assert state.futures_ledger is not None
    opened = _et(2026, 9, 11, 11, 0).astimezone(timezone.utc)
    state.futures_ledger.open_buy(
        product=US5,
        fill_px=200.0,
        notional_usd=200.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="momentum_15m:breakout_enter",
        ts=opened,
        strategy="momentum_15m",
    )
    # +1% green → above 0.8% TP
    market = FakeMomMarket(last=202.0, bars_15m=_no_breakout_bars(last=202.0))
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    state.futures_pairs[US5] = PairSnapshot(
        product=US5, last=202.0, bid=201.9, ask=202.1, max_open=1
    )
    engine._last_budget = {
        "account_value_usd": 1000.0,
        "budget_usd": 300.0,
        "per_index_usd": 150.0,
        "per_leg_notional_usd": 500.0,
    }
    midday = _et(2026, 9, 11, 12, 0).astimezone(timezone.utc)
    engine._act_momentum(
        product=US5,
        now=midday,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={US5: 202.0},
    )
    assert state.futures_ledger.open_count(US5) == 0


def test_momentum_never_sell_red_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    settings = _settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    assert state.futures_ledger is not None
    opened = _et(2026, 9, 11, 11, 0).astimezone(timezone.utc)
    state.futures_ledger.open_buy(
        product=US5,
        fill_px=200.0,
        notional_usd=200.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="momentum_15m:breakout_enter",
        ts=opened,
        strategy="momentum_15m",
    )
    market = FakeMomMarket(last=195.0, bars_15m=_no_breakout_bars(last=195.0))
    engine = FuturesPaperEngine(state, market=market)  # type: ignore[arg-type]
    state.futures_pairs[US5] = PairSnapshot(
        product=US5, last=195.0, max_open=1
    )
    engine._last_budget = {
        "account_value_usd": 1000.0,
        "budget_usd": 300.0,
        "per_index_usd": 150.0,
        "per_leg_notional_usd": 500.0,
    }
    # EOD red → hold
    close_now = _et(2026, 9, 11, 15, 56).astimezone(timezone.utc)
    engine._act_momentum(
        product=US5,
        now=close_now,
        halted=False,
        can_trade=True,
        daily_killed=False,
        marks={US5: 195.0},
    )
    assert state.futures_ledger.open_count(US5) == 1


def test_snapshot_shows_momentum_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FUTURES_MODE", raising=False)
    from snowball.futures.snapshot import build_futures_snapshot

    settings = _settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_futures_lane(state)
    state.futures_account_value_usd = 10_000.0
    state.futures_budget_usd = 3_000.0
    state.futures_per_index_allotment_usd = 1_500.0
    body = build_futures_snapshot(state)
    assert body["risk"]["budget_pct"] == 0.30
    assert body["momentum"]["enabled"] is True
    assert body["momentum"]["strategy"] == "momentum_15m"
    assert "session_day" in body["status"]["strategies"]
    assert "momentum_15m" in body["status"]["strategies"]
    assert body["daily_pnl_target_usd"] == 100.0


def test_parse_ohlcv_helper():
    rows = _ohlcv_rows(_breakout_bars())
    o, h, l, c = parse_ohlcv_ohlc(rows)
    assert len(o) == len(h) == len(l) == len(c) == len(rows)
