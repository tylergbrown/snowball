"""CFM stock lane: intraday stall take-profit (>=5% green + stalled) vs trending hold."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.config import Settings
from snowball.futures.session import in_us_cash_session
from snowball.gates import price_stalled, stall_exit_allowed
from snowball.models import PairSnapshot, Position, Ticker
from snowball.paper import PaperLedger
from snowball.state import AppState
from snowball.stocks.engine import StockPaperEngine

TEK = "TEK-19DEC30-CDE"
ENTRY = 3912.8

# Friday 2026-09-11 12:00 EDT = 16:00 UTC (US cash session)
MIDDAY_UTC = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)
# After cash stall window (16:00 EDT = 20:00 UTC)
AFTER_HOURS_UTC = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)


def _lot(*, entry: float = ENTRY, strategy: str = "ema_15m", pid: int = 1) -> Position:
    return Position(
        id=pid,
        product=TEK,
        side="long",
        qty=1.0,
        entry_price=entry,
        notional_usd=entry,
        opened_at=datetime(2026, 9, 11, 5, 49, tzinfo=timezone.utc),
        status="open",
        strategy=strategy,
    )


def _ohlcv_rows(
    bars: list[tuple[float, float, float, float]],
    *,
    timeframe: str = "15m",
) -> list[list[float]]:
    """bars = (open, high, low, close) chronologically."""
    step = {"5m": 300_000.0, "15m": 900_000.0, "1d": 86_400_000.0}.get(timeframe, 900_000.0)
    base = 1_700_000_000_000.0
    rows: list[list[float]] = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append([base + i * step, o, h, l, c, 1.0])
    return rows


def _pad_flat(n: int, px: float) -> list[tuple[float, float, float, float]]:
    return [(px, px, px, px)] * n


def _trending_15m(*, last: float) -> list[tuple[float, float, float, float]]:
    """Clear uptrend making fresh highs — must NOT look stalled."""
    # Warm-up flat then ascending highs; last bar extends; mark at high.
    bars = _pad_flat(50, last - 50.0)
    base = last - 40.0
    for i in range(8):
        o = base + i * 5.0
        h = o + 6.0
        l = o - 1.0
        c = o + 5.0
        bars.append((o, h, l, c))
    bars[-1] = (last - 5.0, last, last - 6.0, last)
    return bars


def _stalled_15m(*, last: float, peak: float | None = None) -> list[tuple[float, float, float, float]]:
    """Compressed plateau then mark off the window high — stalled."""
    peak = peak if peak is not None else last * 1.004  # ~0.4% above mark
    # Warm-up then flat compressed range around peak; last bar settles off highs.
    bars: list[tuple[float, float, float, float]] = _pad_flat(50, peak)
    for _ in range(4):
        bars.append((peak - 1.0, peak, peak - 3.0, peak - 1.0))
    bars.append((last + 1.0, max(last + 2.0, peak - 1.0), last - 1.0, last))
    return bars


class FakeOhlcMarket:
    mark_source = "yahoo_paper"

    def __init__(
        self,
        *,
        last: float,
        bars_15m: list[tuple[float, float, float, float]],
        product: str = TEK,
    ) -> None:
        self.last = {product: last}
        self.bars_15m = bars_15m
        self.product = product

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        if timeframe == "15m":
            rows = _ohlcv_rows(self.bars_15m, timeframe="15m")
        else:
            # Flat series at last for other TFs (avoid accidental exit/entry signals)
            px = self.last[self.product]
            flat = _pad_flat(max(limit, 60), px)
            rows = _ohlcv_rows(flat, timeframe=timeframe)
        return rows[-limit:] if limit > 0 else rows

    def fetch_ticker(self, product: str) -> Ticker:
        px = self.last.get(product, self.last[self.product])
        return Ticker(product=product, last=px, bid=px - 1.0, ask=px + 1.0, ts=MIDDAY_UTC)


def _stock_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    last: float,
    bars_15m: list[tuple[float, float, float, float]],
    entry: float = ENTRY,
    strategy: str = "ema_15m",
    now: datetime = MIDDAY_UTC,
    stall_enabled: bool = True,
) -> tuple[AppState, StockPaperEngine]:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.setattr("snowball.stocks.engine.utcnow", lambda: now)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="paper",
        stock_live_enabled=False,
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_bankroll_usd=10_000.0,
        stock_products=TEK,
        stock_strategies=strategy,
        stock_max_positions=1,
        stock_max_active=2,
        stock_dynamic_max=0,
        stock_poll_seconds=0.05,
        indicator_filters_enabled=False,
        never_sell_red=True,
        min_take_profit_pct=0.06,
        sma_min_take_profit_pct=0.08,
        fee_buffer_pct=0.01,
        stock_cfm_stall_exit_enabled=stall_enabled,
        stock_cfm_stall_exit_pct=0.05,
        stock_cfm_stall_lookback_bars=5,
        stock_cfm_stall_new_high_tol=0.002,
        stock_cfm_stall_range_compress_pct=0.006,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        slippage_bps=0.0,
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    state.stock_ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.stock_universe_active = [TEK]
    state.stock_pairs[TEK] = PairSnapshot(product=TEK, max_open=1)
    # Seed open TEK lot @ entry (matches live open lot)
    state.stock_ledger.open_buy(
        TEK, entry, 1.0, 0.0, 0.0, "seed", strategy=strategy, ts=now
    )
    market = FakeOhlcMarket(last=last, bars_15m=bars_15m)
    engine = StockPaperEngine(state, market=market)  # type: ignore[arg-type]
    return state, engine


# --- unit: detector ---


def test_price_stalled_true_on_compressed_off_highs() -> None:
    last = 4100.0
    bars = _stalled_15m(last=last, peak=4118.0)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    closes = [b[3] for b in bars]
    assert price_stalled(highs, lows, closes, last, lookback=5) is True


def test_price_stalled_false_when_trending_new_highs() -> None:
    last = 4200.0
    bars = _trending_15m(last=last)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    closes = [b[3] for b in bars]
    assert price_stalled(highs, lows, closes, last, lookback=5) is False


def test_stall_exit_allowed_green_floor() -> None:
    ok, reason = stall_exit_allowed(_lot(), mark=ENTRY * 1.05, stall_exit_pct=0.05, never_sell_red=True)
    assert ok is True and reason == "ok"
    ok, reason = stall_exit_allowed(_lot(), mark=ENTRY * 1.049, stall_exit_pct=0.05, never_sell_red=True)
    assert ok is False and reason == "below_stall_take_profit"


def test_stall_exit_never_sell_red() -> None:
    ok, reason = stall_exit_allowed(_lot(), mark=ENTRY * 0.99, stall_exit_pct=0.05, never_sell_red=True)
    assert ok is False and reason == "never_sell_red"


def test_in_us_cash_session_midday() -> None:
    assert in_us_cash_session(MIDDAY_UTC) is True
    assert in_us_cash_session(AFTER_HOURS_UTC) is False


# --- engine integration ---


def test_stall_plus_5pct_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = ENTRY * 1.055  # ~5.5% green
    bars = _stalled_15m(last=mark, peak=mark * 1.004)
    state, engine = _stock_state(tmp_path, monkeypatch, last=mark, bars_15m=bars)
    assert state.stock_ledger is not None
    assert state.stock_ledger.open_count(TEK) == 1
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 0
    sells = [f for f in state.stock_ledger.recent_fills(10) if f.side == "sell"]
    assert len(sells) == 1
    assert sells[0].reason == "stall_take_profit"


def test_trending_plus_green_holds_for_fade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still making highs → do NOT take 5% stall exit; wait for fade."""
    mark = ENTRY * 1.06
    bars = _trending_15m(last=mark)
    state, engine = _stock_state(tmp_path, monkeypatch, last=mark, bars_15m=bars)
    assert state.stock_ledger is not None
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 1


