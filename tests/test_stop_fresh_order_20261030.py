"""Regression for TEK #21 (2026-10-30, image live-20261030a).

10/25 6:51 PM CT the FT stop for TEK #21 (1 long, basis 4,083.1, stop 3,833.3) sent MARKET IOC
orders after hours; Coinbase rejected all 3 (PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE) and the
record stayed status=error, attempts=3, order_id=null. From 10/30 8:02 AM CT every trigger reused
that record -> "futures stop gave up after 3 failed attempts", no fresh order, and the liquidation
guard (same record, evaluated only when the stop was NOT hit) never fired.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from snowball.config import Settings
from snowball.futures import stops as S

TEK = "TEK-19DEC30-CDE"
AFTER_HOURS = ('coinbase {"success":false,"error_response":{"error":"UNKNOWN_FAILURE_REASON",'
               '"preview_failure_reason":"PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE"}}')
NOW = datetime(2026, 10, 30, 13, 2, tzinfo=timezone.utc)  # 8:02 AM CT


@dataclass
class Lot:
    id: int = 21
    product: str = TEK
    side: str = "long"
    qty: float = 1.0
    entry_price: float = 4083.1
    gate_entry_price: float = 4083.1
    strategy: str = "bb_15m"
    opened_at: datetime = datetime(2026, 10, 14, 12, 52, tzinfo=timezone.utc)


class Ledger:
    def __init__(self, lots):
        self.lots = list(lots)

    def open_positions(self, product=None):
        return [l for l in self.lots if product is None or l.product == product]


class Exchange:
    """Fake ccxt Coinbase: CFM positions + balance summary (READ endpoints)."""

    def __init__(self, contracts=1.0, buffer=400.0):
        self.contracts = contracts
        self.buffer = buffer

    def v3PrivateGetBrokerageCfmPositions(self):
        if self.contracts <= 0:
            return {"positions": []}
        return {"positions": [{"product_id": TEK, "side": "LONG", "number_of_contracts": str(self.contracts)}]}

    def v3PrivateGetBrokerageCfmBalanceSummary(self):
        return {"balance_summary": {"available_margin": {"value": str(self.buffer + 100)},
                                    "liquidation_buffer_amount": {"value": str(self.buffer)},
                                    "overnight_margin_window_measure": {"maintenance_margin": {"value": "100"}},
                                    "cbi_usd_balance": {"value": "60"}}}


class Market:
    def __init__(self, ex, *, market_error=None, limit_fill=True, bid=3825.0, ask=3826.0):
        self.exchange = ex
        self.market_error = market_error
        self.limit_fill = limit_fill
        self.bid, self.ask = bid, ask
        self.calls: list[tuple] = []
        self.orders: dict[str, dict] = {}

    def fetch_bba(self, product):
        return self.bid, self.ask

    def create_swap_market_order(self, product, side, amount, *, leverage=1.0, reduce_only=False):
        self.calls.append(("market", product, side, amount))
        if self.market_error:
            raise RuntimeError(self.market_error)
        self.exchange.contracts -= amount
        return {"id": "mkt-1", "status": "closed", "filled": amount, "remaining": 0.0, "average": self.bid,
                "fee": {"cost": 0.11, "currency": "USD"}}

    def create_swap_limit_close_order(self, product, side, amount, *, price, leverage=1.0):
        oid = f"lim-{len(self.calls) + 1}"
        self.calls.append(("limit", product, side, amount, round(price, 4)))
        if self.limit_fill:
            self.exchange.contracts -= amount
            o = {"id": oid, "status": "closed", "filled": amount, "remaining": 0.0, "average": self.bid,
                 "fee": {"cost": 0.11, "currency": "USD"}}
        else:
            o = {"id": oid, "status": "open", "filled": 0.0, "remaining": amount, "average": None}
        self.orders[oid] = dict(o)
        return o

    def fetch_order_safe(self, order_id, product):
        return self.orders.get(order_id)


def _state(tmp_path, lots, **kw):
    s = Settings(_env_file=None, futures_netted_pairs="", futures_aged_exit_enabled=False, **kw)
    st = SimpleNamespace(settings=s, stop_store=S.StopStore(tmp_path / "stops.db"),
                         futures_ledger=Ledger(lots))
    return st


def _hooks(st, market, booked):
    def book(lot, px, fee, reason, ts):
        booked.append((lot.id, px, fee, reason))
        st.futures_ledger.lots = [l for l in st.futures_ledger.lots if l.id != lot.id]
        return (px - lot.entry_price) * lot.qty - fee

    pairs = {TEK: SimpleNamespace(bid=market.bid, ask=market.ask, last=market.bid)}
    return S.LaneHooks(lane="futures", ledger=st.futures_ledger, market=market, pairs=pairs, live=True, book=book)


def _seed_failed_1025(st, attempts=3, updated=None):
    st.stop_store.upsert_order("futures", 21, product=TEK, side="long", contracts=1, reason="stop_loss:bb_15m",
                               status="error", pre_exchange_contracts=1.0, attempts=attempts, note=AFTER_HOURS)
    ts = (updated or datetime(2026, 10, 25, 23, 55, 1, tzinfo=timezone.utc)).isoformat()
    with S._LOCK:
        st.stop_store._conn.execute("UPDATE stop_orders SET created_at=?, updated_at=? WHERE lane='futures'", (ts, ts))
        st.stop_store._conn.commit()


@pytest.fixture(autouse=True)
def _clear_caches():
    S._EX_CACHE.clear()
    S._LOG_AT.clear()
    yield
    S._EX_CACHE.clear()


def test_tek21_stop_price_matches_record():
    s = Settings(_env_file=None)
    assert S.stop_price("long", 4083.1, TEK, s) == pytest.approx(3833.34, abs=0.01)


def test_stale_failed_record_expires_and_fresh_order_is_built(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    _seed_failed_1025(st)
    m = Market(Exchange(), bid=3820.0, ask=3821.0)
    booked: list = []
    views = S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert views and views[0]["id"] == 21
    assert [c[0] for c in m.calls] == ["market"], m.calls  # fresh order, market accepted (core session)
    assert booked == [(21, 3820.0, 0.11, "stop_loss:bb_15m")]
    row = st.stop_store.order_row("futures", 21)
    assert row["status"] == "booked" and row["order_id"] == "mkt-1" and row["attempts"] == 1


def test_recent_failures_still_back_off_until_ttl(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    _seed_failed_1025(st, updated=NOW - timedelta(minutes=2))
    m = Market(Exchange(buffer=400.0))
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert m.calls == [] and booked == []
    # ... and once the TTL (900 s default) passes, the same trigger builds a fresh order.
    S.run_stops(st, _hooks(st, m, booked), now=NOW + timedelta(minutes=14), can_trade=True)
    assert len(m.calls) == 1 and len(booked) == 1


def test_after_hours_market_rejection_falls_back_to_limit(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    m = Market(Exchange(), market_error=AFTER_HOURS, limit_fill=True, bid=3800.0, ask=3801.0)
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert [c[0] for c in m.calls] == ["market", "limit"]
    assert m.calls[1][4] == pytest.approx(3800.0 * (1 - 0.005), abs=1e-6)  # bid - 0.5% collar
    assert booked and booked[0][0] == 21
    assert st.stop_store.order_row("futures", 21)["status"] == "booked"


def test_working_limit_is_monitored_then_booked_without_resend(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    m = Market(Exchange(), market_error=AFTER_HOURS, limit_fill=False, bid=3800.0, ask=3801.0)
    booked: list = []
    h = _hooks(st, m, booked)
    S.run_stops(st, h, now=NOW, can_trade=True)
    row = st.stop_store.order_row("futures", 21)
    assert row["status"] == "sent" and row["order_id"].startswith("lim-")
    n_calls = len(m.calls)
    S._LOG_AT.clear()
    S.run_stops(st, h, now=NOW + timedelta(minutes=1), can_trade=True)
    assert len(m.calls) == n_calls and booked == []  # still working: no second order
    oid = row["order_id"]
    m.orders[oid].update({"status": "closed", "filled": 1.0, "remaining": 0.0, "average": 3799.0,
                          "fee": {"cost": 0.11, "currency": "USD"}})
    m.exchange.contracts = 0.0
    S._LOG_AT.clear()
    S.run_stops(st, h, now=NOW + timedelta(minutes=2), can_trade=True)
    assert booked == [(21, 3799.0, 0.11, "stop_loss:bb_15m")]
    assert len(m.calls) == n_calls


def test_working_limit_that_ends_unfilled_frees_the_record(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    m = Market(Exchange(), market_error=AFTER_HOURS, limit_fill=False)
    booked: list = []
    h = _hooks(st, m, booked)
    S.run_stops(st, h, now=NOW, can_trade=True)
    oid = st.stop_store.order_row("futures", 21)["order_id"]
    m.orders[oid]["status"] = "canceled"
    S._LOG_AT.clear()
    assert S.poll_working_close(st, h, lot, {"basis": 4083.1, "stop_px": 3833.3, "touch_px": 3825.0},
                                now=NOW + timedelta(minutes=1)) == "ended"
    row = st.stop_store.order_row("futures", 21)
    assert row["status"] == "error" and row["order_id"] is None


def test_liq_guard_fires_independently_when_stop_record_is_stuck(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    _seed_failed_1025(st, updated=NOW - timedelta(minutes=1))  # stop record stuck (within TTL)
    m = Market(Exchange(buffer=40.0))  # headroom $40/contract <= $50 guard
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert len(m.calls) == 1, m.calls
    assert booked and booked[0][3] == "stop_loss:liq_guard:bb_15m"
    assert st.stop_store.order_row("futures#liq", 21)["status"] == "booked"
    assert st.stop_store.order_row("futures", 21)["status"] == "error"  # untouched


def test_liq_guard_fires_without_stop_hit(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    m = Market(Exchange(buffer=45.0), bid=3990.0, ask=3991.0)  # above the 3,833.3 stop
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert booked and booked[0][3].startswith("stop_loss:liq_guard")


def test_one_closing_order_per_lot_across_kinds(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    st.stop_store.upsert_order("futures#liq", 21, status="sent", order_id="x-1", reason="stop_loss:liq_guard:bb_15m")
    m = Market(Exchange(buffer=400.0))
    m.orders["x-1"] = {"id": "x-1", "status": "open", "filled": 0.0, "remaining": 1.0}
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert m.calls == [] and booked == []
    assert S.close_pending(st, "futures", 21)


def test_errored_send_that_executed_is_not_resent_after_expiry(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot])
    st.stop_store.upsert_order("futures", 21, product=TEK, side="long", contracts=1, reason="stop_loss:bb_15m",
                               status="error", pre_exchange_contracts=2.0, attempts=1, note="timeout")
    old = (NOW - timedelta(hours=2)).isoformat()
    with S._LOCK:
        st.stop_store._conn.execute("UPDATE stop_orders SET updated_at=?", (old,))
        st.stop_store._conn.commit()
    m = Market(Exchange(contracts=1.0))  # exchange dropped 2 -> 1 after the errored send
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert m.calls == []
    assert st.stop_store.order_row("futures", 21)["status"] == "sent_unconfirmed"


def test_market_only_mode_keeps_legacy_behaviour(tmp_path):
    lot = Lot()
    st = _state(tmp_path, [lot], futures_stop_order_type="market")
    m = Market(Exchange(), market_error=AFTER_HOURS)
    booked: list = []
    S.run_stops(st, _hooks(st, m, booked), now=NOW, can_trade=True)
    assert [c[0] for c in m.calls] == ["market"] and booked == []
    assert st.stop_store.order_row("futures", 21)["status"] == "error"


def test_close_limit_price_directions():
    assert S.close_limit_price("sell", 100.0, 101.0, None, 0.01) == pytest.approx(99.0)
    assert S.close_limit_price("buy", 100.0, 101.0, None, 0.01) == pytest.approx(102.01)
    assert S.close_limit_price("sell", None, None, 50.0, 0.0) == 50.0
    assert S.market_rejected_for_session(RuntimeError(AFTER_HOURS))
    assert not S.market_rejected_for_session(RuntimeError("insufficient funds"))
