"""Treasury dip buy (Tb 2026-10-30 4:46 PM CT; image live-20261030a)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from snowball.config import Settings
from snowball.treasury import dip_buy as D

T0 = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)


def _w(tmp_path):
    return D.DipBuyWatcher(tmp_path / "treasury.db", pct=0.05, window_min=60.0, usd=10.0)


def _feed(w, pts):
    out = None
    for mins, px in pts:
        r = w.observe(T0 + timedelta(minutes=mins), px)
        out = r or out
    return out


def _contribs(db):
    con = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(contributions)")]
        return [dict(zip(cols, r)) for r in con.execute("SELECT * FROM contributions")]
    finally:
        con.close()


def test_defaults_enabled():
    s = Settings(_env_file=None)
    assert s.treasury_dip_buy_enabled is True
    assert (s.treasury_dip_buy_pct, s.treasury_dip_buy_window_min, s.treasury_dip_buy_usd) == (0.05, 60.0, 10.0)


def test_env_flag_disables(monkeypatch):
    monkeypatch.setenv("TREASURY_DIP_BUY_ENABLED", "false")
    assert Settings(_env_file=None).treasury_dip_buy_enabled is False


def test_fires_at_5pct_within_60min_not_below(tmp_path):
    w = _w(tmp_path)
    assert _feed(w, [(0, 80000), (20, 77000), (40, 76040)]) is None  # -4.95%
    trig = w.observe(T0 + timedelta(minutes=50), 76000)  # -5.00%
    assert trig and trig["high_px"] == 80000 and trig["drop_pct"] == pytest.approx(5.0)


def test_slow_drop_over_more_than_60min_does_not_fire(tmp_path):
    w = _w(tmp_path)
    assert _feed(w, [(0, 80000), (30, 78500), (61, 77500), (90, 76100), (125, 75500)]) is None


def _live(w, trig, now, cash=50.0, fill=(0.00013, 76000.0, 0.06, "oid-1")):
    calls = []

    def buy(usd):
        calls.append(usd)
        return fill

    res = D.execute_dip_buy(w, trig, now=now, live=True, free_usd=lambda: cash, buy=buy)
    return res, calls


def test_once_per_instance_then_rearm_on_recovery(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    res, calls = _live(w, trig, T0 + timedelta(minutes=30))
    assert res.status == "bought" and calls == [10.0] and res.detail["order_id"] == "oid-1"
    # keeps falling inside the same instance: no second buy
    assert _feed(w, [(31, 75000), (35, 72000), (40, 71000)]) is None
    # recovers above the drop start (80,000) -> re-armed; a NEW 5% drop from post-buy highs fires
    assert w.observe(T0 + timedelta(minutes=45), 80100) is None
    assert w.armed
    trig2 = w.observe(T0 + timedelta(minutes=50), 76000)
    assert trig2 and trig2["high_px"] == 80100


def test_rearm_after_new_window(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    _live(w, trig, T0 + timedelta(minutes=30))
    assert _feed(w, [(60, 75500), (89, 75000)]) is None and not w.armed
    assert w.observe(T0 + timedelta(minutes=91), 74000) is None  # new window: re-armed, but only -2.7% from post-buy high
    assert w.armed
    assert w.observe(T0 + timedelta(minutes=100), 71200)  # -5.1% from 75,000 high after the buy


def test_state_survives_restart_no_double_buy(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    _live(w, trig, T0 + timedelta(minutes=30))
    w2 = _w(tmp_path)  # container restart
    assert not w2.armed
    assert _feed(w2, [(31, 80000), (32, 75000)]) is None


def test_skips_when_spot_usd_below_buy(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    res, calls = _live(w, trig, T0 + timedelta(minutes=30), cash=9.99)
    assert res.status == "skipped_insufficient_cash" and calls == []
    assert _contribs(tmp_path / "treasury.db") == []


def test_records_dip_buy_contribution(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    res, _ = _live(w, trig, T0 + timedelta(minutes=30), fill=(0.00013, 76000.0, 0.06, "abc-123"))
    rows = _contribs(tmp_path / "treasury.db")
    assert len(rows) == 1 and rows[0]["kind"] == "dip_buy"
    assert rows[0]["btc_qty"] == pytest.approx(0.00013)
    assert "abc-123" in rows[0]["note"] and "CT" in rows[0]["note"]
    assert res.detail["time_ct"].endswith("CT") and res.detail["price"] == 76000.0


def test_paper_mode_places_no_order(tmp_path):
    w = _w(tmp_path)
    trig = _feed(w, [(0, 80000), (30, 75900)])
    called = []
    res = D.execute_dip_buy(w, trig, now=T0, live=False, free_usd=lambda: 100.0, buy=lambda u: called.append(u))
    assert res.status == "paper" and called == []


class Broker:
    def __init__(self, maker_fill=True):
        self.maker_fill = maker_fill
        self.calls = []

    def fetch_bba(self, product):
        return 76000.0, 76001.0

    def create_maker_limit_order(self, product, side, amount, **kw):
        self.calls.append(("maker", product, side, amount, kw.get("post_only")))
        if not self.maker_fill:
            return {"id": "mk-1", "filled": 0.0, "average": None}
        return {"id": "mk-1", "filled": amount, "average": 76000.0, "fee": {"cost": 0.06, "currency": "USD"}}

    def create_market_order(self, product, side, amount, *, cost=None, price=None):
        self.calls.append(("market", product, side, cost))
        return {"id": "mk-2", "filled": cost / 76010.0, "average": 76010.0, "fee": {"cost": 0.12, "currency": "USD"}}

    def fetch_free_usd(self):
        return 40.0


def test_broker_buy_prefers_maker():
    b = Broker(maker_fill=True)
    qty, px, fee, oid = D.broker_buy(b, 10.0)
    assert [c[0] for c in b.calls] == ["maker"] and b.calls[0][4] is True
    assert oid == "mk-1" and qty * px + fee == pytest.approx(10.0, abs=0.01)


def test_broker_buy_market_fallback():
    b = Broker(maker_fill=False)
    qty, px, fee, oid = D.broker_buy(b, 10.0)
    assert [c[0] for c in b.calls] == ["maker", "market"] and b.calls[1][3] == 10.0
    assert oid == "mk-2" and px == 76010.0


def test_engine_tick_exempt_from_spot_floor(tmp_path):
    from snowball.engine import Engine

    s = Settings(_env_file=None, mode="live", live_enabled=True, trading_enabled=True, spot_usd_floor=300.0,
                 treasury_sqlite_path=tmp_path / "treasury.db", halt_file=tmp_path / "HALT")
    if not s.live_orders_permitted():
        pytest.skip("live orders gated by other settings in this build")
    b = Broker(maker_fill=True)
    eng = Engine.__new__(Engine)
    eng.state = SimpleNamespace(settings=s, broker=b)
    eng.market = None
    eng._dip_buy_tick(T0, {"BTC-USD": 80000.0})
    eng._dip_buy_tick(T0 + timedelta(minutes=20), {"BTC-USD": 75900.0})
    assert eng.state.treasury_dip_buy_last["status"] == "bought"  # $40 free < $300 floor, still buys
    assert [c[0] for c in b.calls] == ["maker"]


def test_engine_tick_disabled_flag(tmp_path):
    from snowball.engine import Engine

    s = Settings(_env_file=None, treasury_dip_buy_enabled=False, treasury_sqlite_path=tmp_path / "t.db")
    eng = Engine.__new__(Engine)
    eng.state = SimpleNamespace(settings=s, broker=Broker())
    eng.market = None
    eng._dip_buy_tick(T0, {"BTC-USD": 80000.0})
    assert not (tmp_path / "t.db").exists()