def test_red_never_stall_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = ENTRY * 0.97
    bars = _stalled_15m(last=mark, peak=mark * 1.004)
    state, engine = _stock_state(tmp_path, monkeypatch, last=mark, bars_15m=bars)
    assert state.stock_ledger is not None
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 1


def test_below_5pct_plus_stall_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = ENTRY * 1.04  # 4% < 5%
    bars = _stalled_15m(last=mark, peak=mark * 1.004)
    state, engine = _stock_state(tmp_path, monkeypatch, last=mark, bars_15m=bars)
    assert state.stock_ledger is not None
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 1


def test_fade_when_green_enough_still_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing fade path: not-stalled / no stall bank, but fade + swing floor → exit."""
    # sma_15m fade: last < SMA20 and last > SMA50; green >= effective SMA floor 9%.
    # Series: 40@100 + 20@120 + last 112 → SMA20=120, SMA50=108; mark 112 fades.
    entry = 100.0
    mark = 112.0  # +12% >= 9% floor
    closes = [100.0] * 40 + [120.0] * 20 + [112.0]
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.setattr("snowball.stocks.engine.utcnow", lambda: MIDDAY_UTC)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=True,
        stock_mode="paper",
        stock_sqlite_path=tmp_path / "stocks.db",
        stock_bankroll_usd=10_000.0,
        stock_products=TEK,
        stock_strategies="sma_15m",
        stock_max_positions=1,
        stock_max_active=2,
        stock_dynamic_max=0,
        indicator_filters_enabled=False,
        never_sell_red=True,
        min_take_profit_pct=0.06,
        sma_min_take_profit_pct=0.08,
        fee_buffer_pct=0.01,
        # Disable stall so this asserts the fade path specifically
        stock_cfm_stall_exit_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        slippage_bps=0.0,
        trend_filter_enabled=False,
    )
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    state.stock_ledger = PaperLedger(settings.stock_sqlite_path, settings.stock_bankroll_usd)
    state.stock_universe_active = [TEK]
    state.stock_pairs[TEK] = PairSnapshot(product=TEK, max_open=1)
    state.stock_ledger.open_buy(
        TEK, entry, 1.0, 0.0, 0.0, "seed", strategy="sma_15m", ts=MIDDAY_UTC
    )

    class M:
        mark_source = "yahoo_paper"

        def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
            series = closes if timeframe == "15m" else [mark] * 60
            return _ohlcv_rows([(c, c, c, c) for c in series[-limit:]], timeframe=timeframe)

        def fetch_ticker(self, product: str) -> Ticker:
            return Ticker(
                product=product, last=mark, bid=mark - 0.1, ask=mark + 0.1, ts=MIDDAY_UTC
            )

    engine = StockPaperEngine(state, market=M())  # type: ignore[arg-type]
    engine.refresh_universe = lambda force=False: None  # type: ignore[method-assign]
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 0
    sells = [f for f in state.stock_ledger.recent_fills(10) if f.side == "sell"]
    assert len(sells) == 1
    assert sells[0].reason == "sma_15m:fade"


def test_outside_cash_hours_no_stall_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = ENTRY * 1.055
    bars = _stalled_15m(last=mark, peak=mark * 1.004)
    state, engine = _stock_state(
        tmp_path, monkeypatch, last=mark, bars_15m=bars, now=AFTER_HOURS_UTC
    )
    assert state.stock_ledger is not None
    engine.tick()
    assert state.stock_ledger.open_count(TEK) == 1
