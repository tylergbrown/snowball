"""FUTURES_STOCK_ENTRY_DISABLED blocks NEW futures-lane entries for listed merged stock
strategies while their existing lots keep being managed (Tb 2026-10-16 6:42 PM CT:
"turn off futures bb_15m for new trades"). Also FED/CRASH disabled-note labels.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from snowball.config import Settings
from snowball.futures import stock_strategies as ss
from snowball.models import PairSnapshot, Signal

US5 = "US5-19DEC30-CDE"
NOW = datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


class _Ledger:
    def __init__(self, lots):
        self.lots = lots

    def open_positions(self, product=None):
        return [l for l in self.lots if product is None or l.product == product]

    def last_entry_at(self, product, strategy):
        return None

    def cash_usd(self):
        return 10_000.0

    def open_count(self, product=None):
        return len(self.open_positions(product))

    def is_pair_paused(self, product, now):
        return False


class _Engine:
    def __init__(self, settings, lots):
        self.state = SimpleNamespace(settings=settings, futures_ledger=_Ledger(lots),
                                     futures_stock_pairs={US5: PairSnapshot(product=US5, last=3000.0)},
                                     futures_pairs={}, crash_ledger=None)
        self.closed = []
        self.opened = []

    def product_list(self):
        return [US5]

    def _close_lots(self, product, lots, marks, *, reason, now):
        self.closed.append((product, [l.id for l in lots], reason))

    def _open_lot(self, product, marks, **kw):
        self.opened.append((product, kw))

    def _expiry_blocks_entry(self, product, now):
        return False

    def _session_entry_notional(self, product, ledger):
        return 3000.0


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(ss.signal_context, "stage", lambda *a, **k: None)
    monkeypatch.setattr(ss, "lot_unrealized_pnl_pct", lambda lot, mark: 1.0)


def _lot(i=20, strategy="bb_15m"):
    return SimpleNamespace(id=i, product=US5, side="long", qty=1.0, entry_price=2900.0,
                           gate_entry_price=2900.0, strategy=strategy)


def test_settings_parse_and_spot_untouched():
    s = _settings(futures_stock_entry_disabled=" BB_15M , ")
    assert s.futures_stock_entry_disabled_list == ["bb_15m"]
    assert "bb_15m" in s.futures_stock_strategy_list  # still runs (manages its lots)
    assert "bb_15m" in s.strategy_list  # spot crypto unaffected
    assert _settings().futures_stock_entry_disabled_list == []


def test_entry_blocked_when_disabled(monkeypatch):
    monkeypatch.setattr(ss, "signal_for_strategy", lambda snap, sid: (Signal.ENTER, True))
    called = []
    monkeypatch.setattr(ss, "family_lots", lambda *a: called.append(1) or [])
    eng = _Engine(_settings(futures_stock_entry_disabled="bb_15m"), [])
    m = ss.MergedStockStrategies(eng)
    m.act(US5, "bb_15m", now=NOW, halted=False, can_trade=True, daily_killed=False, marks={US5: 3000.0})
    assert eng.opened == [] and called == []
    assert m.last_decisions[US5]["bb_15m"]["decision"] == "entry_disabled"
    assert m.payload()["entry_disabled"] == ["bb_15m"]


def test_other_strategy_and_default_still_reach_entry_gates(monkeypatch):
    monkeypatch.setattr(ss, "signal_for_strategy", lambda snap, sid: (Signal.ENTER, True))
    monkeypatch.setattr(ss, "family_lots", lambda *a: [_lot(99, "rsi_15m")])
    for settings, sid in ((_settings(futures_stock_entry_disabled="bb_15m"), "rsi_15m"),
                          (_settings(), "bb_15m")):
        eng = _Engine(settings, [])
        m = ss.MergedStockStrategies(eng)
        m.act(US5, sid, now=NOW, halted=False, can_trade=True, daily_killed=False, marks={US5: 3000.0})
        assert m.last_decisions[US5][sid]["decision"] == "entry_skipped_one_net_position"


def test_existing_lot_still_exits_when_entry_disabled(monkeypatch):
    monkeypatch.setattr(ss, "signal_for_strategy", lambda snap, sid: (Signal.EXIT, False))
    monkeypatch.setattr(ss, "strategy_exit_allowed", lambda *a, **k: (True, ""))
    monkeypatch.setattr(ss, "allow_exit", lambda ctx: (True, ""))
    eng = _Engine(_settings(futures_stock_entry_disabled="bb_15m"), [_lot(20)])
    m = ss.MergedStockStrategies(eng)
    assert m.owns(_lot(20)) and US5 in m.products()
    m.act(US5, "bb_15m", now=NOW, halted=False, can_trade=True, daily_killed=False, marks={US5: 3000.0})
    assert eng.closed == [(US5, [20], "bb_15m:exit")]


def test_existing_lot_holding_when_no_exit(monkeypatch):
    monkeypatch.setattr(ss, "signal_for_strategy", lambda snap, sid: (Signal.HOLD, False))
    eng = _Engine(_settings(futures_stock_entry_disabled="bb_15m"), [_lot(20)])
    m = ss.MergedStockStrategies(eng)
    m.act(US5, "bb_15m", now=NOW, halted=False, can_trade=True, daily_killed=False, marks={US5: 3000.0})
    assert m.last_decisions[US5]["bb_15m"]["decision"] == "holding"
    assert eng.opened == [] and eng.closed == []


def test_fed_crash_disabled_labels():
    from snowball.crash.snapshot import build_crash_snapshot
    from snowball.fed.snapshot import build_fed_snapshot

    st = SimpleNamespace(settings=_settings(fed_enabled=False, crash_enabled=False,
                                            fed_disabled_note="pending re-evaluation",
                                            crash_disabled_note="pending re-evaluation"),
                         fed_ledger=None, crash_ledger=None)
    f, c = build_fed_snapshot(st), build_crash_snapshot(st)
    assert f["enabled"] is False and f["label"] == "Fed Desk (disabled - pending re-evaluation)"
    assert c["enabled"] is False and c["label"] == "Crash Guard (disabled - pending re-evaluation)"
    st2 = SimpleNamespace(settings=_settings(fed_enabled=False, crash_enabled=False),
                          fed_ledger=None, crash_ledger=None)
    assert build_fed_snapshot(st2)["label"] == "Fed Desk (disabled)"
    assert build_crash_snapshot(st2)["disabled_note"] is None
