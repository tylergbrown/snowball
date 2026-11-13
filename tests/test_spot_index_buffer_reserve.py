"""Spot-buy CFM buffer reserve: while N US5/TEK lots are open, a new trading-lane
spot buy may not leave the liquidation buffer under N × $300 (Tb via nala 2026-11-11).

Does not change stops, SPOT_USD_FLOOR, never-sell-red, or the futures:* slot reservation.
Treasury dividend and the $10 dip-buy stay exempt (they never call the trading-lane gate).
"""
from __future__ import annotations

import inspect
import time
from types import SimpleNamespace

import pytest

from snowball import engine as eng
from snowball.config import Settings
from snowball.futures.stops import open_index_n, spot_index_buffer_eval
from snowball.treasury import dip_buy as dip


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _state(*, n_ex=None, buffer=None, live=False, **kw):
    s = _settings(spot_usd_floor=300.0, **kw)
    st = SimpleNamespace(settings=s, spot_floor_stats={})
    if n_ex is not None:
        st.futures_exchange_net = {"mono": time.monotonic(), "at": "t",
                                   "net": {"US5-19DEC30-CDE": float(n_ex)}}
    if buffer is not None:
        st.futures_stop_views = {
            "futures": {"at": "2099-01-01T00:00:00+00:00",
                        "liq": {"available": True, "buffer_usd": float(buffer)}},
        }
    if live:
        st.settings = _settings(spot_usd_floor=300.0, mode="live", live_enabled=True, **kw)
    return st


def test_defaults_on_and_300():
    s = _settings()
    assert s.spot_index_buffer_reserve_enabled is True
    assert s.spot_index_buffer_reserve_usd == 300.0


def test_n0_allows_regardless_of_buffer():
    ev = spot_index_buffer_eval(_state(n_ex=0, buffer=50.0), cost_usd=100.6)
    assert ev["ok"] and ev["why"] == "no_open_index" and ev["n"] == 0


def test_n1_850_allows_typical_spot_buy():
    """11/11 8:00 AM CT buffer $850.38, US5 #26 open (N=1): a $100 buy must still pass."""
    ev = spot_index_buffer_eval(_state(n_ex=1, buffer=850.38), cost_usd=100.6)
    assert ev["ok"], ev
    assert ev["need_usd"] == 300.0
    assert ev["after_usd"] == pytest.approx(749.78)


def test_n1_blocks_when_after_would_fall_under_300():
    ev = spot_index_buffer_eval(_state(n_ex=1, buffer=350.0), cost_usd=100.6)
    assert not ev["ok"]
    assert ev["why"].startswith("spot_index_buffer_")
    assert ev["after_usd"] == pytest.approx(249.4)
    assert ev["need_usd"] == 300.0


def test_n2_reserve_is_600():
    allow = spot_index_buffer_eval(_state(n_ex=2, buffer=850.0), cost_usd=100.6)
    block = spot_index_buffer_eval(_state(n_ex=2, buffer=650.0), cost_usd=100.6)
    assert allow["ok"] and allow["need_usd"] == 600.0
    assert not block["ok"] and block["after_usd"] == pytest.approx(549.4)


def test_pending_inflight_counts_against_buffer():
    ev = spot_index_buffer_eval(_state(n_ex=1, buffer=400.0), cost_usd=100.6, pending_usd=50.0)
    assert not ev["ok"]
    assert ev["after_usd"] == pytest.approx(249.4)


def test_disabled_or_zero_is_off():
    ev = spot_index_buffer_eval(_state(n_ex=1, buffer=50.0, spot_index_buffer_reserve_enabled=False),
                                cost_usd=100.6)
    assert ev["ok"] and ev["why"] == "reserve_off"
    ev2 = spot_index_buffer_eval(_state(n_ex=1, buffer=50.0, spot_index_buffer_reserve_usd=0.0),
                                 cost_usd=100.6)
    assert ev2["ok"] and ev2["why"] == "reserve_off"


def test_live_fails_closed_when_buffer_unknown_and_lot_open():
    ev = spot_index_buffer_eval(_state(n_ex=1, live=True), cost_usd=100.6)
    assert not ev["ok"] and ev["why"] == "spot_index_buffer_data_unavailable"


def test_non_live_without_buffer_stays_open():
    ev = spot_index_buffer_eval(_state(n_ex=1), cost_usd=100.6)
    assert ev["ok"] and ev["why"] == "no_buffer_non_live"


def test_open_index_n_prefers_exchange_net():
    st = _state(n_ex=1)
    n, src = open_index_n(st)
    assert n == 1 and src == "exchange_net"


def test_engine_gate_blocks_and_still_enforces_spot_floor():
    e = eng.Engine.__new__(eng.Engine)
    e.state = _state(n_ex=1, buffer=850.38)
    eng._SPOT_INFLIGHT.clear()
    # Floor: 420-100.6 = 319.4 >= 300, and buffer after 749.78 >= 300, so both pass.
    assert e._spot_floor_allows("BTC-USD", 100.0, 420.0, "t", source="exchange")
    # Floor still blocks first when cash would breach $300, even if the buffer is ample.
    e.state = _state(n_ex=1, buffer=5000.0)
    assert not e._spot_floor_allows("BTC-USD", 100.0, 350.0, "t", source="exchange")
    assert e.state.spot_floor_stats["last_blocked"]["floor_usd"] == 300.0
    # Buffer reserve blocks even when the $300 cash floor would pass.
    e.state = _state(n_ex=1, buffer=350.0)
    assert not e._spot_floor_allows("BTC-USD", 100.0, 900.0, "t", source="exchange")
    assert "spot_index_buffer" in str(e.state.spot_floor_stats["last_blocked"]["reason"])


def test_dividend_and_dip_buy_do_not_call_the_gate():
    import snowball.treasury.dca as dca
    import tools.treasury_dca as tools_dca

    for mod in (dca, tools_dca, dip):
        src = inspect.getsource(mod)
        assert "spot_index_buffer_eval" not in src
        assert "_spot_floor_allows" not in src
