"""FED_RESEARCH_ENABLED keeps the FedWatch research poll running while FED_ENABLED=false
(Tb 2026-10-16 6:55 PM CT), and no path can place a Fed order while FED_ENABLED=false.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from types import SimpleNamespace

import pytest

from snowball.config import Settings
from snowball.fed import engine as fe


def _settings(tmp_path, **kw) -> Settings:
    base = dict(fed_sqlite_path=tmp_path / "snowball_fed.db", fed_enabled=False)
    base.update(kw)
    return Settings(_env_file=None, **base)


def _state(settings):
    return SimpleNamespace(settings=settings, fed_ledger=None, fed_engine=None,
                           fed_research_engine=None, fed_last_research={}, watcher=None, running=True)


@pytest.fixture
def fake_fedwatch(monkeypatch):
    monkeypatch.setattr(fe, "summarize_fedwatch_payload",
                        lambda p: {"next_meeting_date": "2026-10-28", "p_hold": 0.2, "p_cut": 0.8,
                                   "p_hike": 0.0, "next_meeting": {"probabilities": {}},
                                   "meetings": [{"date": "2026-10-28", "p_cut": 0.8}]})
    monkeypatch.setitem(sys.modules, "cme_fedwatch",
                        types.SimpleNamespace(get_probabilities=lambda: {"raw": True},
                                              get_history=lambda: {"lookback": []}))


def test_default_on():
    assert Settings(_env_file=None).fed_research_enabled is True


def test_research_only_polls_and_writes_snapshot(tmp_path, fake_fedwatch):
    st = _state(_settings(tmp_path))
    runner = fe.attach_fed_research(st)
    assert runner is not None and st.fed_research_engine is runner
    # no Fed lane visible to any trading path, and no market/order client at all
    assert st.fed_ledger is None and st.fed_engine is None and runner.market is None
    runner.tick()
    assert st.fed_last_research["p_cut"] == 0.8
    c = sqlite3.connect(tmp_path / "snowball_fed.db")
    rows = c.execute("SELECT meeting_date, payload_json FROM research_snapshots").fetchall()
    assert len(rows) == 1 and rows[0][0] == "2026-10-28"
    assert json.loads(rows[0][1])["p_cut"] == 0.8
    meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
    assert meta.get("last_research_at") and "last_research_json" in meta
    # 3-hour interval respected: second tick does not re-poll
    runner.tick()
    assert c.execute("SELECT count(*) FROM research_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["_open_long", "_open_short", "_open_side_live", "_close_lot",
                                  "_close_lot_live", "_act_on_product", "_maybe_exit", "_stop_book",
                                  "_bet_decision", "_update_pair", "_refresh_budget", "_roll"])
def test_research_runner_refuses_order_paths(tmp_path, name):
    runner = fe.attach_fed_research(_state(_settings(tmp_path)))
    with pytest.raises(RuntimeError):
        getattr(runner, name)


def test_not_attached_when_fed_enabled_or_research_off(tmp_path):
    assert fe.attach_fed_research(_state(_settings(tmp_path, fed_enabled=True, fed_mode="paper"))) is None
    assert fe.attach_fed_research(_state(_settings(tmp_path, fed_research_enabled=False))) is None


def test_full_fed_engine_tick_noop_when_disabled(tmp_path):
    touched = []

    class _Market:
        def __getattr__(self, name):
            touched.append(name)
            raise AssertionError(f"market.{name} used while FED_ENABLED=false")

    st = _state(_settings(tmp_path))
    st.fed_pairs = {}
    eng = fe.FedEngine.__new__(fe.FedEngine)
    eng.state, eng.market, eng._roll = st, _Market(), None
    eng._last_research, eng._last_research_poll_mono = {}, 0.0
    eng.tick()
    assert touched == []


def test_research_toggle_off_at_runtime(tmp_path, fake_fedwatch):
    st = _state(_settings(tmp_path))
    runner = fe.attach_fed_research(st)
    st.settings = _settings(tmp_path, fed_research_enabled=False)
    runner.tick()
    assert st.fed_last_research == {}


def test_disabled_snapshot_shows_research(tmp_path, fake_fedwatch):
    from snowball.fed.snapshot import build_fed_snapshot

    st = _state(_settings(tmp_path, fed_disabled_note="pending re-evaluation"))
    fe.attach_fed_research(st).tick()
    snap = build_fed_snapshot(st)
    assert snap["enabled"] is False and snap["research_only"] is True
    assert snap["research"]["p_cut"] == 0.8
    assert snap["positions"] == [] and snap["bet_eligible"] is False
