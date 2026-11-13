"""SPOT_USD_FLOOR must hold against futures margin sweeps, in-flight buys and orphaned makers.

Regression for 2026-10-14: spot USD fell from $981.94 to $128.16 ($171.84 under the $300 floor)
while every spot buy passed the floor check. The cash went to (a) a TEK long whose CFM margin
Coinbase took from spot USD ($385.70 derivatives_settlement at 5:30 PM CT) and (b) an orphaned
GTC MOG maker buy ($120.98) that filled 14 hours after the restart that orphaned it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from snowball import engine as eng
from snowball.config import Settings
from snowball.futures.stops import margin_gate_eval
from snowball.live import LiveBroker, maker_gtd_end_time
from snowball.models import Ticker

TEK = "TEK-19DEC30-CDE"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _liq(buffer_usd: float, cbi: float | None, contracts: float = 1.0) -> dict:
    return {"available": True, "buffer_usd": buffer_usd, "exchange_contracts": contracts,
            "cbi_usd_balance": cbi}


# ---- (1) futures margin gate reserves the spot floor --------------------------------------

def test_margin_gate_unchanged_when_floor_off():
    st = SimpleNamespace(settings=_settings(spot_usd_floor=0.0))
    ev = margin_gate_eval(st, TEK, "long", price=4083.1, liq=_liq(1400.0, cbi=552.0))
    assert ev["ok"], ev
    assert ev["spot_floor_reserve_usd"] == 0.0


def test_margin_gate_reserves_floor_out_of_cfm_buffer():
    """10/14 7:52 AM CT shape: a buffer that passes without the reserve must fail with it."""
    probe = margin_gate_eval(SimpleNamespace(settings=_settings(spot_usd_floor=0.0)), TEK, "long",
                             price=4083.1, liq=_liq(100000.0, cbi=552.0))
    need, m_new = probe["required_usd_per_contract"], probe["new_contract_margin_usd"]
    buf = m_new + 2.0 * (need + 50.0)  # 1 contract open -> headroom after = need + $50 (passes)
    off = margin_gate_eval(SimpleNamespace(settings=_settings(spot_usd_floor=0.0)), TEK, "long",
                           price=4083.1, liq=_liq(buf, cbi=552.0))
    on = margin_gate_eval(SimpleNamespace(settings=_settings(spot_usd_floor=300.0)), TEK, "long",
                          price=4083.1, liq=_liq(buf, cbi=552.0))
    assert on["spot_floor_reserve_usd"] == 300.0
    assert on["headroom_after_usd_per_contract"] == pytest.approx(
        off["headroom_after_usd_per_contract"] - 300.0 / 2.0, abs=0.01)
    assert off["ok"] and not on["ok"], (off, on)


def test_margin_gate_blocks_new_futures_entry_while_spot_under_floor():
    st = SimpleNamespace(settings=_settings(spot_usd_floor=300.0))
    ev = margin_gate_eval(st, TEK, "long", price=4083.1, liq=_liq(5000.0, cbi=128.16, contracts=0))
    assert not ev["ok"]
    assert ev["why"] == "margin_gate_spot_floor_128_lt_300"


# ---- (2) spot floor counts in-flight buys ---------------------------------------------------

def _engine(floor: float = 300.0):
    e = eng.Engine.__new__(eng.Engine)
    e.state = SimpleNamespace(settings=_settings(spot_usd_floor=floor), spot_floor_stats={})
    return e


def test_floor_counts_inflight_reservations():
    e = _engine()
    eng._SPOT_INFLIGHT.clear()
    assert e._spot_floor_allows("BTC-USD", 100.0, 420.0, "t", source="exchange")  # 420-100.6 >= 300
    eng._SPOT_INFLIGHT["WLD-USD:1"] = 100.6  # another buy placed, not yet reflected in free USD
    try:
        assert not e._spot_floor_allows("BTC-USD", 100.0, 420.0, "t", source="exchange")
        assert e.state.spot_floor_stats["last_blocked"]["pending_usd"] == 100.6
    finally:
        eng._SPOT_INFLIGHT.clear()


def test_concurrent_buys_cannot_jointly_breach_floor():
    """Two threads see the same stale free USD; at most one may pass once the other reserves."""
    e = _engine()
    eng._SPOT_INFLIGHT.clear()
    passed: list[str] = []
    gate = eng._SPOT_INFLIGHT_LOCK  # same lock _open_lot_live holds for check + reserve

    def buy(p: str) -> None:
        with gate:
            if e._spot_floor_allows(p, 100.0, 420.0, "t", source="exchange"):
                eng._SPOT_INFLIGHT[p] = 100.6
                passed.append(p)

    ts = [threading.Thread(target=buy, args=(f"P{i}-USD",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    eng._SPOT_INFLIGHT.clear()
    assert len(passed) == 1


def test_inflight_reservation_released_after_order():
    e = _engine()
    eng._SPOT_INFLIGHT.clear()
    seen: list[float] = []

    class Broker:
        def fetch_free_usd(self):
            return 900.0

        def create_maker_limit_order(self, *a, **k):
            seen.append(eng.spot_inflight_usd())
            return {"status": "canceled", "filled": 0}

    ledger = SimpleNamespace(set_cash_usd=lambda *a, **k: None, cash_usd=lambda: 900.0)
    e.state = SimpleNamespace(settings=_settings(spot_usd_floor=300.0), spot_floor_stats={},
                              broker=Broker(), ledger=ledger)
    ticker = Ticker(product="BTC-USD", last=100.0, bid=99.9, ask=100.1, ts=datetime.now(timezone.utc))
    e._open_lot_live(product="BTC-USD", ticker=ticker, reason="t", now=datetime.now(timezone.utc),
                     strategy="bb_15m", notional_usd=100.0)
    assert seen and seen[0] == pytest.approx(100.6)
    assert eng.spot_inflight_usd() == 0.0


# ---- (3) maker orders expire on the exchange (no orphaned GTC buys) -------------------------

def test_maker_gtd_end_time_is_wait_plus_grace():
    now = datetime(2026, 10, 14, 12, 35, 33, tzinfo=timezone.utc)
    assert maker_gtd_end_time(90.0, now) == "2026-10-14T12:38:03.000Z"
    assert maker_gtd_end_time(5.0, now) == "2026-10-14T12:37:33.000Z"  # 2-minute minimum


def test_maker_limit_order_uses_gtd_with_end_time():
    calls: list[tuple] = []

    class Ex:
        def create_order(self, symbol, typ, side, amount, price, params):
            calls.append((symbol, typ, side, amount, price, dict(params)))
            return {"id": "x", "status": "closed", "filled": amount, "average": price}

    b = LiveBroker.__new__(LiveBroker)
    b._exchange = Ex()
    b.create_maker_limit_order("MOG-USD", "buy", 1000.0, price=1e-7, bid=1e-7, ask=2e-7,
                               timeout_sec=90.0)
    params = calls[0][5]
    assert params["timeInForce"] == "GTD"
    assert params["postOnly"] is True
    assert params["end_time"].endswith("Z")
