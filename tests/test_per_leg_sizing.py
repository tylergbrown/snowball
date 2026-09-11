"""Tests for shared per-leg notional autoscale (all lanes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowball.config import Settings
from snowball.crash.engine import CrashEngine
from snowball.crash.store import CrashStore
from snowball.engine import Engine
from snowball.fed.engine import FedEngine
from snowball.fed.store import FedStore
from snowball.futures.engine import FuturesEngine
from snowball.market import MarketData
from snowball.paper import PaperLedger
from snowball.sizing import effective_per_leg_from_settings, per_leg_notional_usd
from snowball.snapshot import build_snapshot
from snowball.state import AppState
from snowball.stocks.engine import StockPaperEngine as StockEngine


def test_per_leg_formula_examples() -> None:
    assert per_leg_notional_usd(1000.0) == 110.0
    assert per_leg_notional_usd(2000.0) == 120.0
    assert per_leg_notional_usd(50.0) == 100.0  # floor at BASE
    assert per_leg_notional_usd(0.0) == 100.0
    assert per_leg_notional_usd(99.99) == 100.0
    assert per_leg_notional_usd(100.0) == 101.0
    assert per_leg_notional_usd(900.0) == 109.0
    assert per_leg_notional_usd(1900.0) == 119.0


def test_per_leg_autoscale_flag_off_fixed_base() -> None:
    assert per_leg_notional_usd(5000.0, base_usd=100.0, autoscale=False) == 100.0


def test_per_leg_custom_base_and_scale() -> None:
    # BASE=200, +2% of base per $100 AV → at AV=1000: blocks=10 → 200*(1+0.02*10)=240
    assert (
        per_leg_notional_usd(
            1000.0, base_usd=200.0, scale_per_100_usd_pct=2.0, autoscale=True
        )
        == 240.0
    )


def test_settings_effective_per_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PER_LEG_AUTOSCALE", raising=False)
    s = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "t.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        per_leg_base_usd=100.0,
        per_leg_scale_per_100_usd_pct=1.0,
        per_leg_autoscale=True,
    )
    assert s.effective_per_leg_notional_usd(1000.0) == 110.0
    assert effective_per_leg_from_settings(s, 2000.0) == 120.0
    s2 = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        halt_file=tmp_path / "HALT2",
        sqlite_path=tmp_path / "t2.db",
        heartbeat_path=tmp_path / "hb2",
        stock_enabled=False,
        futures_enabled=False,
        crash_enabled=False,
        fed_enabled=False,
        per_leg_autoscale=False,
    )
    assert s2.effective_per_leg_notional_usd(9999.0) == 100.0


class _DummyMarket(MarketData):
    def fetch_ticker(self, product: str):  # type: ignore[override]
        raise NotImplementedError

    def fetch_ohlcv(self, product: str, timeframe: str, limit: int):  # type: ignore[override]
        return []


def test_crypto_leg_uses_helper(app_state: AppState) -> None:
    eng = Engine(app_state, market=_DummyMarket())
    # Paper equity starts at bankroll 1000 → per_leg 110
    assert eng._crypto_leg_notional() == 110.0


def test_stock_target_leg_uses_helper(app_state: AppState, tmp_path: Path) -> None:
    settings = app_state.settings
    object.__setattr__(settings, "stock_enabled", True)  # may be frozen? try assign
    try:
        settings.stock_enabled = True  # type: ignore[misc]
    except Exception:
        pass
    app_state.stock_ledger = PaperLedger(tmp_path / "stocks.db", 1000.0)
    eng = StockEngine(app_state)
    eng._last_budget = {
        "account_value_usd": 1000.0,
        "budget_usd": 320.0,
        "open_notional_usd": 0.0,
    }
    assert eng._target_leg_notional() == 110.0
    eng._last_budget = {
        "account_value_usd": 50.0,
        "budget_usd": 16.0,
        "open_notional_usd": 0.0,
    }
    # remaining budget 16 < floor 100 → leg sized to remaining
    assert eng._target_leg_notional() == 16.0
    eng._last_budget = {
        "account_value_usd": 2000.0,
        "budget_usd": 640.0,
        "open_notional_usd": 0.0,
    }
    assert eng._target_leg_notional() == 120.0


class _AvMarket:
    mark_source = "test"

    def __init__(self, av: float = 1000.0) -> None:
        self.av = av

    def fetch_account_value_usd(self, *, crypto_marks: Any = None) -> float:
        return float(self.av)


def test_futures_crash_fed_budget_ceiling_uses_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        mode="paper",
        live_enabled=False,
        halt_file=tmp_path / "HALT",
        sqlite_path=tmp_path / "c.db",
        heartbeat_path=tmp_path / "hb",
        stock_enabled=False,
        futures_enabled=True,
        crash_enabled=True,
        fed_enabled=True,
        futures_mode="paper",
        crash_mode="paper",
        fed_mode="paper",
        per_leg_autoscale=True,
        per_leg_base_usd=100.0,
        per_leg_scale_per_100_usd_pct=1.0,
        futures_account_budget_pct=0.20,
        crash_account_budget_pct=0.10,
        fed_account_budget_pct=0.05,
        futures_sqlite_path=tmp_path / "ft.db",
        crash_sqlite_path=tmp_path / "cg.db",
        fed_sqlite_path=tmp_path / "fd.db",
    )
    state = AppState(settings=settings, ledger=PaperLedger(tmp_path / "crypto.db", 1000.0))
    state.futures_ledger = PaperLedger(tmp_path / "ft.db", 1000.0)
    state.crash_ledger = CrashStore(tmp_path / "cg.db", 1000.0)
    state.fed_ledger = FedStore(tmp_path / "fd.db", 1000.0)

    marks = {"US5-19DEC30-CDE": 500.0, "TEK-19DEC30-CDE": 400.0}
    ft = FuturesEngine(state, market=_AvMarket(1000.0))  # type: ignore[arg-type]
    ft._refresh_budget(marks)
    # CFM: paper equity ~1000; budget 200 / 2 = 100 (no PER_LEG soft cap)
    assert ft._last_budget["per_leg_notional_usd"] == 110.0
    assert ft._last_budget["per_index_usd"] == 100.0

    # High equity: CFM keeps full budget/n (PER_LEG does not bind)
    state.futures_ledger = PaperLedger(tmp_path / "ft2.db", 5000.0)
    ft = FuturesEngine(state, market=_AvMarket(5000.0))  # type: ignore[arg-type]
    ft._refresh_budget(marks)
    av = ft._last_budget["account_value_usd"]
    per_leg = settings.effective_per_leg_notional_usd(av)
    assert ft._last_budget["per_leg_notional_usd"] == per_leg
    assert ft._last_budget["per_index_usd"] == pytest.approx(av * 0.20 / 2.0)

    cg = CrashEngine(state, market=_AvMarket(1000.0))  # type: ignore[arg-type]
    cg._refresh_budget(marks)
    assert cg._last_budget["per_leg_notional_usd"] == 110.0
    assert cg._last_budget["per_index_usd"] == pytest.approx(1000.0 * 0.10 / 2.0)

    fd = FedEngine(state, market=_AvMarket(1000.0))  # type: ignore[arg-type]
    fd._refresh_budget(marks)
    assert fd._last_budget["per_leg_notional_usd"] == 110.0
    assert fd._last_budget["per_index_usd"] == pytest.approx(1000.0 * 0.05 / 2.0)


def test_snapshot_shows_effective_per_leg(app_state: AppState) -> None:
    snap = build_snapshot(app_state)
    risk = snap["risk"]
    assert risk["per_leg_autoscale"] is True
    assert risk["per_leg_base_usd"] == 100.0
    assert risk["effective_per_leg_notional_usd"] == 110.0
    assert risk["max_position_notional_usd"] == 110.0
