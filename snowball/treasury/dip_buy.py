"""Treasury dip buy (Tb 2026-10-30 4:46 PM CT; image live-20261030a).

Rule: when BTC-USD falls >= TREASURY_DIP_BUY_PCT (5%) from its high within any rolling
TREASURY_DIP_BUY_WINDOW_MIN (60) minutes, buy TREASURY_DIP_BUY_USD ($10) of BTC into the
treasury (kind ``dip_buy``). Maker-preferred (post-only limit at the bid, short timeout), market
fallback. Once per instance: after a buy it does not fire again until the price recovers above
the drop's start (the window high) OR a new 60-minute window has passed since the buy; the new
drop must then be measured from highs AFTER the buy. Exempt from SPOT_USD_FLOOR exactly like the
dividend (it buys through the broker directly, not the trading-lane entry path); skipped when
free spot USD < the buy amount. Receive-only (never sells). Every buy is logged as "dip buy"
with CT time, price and order id. One-flag rollback: TREASURY_DIP_BUY_ENABLED=false.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("snowball.treasury.dip_buy")

CT = ZoneInfo("America/Chicago")
KIND = "dip_buy"
STATE_KEY = "dip_buy_state"
PRODUCT = "BTC-USD"
MAKER_FEE_RESERVE = 0.006  # size the maker leg so qty*px + fee stays ~= the buy amount


@dataclass
class DipBuyResult:
    status: str  # idle | bought | skipped_insufficient_cash | disarmed | error | paper
    detail: dict[str, Any] = field(default_factory=dict)


def _meta_get(db: Path, key: str) -> dict | None:
    from snowball.treasury.ledger import ensure_schema

    ensure_schema(db)
    con = sqlite3.connect(str(db))
    try:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    try:
        v = json.loads(r[0])
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


def _meta_set(db: Path, key: str, value: dict) -> None:
    from snowball.treasury.ledger import ensure_schema

    ensure_schema(db)
    con = sqlite3.connect(str(db))
    try:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value, default=str)))
        con.commit()
    finally:
        con.close()


class DipBuyWatcher:
    """Feed it BTC-USD prices; it fires at most once per drop instance."""

    def __init__(self, db: Path, *, pct: float = 0.05, window_min: float = 60.0, usd: float = 10.0) -> None:
        self.db = Path(db)
        self.pct = float(pct)
        self.window = timedelta(minutes=float(window_min))
        self.usd = float(usd)
        self._px: deque[tuple[datetime, float]] = deque()
        self._lock = threading.RLock()
        st = _meta_get(self.db, STATE_KEY) or {}
        self.armed: bool = bool(st.get("armed", True))
        self.fired_at: datetime | None = _dt(st.get("fired_at"))
        self.drop_start_px: float | None = _f(st.get("drop_start_px"))

    # ------------------------------------------------------------------ state
    def _save(self) -> None:
        _meta_set(self.db, STATE_KEY, {"armed": self.armed,
                                       "fired_at": self.fired_at.isoformat() if self.fired_at else None,
                                       "drop_start_px": self.drop_start_px})

    def _rearm_check(self, now: datetime, px: float) -> None:
        if self.armed:
            return
        why = None
        if self.drop_start_px is not None and px > self.drop_start_px:
            why = "recovered_above_drop_start"
        elif self.fired_at is not None and now - self.fired_at >= self.window:
            why = "new_window"
        elif self.fired_at is None:
            why = "no_fire_record"
        if why:
            self.armed = True
            self._save()
            log.info("treasury dip buy re-armed", extra={"data": {"why": why, "px": px,
                                                                 "drop_start_px": self.drop_start_px}})

    def observe(self, now: datetime, px: float) -> dict | None:
        """Record a price. Returns the trigger dict when a NEW drop instance fires, else None."""
        if px is None or float(px) <= 0:
            return None
        px = float(px)
        with self._lock:
            self._px.append((now, px))
            cutoff = now - self.window
            while self._px and self._px[0][0] < cutoff:
                self._px.popleft()
            self._rearm_check(now, px)
            floor_ts = self.fired_at if self.fired_at is not None else None
            pts = [(t, p) for t, p in self._px if floor_ts is None or t >= floor_ts]
            if not pts:
                return None
            hi_t, hi = max(pts, key=lambda tp: tp[1])
            drop = (hi - px) / hi if hi > 0 else 0.0
            if drop + 1e-12 < self.pct or not self.armed:
                return None
            return {"high_px": hi, "high_at": hi_t.isoformat(), "px": px, "drop_pct": round(100.0 * drop, 3),
                    "window_min": self.window.total_seconds() / 60.0}

    def mark_fired(self, now: datetime, trig: dict) -> None:
        with self._lock:
            self.armed = False
            self.fired_at = now
            self.drop_start_px = float(trig["high_px"])
            # The next instance must be measured from prices after this buy.
            self._px = deque([(t, p) for t, p in self._px if t >= now])
            self._save()


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def execute_dip_buy(
    watcher: DipBuyWatcher,
    trig: dict,
    *,
    now: datetime,
    live: bool,
    free_usd: Callable[[], float],
    buy: Callable[[float], tuple[float, float, float, str]],
) -> DipBuyResult:
    """Fire one dip buy for trigger *trig* (claim first so a crash can never double-buy)."""
    usd = float(watcher.usd)
    ct = now.astimezone(CT)
    detail = {**trig, "usd": usd, "time_ct": ct.strftime("%Y-%m-%d %I:%M:%S %p CT")}
    if not live:
        watcher.mark_fired(now, trig)
        log.warning("dip buy (paper: no order)", extra={"data": detail})
        return DipBuyResult("paper", detail)
    try:
        cash = float(free_usd())
    except Exception as exc:  # noqa: BLE001
        log.exception("dip buy skipped: free USD read failed")
        return DipBuyResult("error", {**detail, "error": str(exc)[:200]})
    if cash + 1e-9 < usd:
        # Skip this instance (do not retry every tick while cash is short).
        watcher.mark_fired(now, trig)
        log.warning("dip buy skipped: spot USD below buy amount", extra={"data": {**detail, "free_usd": round(cash, 2)}})
        return DipBuyResult("skipped_insufficient_cash", {**detail, "free_usd": round(cash, 2)})
    watcher.mark_fired(now, trig)  # claim before the order (never double-buys on a crash/restart)
    try:
        qty, px, fee, oid = buy(usd)
    except Exception as exc:  # noqa: BLE001
        log.error("dip buy order error (instance consumed; check Coinbase)",
                  extra={"data": {**detail, "error": str(exc)[:300]}})
        return DipBuyResult("error", {**detail, "error": str(exc)[:300]})
    if qty <= 0 or px <= 0:
        log.error("dip buy order returned no fill (instance consumed; check Coinbase)",
                  extra={"data": {**detail, "order_id": oid}})
        return DipBuyResult("error", {**detail, "order_id": oid})
    from snowball.treasury.ledger import record_contribution

    spent = qty * px + fee
    note = (f"dip buy: BTC -{trig['drop_pct']}% from {trig['high_px']:.2f} within {trig['window_min']:g} min; "
            f"order {oid}; fee {fee:.6f}; {detail['time_ct']}")
    record_contribution(watcher.db, usd_amount=round(spent, 8), btc_qty=qty, btc_price=px, kind=KIND, ts=now, note=note)
    out = {**detail, "btc_qty": qty, "price": px, "fee_usd": fee, "order_id": oid, "usd_spent": round(spent, 6)}
    log.warning("dip buy", extra={"data": out})
    return DipBuyResult("bought", out)


def broker_buy(broker: Any, usd: float, *, maker_timeout_sec: float = 20.0) -> tuple[float, float, float, str]:
    """Maker-preferred $usd BTC buy: post-only limit at the bid, then market for any remainder."""
    from snowball.live import parse_order_fill

    qty_t = px_t = fee_t = 0.0
    oids: list[str] = []
    try:
        bid, ask = broker.fetch_bba(PRODUCT)
        if bid is not None and float(bid) > 0:
            o = broker.create_maker_limit_order(PRODUCT, "buy", round(float(usd) * (1.0 - MAKER_FEE_RESERVE) / float(bid), 8), bid=bid, ask=ask,
                                                timeout_sec=maker_timeout_sec, post_only=True)
            p, q, f = parse_order_fill(o)
            if q > 0 and p > 0:
                qty_t, px_t, fee_t = q, p, f
                oids.append(str(o.get("id") or ""))
    except Exception:  # noqa: BLE001
        log.exception("dip buy maker leg failed; using market")
    rem = float(usd) - (qty_t * px_t + fee_t)
    if rem >= 1.0:
        o = broker.create_market_order(PRODUCT, "buy", 0.0, cost=round(rem, 2))
        p, q, f = parse_order_fill(o)
        if q > 0 and p > 0:
            px_t = (px_t * qty_t + p * q) / (qty_t + q)
            qty_t += q
            fee_t += f
            oids.append(str(o.get("id") or ""))
    return qty_t, px_t, fee_t, ",".join(x for x in oids if x)
