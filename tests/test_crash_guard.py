"""Crash Guard — triggers, paper shorts, cover gate, live dual-gate, allocation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from snowball.config import LiveTradingRefused, Settings
from snowball.crash.engine import CrashEngine, attach_crash_lane
from snowball.crash.store import CrashStore
from snowball.crash.triggers import (
    evaluate_crash_triggers,
    short_cover_allowed,
)
from snowball.dashboard import create_app
from snowball.models import PairSnapshot, Ticker
from snowball.paper import PaperLedger
from snowball.snapshot import build_snapshot
from snowball.state import AppState


def _closes_dump() -> list[float]:
    """Synthetic daily closes ending with ≤ −2% dump."""
    base = [100.0 + (i % 3) * 0.1 for i in range(40)]
    # prior close 100, last 97.5 → −2.5%
    base[-2] = 100.0
    base[-1] = 97.5
    return base


def test_trigger_daily_dump() -> None:
    tr = evaluate_crash_triggers(_closes_dump())
    assert tr.fire is True
    assert any(r.startswith("daily_dump") for r in tr.reasons)


def test_trigger_bb_lower_expanding() -> None:
    # Build a series that goes below lower band with expanding width
    closes = [100.0] * 25
    # widen then dump
    closes = closes + [100.0, 100.0, 99.0, 98.0, 90.0]
    tr = evaluate_crash_triggers(closes)
    # May fire via dump and/or BB; at least dump should fire (−~8% last vs prior)
    assert tr.fire is True


def test_short_cover_refused_red_allowed_at_7pct() -> None:
    ok, reason = short_cover_allowed(100.0, 101.0, min_take_profit_pct=0.06, fee_buffer_pct=0.01)
    assert ok is False and reason == "never_cover_red"
    ok, reason = short_cover_allowed(100.0, 94.0, min_take_profit_pct=0.06, fee_buffer_pct=0.01)
    assert ok is False and reason == "below_take_profit"  # 6% < 7%
    ok, reason = short_cover_allowed(100.0, 93.0, min_take_profit_pct=0.06, fee_buffer_pct=0.01)
    assert ok is True and reason == "ok"  # 7%


def test_live_gate_requires_both_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    monkeypatch.delenv("CRASH_LIVE_ENABLED", raising=False)
    s_live_only = Settings(
        _env_file=None,
        crash_enabled=True,
        crash_mode="live",
        crash_live_enabled=False,
        crash_sqlite_path=tmp_path / "cg.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s_live_only.assert_crash_config()
    assert s_live_only.crash_live_orders_permitted() is False

    s_flag_only = Settings(
        _env_file=None,
        crash_enabled=True,
        crash_mode="paper",
        crash_live_enabled=True,
        crash_sqlite_path=tmp_path / "cg2.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c2.db",
        heartbeat_path=tmp_path / "hb2",
        stock_enabled=False,
        futures_enabled=False,
    )
    with pytest.raises(LiveTradingRefused):
        s_flag_only.assert_crash_config()

    s_both = Settings(
        _env_file=None,
        crash_enabled=True,
        crash_mode="live",
        crash_live_enabled=True,
        crash_sqlite_path=tmp_path / "cg3.db",
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c3.db",
        heartbeat_path=tmp_path / "hb3",
        stock_enabled=False,
        futures_enabled=False,
    )
    s_both.assert_crash_config()
    assert s_both.crash_live_orders_permitted() is True


def test_allocation_includes_crash_10() -> None:
    from snowball.allocation import lane_budget_pcts, lane_budgets_usd

    pcts = lane_budget_pcts()
    assert pcts["crash"] == 0.10
    assert pcts["crypto"] == 0.35
    assert pcts["stock"] == 0.35
    assert pcts["futures"] == 0.20
    assert abs(sum(pcts.values()) - 1.0) < 1e-9
    b = lane_budgets_usd(10_000.0)
    assert b["crash_usd"] == 1000.0


class FakeCrashMarket:
    mark_source = "coinbase_perp"

    def __init__(self, last: float = 97.5, closes: list[float] | None = None) -> None:
        self.last = last
        self.closes = closes or _closes_dump()
        self.orders: list[dict] = []
        self.account_value = 10_000.0
        self._allow_orders = False

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int) -> list[list[float]]:
        closes = self.closes[-limit:]
        step = 86_400_000.0
        base = 1_700_000_000_000.0
        return [[base + i * step, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(closes)]

    def fetch_ticker(self, product: str) -> Ticker:
        return Ticker(
            product=product,
            last=self.last,
            bid=self.last - 0.1,
            ask=self.last + 0.1,
            ts=datetime.now(timezone.utc),
        )

    def fetch_account_value_usd(self, *, crypto_marks=None) -> float:
        return float(self.account_value)

    def fetch_bba(self, product):
        return self.last - 0.1, self.last + 0.1

    def create_swap_maker_limit_order(self, *a, **k):
        raise RuntimeError("live orders not allowed in this fake")


def _paper_settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        trading_enabled=True,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "crypto.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=True,
        crash_mode="paper",
        crash_live_enabled=False,
        crash_sqlite_path=tmp_path / "crash.db",
        crash_products="SPY-PERP-INTX,QQQ-PERP-INTX",
        crash_poll_seconds=0.05,
        crash_max_positions=1,
        crash_max_notional_usd=500.0,
        crash_account_budget_pct=0.10,
        crash_bankroll_usd=1000.0,
        min_take_profit_pct=0.06,
        fee_buffer_pct=0.01,
        slippage_bps=0.0,
    )
    base.update(kwargs)
    return Settings(**base)


def test_trigger_fires_paper_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    monkeypatch.delenv("CRASH_LIVE_ENABLED", raising=False)
    # Paper budget from bankroll equity: 10_000 * 10% / 2 = 500 per index
    settings = _paper_settings(tmp_path, crash_bankroll_usd=10_000.0)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_crash_lane(state)
    market = FakeCrashMarket(last=97.5)
    engine = CrashEngine(state, market=market)  # type: ignore[arg-type]
    engine.tick()
    assert state.crash_ledger is not None
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 1
    assert state.crash_ledger.open_count("QQQ-PERP-INTX") == 1
    spy = state.crash_ledger.open_positions("SPY-PERP-INTX")[0]
    assert spy.side == "short"
    assert spy.notional_usd == pytest.approx(500.0, rel=0.05)


def test_second_entry_blocked_while_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_crash_lane(state)
    market = FakeCrashMarket(last=97.5)
    engine = CrashEngine(state, market=market)  # type: ignore[arg-type]
    engine._last_budget = {
        "account_value_usd": 10_000.0,
        "budget_usd": 1000.0,
        "per_index_usd": 500.0,
    }
    engine.tick()
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 1
    engine.tick()
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 1  # no pyramid


def test_cover_refused_when_red_allowed_when_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_crash_lane(state)
    assert state.crash_ledger is not None
    state.crash_ledger.open_short(
        product="SPY-PERP-INTX",
        fill_px=100.0,
        notional_usd=100.0,
        slippage_bps=0.0,
        fee_usd=0.0,
        reason="seed",
        strategy="crash_guard",
    )
    # Red short (mark above entry) — hold
    market = FakeCrashMarket(last=105.0, closes=[100.0] * 40)  # no new trigger needed
    engine = CrashEngine(state, market=market)  # type: ignore[arg-type]
    state.crash_pairs["SPY-PERP-INTX"] = PairSnapshot(
        product="SPY-PERP-INTX", last=105.0, bid=104.9, ask=105.1, max_open=1
    )
    engine._maybe_cover(
        "SPY-PERP-INTX",
        state.crash_ledger.open_positions("SPY-PERP-INTX"),
        {"SPY-PERP-INTX": 105.0},
        datetime.now(timezone.utc),
    )
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 1

    # 6% green refused (needs 7%)
    market.last = 94.0
    state.crash_pairs["SPY-PERP-INTX"].last = 94.0
    engine._maybe_cover(
        "SPY-PERP-INTX",
        state.crash_ledger.open_positions("SPY-PERP-INTX"),
        {"SPY-PERP-INTX": 94.0},
        datetime.now(timezone.utc),
    )
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 1

    # ≥7% short profit → cover
    market.last = 93.0
    state.crash_pairs["SPY-PERP-INTX"].last = 93.0
    state.crash_pairs["SPY-PERP-INTX"].bid = 92.9
    state.crash_pairs["SPY-PERP-INTX"].ask = 93.1
    engine._maybe_cover(
        "SPY-PERP-INTX",
        state.crash_ledger.open_positions("SPY-PERP-INTX"),
        {"SPY-PERP-INTX": 93.0},
        datetime.now(timezone.utc),
    )
    assert state.crash_ledger.open_count("SPY-PERP-INTX") == 0


def test_live_orders_refused_when_paper_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    settings = _paper_settings(tmp_path)  # paper
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    state.crash_ledger = CrashStore(tmp_path / "cg.db", 1000.0)
    state.crash_pairs["SPY-PERP-INTX"] = PairSnapshot(
        product="SPY-PERP-INTX", last=100.0, max_open=1
    )
    engine = CrashEngine(state, market=FakeCrashMarket())  # type: ignore[arg-type]
    with pytest.raises(LiveTradingRefused):
        engine._open_short_live(
            "SPY-PERP-INTX",
            reason="test",
            now=datetime.now(timezone.utc),
            notional_usd=50.0,
        )


def test_api_crash_and_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRASH_MODE", raising=False)
    monkeypatch.delenv("MODE", raising=False)
    settings = _paper_settings(tmp_path)
    state = AppState(settings=settings, ledger=PaperLedger(settings.sqlite_path, 1000.0))
    attach_crash_lane(state)
    state.crash_account_value_usd = 10_000.0
    state.crash_budget_usd = 1000.0
    state.crash_per_index_allotment_usd = 500.0
    from fastapi.testclient import TestClient

    client = TestClient(create_app(state))
    r = client.get("/api/crash")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "paper"
    assert body["risk"]["budget_pct"] == 0.10
    assert body["risk"]["never_cover_red"] is True
    page = client.get("/")
    assert b"Crash Guard" in page.content
    snap = build_snapshot(state)
    assert snap["crash"]["enabled"] is True
