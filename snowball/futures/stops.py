"""Futures stop-losses, liquidation guard, netted pairs, one-index gate (Tb 2026-10-01).

Tb, 6:27 AM CT 10/1 (Scout's chat): "Put stop losses on all futures positions, longs and
any shorts." A deliberate, FUTURES-ONLY exception to never-sell-red / never-cover-red.
Spot crypto is untouched (the stop reason is NOT in gates.EMERGENCY_FLATTEN_REASONS).

* Stop distance per contract = the CLOSER of
    - FUTURES_STOP_PCT (8%) x roll-adjusted basis x contract size  (US5/TEK only), and
    - FUTURES_STOP_USD_PER_CONTRACT ($250) loss NET of real round-trip fees ($0.12/side).
  Shorts mirror (stop above basis). Trigger at the worst touch (long: bid, short: ask).
* Liquidation guard: real Coinbase CFM balance summary. Buffer = min(current-window
  liquidation buffer, available margin - overnight maintenance). Headroom per contract =
  buffer / open exchange contracts; a lot is stopped when headroom <= FUTURES_LIQ_GUARD_USD.
* Bot-side only (no resting exchange stops). Market close. CFM has no reduce-only, so a live
  stop (and every live closing order) first verifies the exchange position for that
  product/side holds at least the contracts being closed (refuses any flip/open), and a lot
  gets at most ONE stop order (persisted in snowball_stops.db; a restart cannot double-send).
* Re-entry: a stopped family is blocked for the rest of the ET day (persisted).
* Netted pairs (FUTURES_NETTED_PAIRS, e.g. FT TEK #13 long vs Fed TEK #2 short; Coinbase
  nets TEK to 0): bookkeeping-only mark. Excluded from stops, exits, margin math and the
  one-index gate. No realized P/L booked, no rows deleted, no orders.
* One-index gate (FUTURES_ONE_INDEX_CONTRACT): no new US5/TEK entry in any lane while any
  non-netted index contract is open in any lane. Rolls are not entries.
* Aged-lot exit (Tb 9:38 AM CT 10/2, FUTURES_AGED_EXIT_ENABLED): a non-netted lot older than
  FUTURES_AGED_LOT_DAYS (3) from its ORIGINAL entry (through contract rolls) gets a lowered
  take-profit: it closes as soon as its net P&L vs the roll-adjusted basis after real round-trip
  fees is >= FUTURES_AGED_LOT_MIN_NET_USD ($0.50) at the worst touch (re-checked on a fresh book
  right before sending). Never closes red because of age; stops stay the only red exits. Same
  reconcile + exchange-position check, same one-closing-order-per-lot store (own row
  "<lane>#aged", cross-checked against the stop row), market close, no re-entry block.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("snowball.futures.stops")

ET = ZoneInfo("America/New_York")
STOP_REASON_BASE = "stop_loss"
LIQ_KIND = "liq_guard"
_LOCK = threading.RLock()


def is_stop_reason(reason: str) -> bool:
    return str(reason or "").split(":", 1)[0] == STOP_REASON_BASE


def family_root(product: str) -> str:
    return str(product or "").strip().upper().split("-", 1)[0]


def _cs(product: str) -> float:
    from snowball.futures.market import contract_size_for_product, is_cfm_product

    if not is_cfm_product(product):
        return 1.0
    v = float(contract_size_for_product(product))
    return v if v > 0 else 1.0


def lot_contracts(product: str, qty: float) -> int:
    from snowball.futures.market import contracts_from_qty, is_cfm_product

    if is_cfm_product(product):
        return int(contracts_from_qty(product, float(qty)))
    return max(1, int(round(float(qty))))


def _families(raw: str) -> set[str]:
    return {x.strip().upper() for x in str(raw or "").split(",") if x.strip()}


def round_trip_fee_per_contract(price: float, product: str, settings: Any) -> float:
    pct = float(getattr(settings, "futures_fee_pct_per_side", 0.0) or 0.0)
    per = float(getattr(settings, "futures_fee_per_contract", 0.12) or 0.0)
    return 2.0 * (per + abs(float(price)) * _cs(product) * pct)


def stop_distance_usd_per_contract(product: str, basis: float, settings: Any) -> tuple[float, str]:
    """Gross adverse price move ($/contract) that triggers the stop, and which leg binds."""
    cap_net = float(getattr(settings, "futures_stop_usd_per_contract", 250.0) or 0.0)
    cap_gross = max(0.0, cap_net - round_trip_fee_per_contract(basis, product, settings))
    dist, leg = cap_gross, "usd_cap"
    pct = float(getattr(settings, "futures_stop_pct", 0.08) or 0.0)
    if pct > 0 and family_root(product) in _families(getattr(settings, "futures_stop_pct_families", "US5,TEK")):
        d_pct = pct * float(basis) * _cs(product)
        if d_pct < dist:
            dist, leg = d_pct, "pct"
    return dist, leg


def stop_price(side: str, basis: float, product: str, settings: Any) -> float:
    dist, _ = stop_distance_usd_per_contract(product, basis, settings)
    px = dist / _cs(product)
    return float(basis) + px if str(side).lower() == "short" else float(basis) - px


def worst_touch(side: str, bid: float | None, ask: float | None, mark: float | None) -> float | None:
    px = ask if str(side).lower() == "short" else bid
    if px is None or float(px) <= 0:
        px = mark
    return None if px is None or float(px) <= 0 else float(px)


def stop_triggered(side: str, stop_px: float, bid: float | None, ask: float | None, mark: float | None) -> bool:
    px = worst_touch(side, bid, ask, mark)
    if px is None:
        return False
    return px >= stop_px if str(side).lower() == "short" else px <= stop_px


def next_et_midnight(now: datetime) -> datetime:
    et = now.astimezone(ET)
    nxt = (et + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- store


class StopStore:
    """snowball_stops.db: one-stop-order-per-lot, re-entry blocks, stop events, netted marks."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stop_orders (
                  lane TEXT NOT NULL, position_id INTEGER NOT NULL, product TEXT, side TEXT,
                  contracts INTEGER, reason TEXT, status TEXT, order_id TEXT,
                  pre_exchange_contracts REAL, attempts INTEGER DEFAULT 0,
                  fill_px REAL, fill_qty REAL, fee_usd REAL,
                  created_at TEXT, updated_at TEXT, note TEXT,
                  PRIMARY KEY (lane, position_id));
                CREATE TABLE IF NOT EXISTS reentry_blocks (
                  family TEXT PRIMARY KEY, until_utc TEXT, lane TEXT, position_id INTEGER,
                  reason TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS stop_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, lane TEXT, position_id INTEGER,
                  product TEXT, side TEXT, kind TEXT, reason TEXT, basis REAL, stop_px REAL,
                  trigger_px REAL, fill_px REAL, fee_usd REAL, realized_usd REAL, order_id TEXT,
                  note TEXT);
                CREATE TABLE IF NOT EXISTS netted_lots (
                  lane TEXT NOT NULL, position_id INTEGER NOT NULL, product TEXT, side TEXT,
                  qty REAL, entry_price REAL, pair_lane TEXT, pair_position_id INTEGER,
                  netted_pnl_usd REAL, marked_at TEXT, note TEXT,
                  PRIMARY KEY (lane, position_id));
                """
            )
            self._conn.commit()

    # stop orders
    def order_row(self, lane: str, pid: int) -> dict | None:
        with _LOCK:
            r = self._conn.execute("SELECT * FROM stop_orders WHERE lane=? AND position_id=?",
                                   (lane, int(pid))).fetchone()
        return dict(r) if r else None

    def upsert_order(self, lane: str, pid: int, **f: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _LOCK:
            cur = self.order_row(lane, pid)
            if cur is None:
                self._conn.execute(
                    "INSERT INTO stop_orders (lane, position_id, created_at, updated_at, attempts) VALUES (?,?,?,?,0)",
                    (lane, int(pid), now, now))
            sets = ", ".join(f"{k}=?" for k in f) + (", " if f else "") + "updated_at=?"
            self._conn.execute(f"UPDATE stop_orders SET {sets} WHERE lane=? AND position_id=?",
                               (*f.values(), now, lane, int(pid)))
            self._conn.commit()

    # re-entry
    def block_family(self, family: str, until: datetime, lane: str, pid: int, reason: str) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT OR REPLACE INTO reentry_blocks VALUES (?,?,?,?,?,?)",
                (family, until.isoformat(), lane, int(pid), reason, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()

    def family_blocked_until(self, family: str, now: datetime) -> datetime | None:
        with _LOCK:
            r = self._conn.execute("SELECT until_utc FROM reentry_blocks WHERE family=?", (family,)).fetchone()
        if not r:
            return None
        until = datetime.fromisoformat(r["until_utc"])
        return until if until > now else None

    def blocks(self, now: datetime) -> list[dict]:
        with _LOCK:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM reentry_blocks")]
        return [r for r in rows if datetime.fromisoformat(r["until_utc"]) > now]

    # events
    def event(self, **f: Any) -> None:
        f.setdefault("ts", datetime.now(timezone.utc).isoformat())
        cols = ", ".join(f)
        with _LOCK:
            self._conn.execute(f"INSERT INTO stop_events ({cols}) VALUES ({','.join('?' * len(f))})",
                               tuple(f.values()))
            self._conn.commit()

    def events(self, limit: int = 50) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM stop_events ORDER BY id DESC LIMIT ?", (int(limit),))]

    # netted
    def mark_netted(self, rows: list[dict]) -> None:
        with _LOCK:
            for r in rows:
                if self._conn.execute("SELECT 1 FROM netted_lots WHERE lane=? AND position_id=?",
                                      (r["lane"], int(r["position_id"]))).fetchone():
                    continue
                cols = ", ".join(r)
                self._conn.execute(f"INSERT INTO netted_lots ({cols}) VALUES ({','.join('?' * len(r))})",
                                   tuple(r.values()))
            self._conn.commit()

    def netted_rows(self) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in self._conn.execute("SELECT * FROM netted_lots ORDER BY lane, position_id")]


def get_store(state: Any) -> StopStore | None:
    st = getattr(state, "stop_store", None)
    if st is not None:
        return st
    path = getattr(state.settings, "futures_stops_sqlite_path", None)
    if not path:
        base = getattr(state.settings, "futures_sqlite_path", None)
        if not base:
            return None
        path = Path(base).parent / "snowball_stops.db"
    try:
        st = StopStore(path)
    except Exception:  # noqa: BLE001
        log.exception("stop store open failed")
        return None
    try:
        state.stop_store = st
    except Exception:  # noqa: BLE001
        pass
    return st


# --------------------------------------------------------------------------- lanes


def lane_ledgers(state: Any) -> dict[str, Any]:
    out = {}
    for lane, attr in (("futures", "futures_ledger"), ("crash", "crash_ledger"), ("fed", "fed_ledger"),
                       ("stocks", "stock_ledger")):
        led = getattr(state, attr, None)
        if led is not None:
            out[lane] = led
    return out


def _open_positions(led: Any) -> list:
    try:
        return list(led.open_positions())
    except TypeError:
        return []
    except Exception:  # noqa: BLE001
        log.exception("open_positions failed")
        return []


# --------------------------------------------------------------------------- netted pairs


def _open_fee(led: Any, pid: int) -> float | None:
    try:
        if hasattr(led, "open_fee_usd"):
            return led.open_fee_usd(pid)
        conn = getattr(led, "_conn", None)
        if conn is None:
            return None
        r = conn.execute("SELECT fee_usd FROM fills WHERE position_id=? ORDER BY id ASC LIMIT 1", (int(pid),)).fetchone()
        return None if r is None or r[0] is None else float(r[0])
    except Exception:  # noqa: BLE001
        return None



def parse_netted_pairs(raw: str) -> list[tuple[tuple[str, int], tuple[str, int]]]:
    out = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        a, b = part.split("=", 1)
        try:
            la, ia = a.strip().split(":"); lb, ib = b.strip().split(":")
            out.append(((la.strip().lower(), int(ia)), (lb.strip().lower(), int(ib))))
        except ValueError:
            log.error("bad FUTURES_NETTED_PAIRS entry", extra={"data": {"entry": part}})
    return out


def _lot_sort_key(lane: str, p: Any) -> tuple:
    return (str(getattr(p, "opened_at", "") or ""), lane, int(p.id))


def netted_keys(state: Any) -> set[tuple[str, int]]:
    """Validated netted legs: both open, same product, opposite sides, same qty.

    Sources, in order: FUTURES_NETTED_PAIRS (explicit), pairs already persisted in
    snowball_stops.db (stable across ticks/restarts), then (FUTURES_AUTO_NET_OVERLAPS, 2026-10-02)
    any remaining same-product long/short overlap across lanes, oldest long with oldest short.
    Coinbase nets per product, so each such pair is 0 on the exchange: bookkeeping-only, excluded
    from stops, exits, margin math and the one-index gate. No P/L booked, no rows deleted, no orders.
    """
    cache = getattr(state, "_netted_cache", None)
    now = time.monotonic()
    if cache and now - cache[0] < 30.0:
        return cache[1]
    keys: set[tuple[str, int]] = set()
    report: list[dict] = []
    leds = lane_ledgers(state)
    opens: dict[tuple[str, int], Any] = {}
    for lane, led in leds.items():
        for p in _open_positions(led):
            opens[(lane, int(p.id))] = p
    st = get_store(state)

    def _try(la: str, ia: int, lb: str, ib: int, source: str) -> bool:
        pa, pb = opens.get((la, ia)), opens.get((lb, ib))
        ok = (pa is not None and pb is not None and (la, ia) not in keys and (lb, ib) not in keys
              and pa.product == pb.product and pa.side != pb.side and abs(float(pa.qty) - float(pb.qty)) < 1e-9)
        if not ok:
            if source == "config":
                report.append({"pair": f"{la}:{ia}={lb}:{ib}", "valid": False, "source": source})
            return False
        keys.update({(la, ia), (lb, ib)})
        lng, sht = (pa, pb) if pa.side == "long" else (pb, pa)
        gross = (float(sht.entry_price) - float(lng.entry_price)) * float(lng.qty)
        fees = 0.0
        for lane_, p in ((la, pa), (lb, pb)):
            f = _open_fee(leds[lane_], p.id)
            fees += float(f) if f else 0.0
        report.append({"pair": f"{la}:{ia}={lb}:{ib}", "valid": True, "source": source, "product": pa.product,
                       "long": {"lane": la if lng is pa else lb, "id": lng.id, "entry": lng.entry_price},
                       "short": {"lane": la if sht is pa else lb, "id": sht.id, "entry": sht.entry_price},
                       "qty": float(lng.qty), "netted_pnl_gross_usd": round(gross, 2),
                       "entry_fees_usd": round(fees, 2), "netted_pnl_net_usd": round(gross - fees, 2),
                       "note": "bookkeeping-only: exchange nets these to 0; NOT booked as realized P/L"})
        if st is not None:
            ts = datetime.now(timezone.utc).isoformat()
            note = f"netted vs pair ({source}); no P/L booked"
            st.mark_netted([
                {"lane": la, "position_id": ia, "product": pa.product, "side": pa.side, "qty": pa.qty,
                 "entry_price": pa.entry_price, "pair_lane": lb, "pair_position_id": ib,
                 "netted_pnl_usd": round(gross - fees, 2), "marked_at": ts, "note": note},
                {"lane": lb, "position_id": ib, "product": pb.product, "side": pb.side, "qty": pb.qty,
                 "entry_price": pb.entry_price, "pair_lane": la, "pair_position_id": ia,
                 "netted_pnl_usd": round(gross - fees, 2), "marked_at": ts, "note": note},
            ])
        return True

    for (la, ia), (lb, ib) in parse_netted_pairs(getattr(state.settings, "futures_netted_pairs", "")):
        _try(la, ia, lb, ib, "config")
    if st is not None:
        try:
            for r in st.netted_rows():
                _try(str(r["lane"]), int(r["position_id"]), str(r["pair_lane"]), int(r["pair_position_id"]), "persisted")
        except Exception:  # noqa: BLE001
            log.exception("persisted netted pairs read failed")
    if bool(getattr(state.settings, "futures_auto_net_overlaps", True)):
        from snowball.futures.market import is_cfm_product

        by_prod: dict[str, dict[str, list]] = {}
        for (lane, pid), p in opens.items():
            if (lane, pid) in keys or not is_cfm_product(p.product):
                continue
            by_prod.setdefault(p.product, {"long": [], "short": []}).setdefault(
                "short" if str(p.side).lower() == "short" else "long", []).append((lane, p))
        for prod, sides in sorted(by_prod.items()):
            longs = sorted(sides["long"], key=lambda t: _lot_sort_key(*t))
            shorts = sorted(sides["short"], key=lambda t: _lot_sort_key(*t))
            for (la, pa) in longs:
                for k, (lb, pb) in enumerate(shorts):
                    if _try(la, int(pa.id), lb, int(pb.id), "auto_overlap"):
                        shorts.pop(k)
                        break
    try:
        state._netted_cache = (now, keys)
        state.netted_report = report
    except Exception:  # noqa: BLE001
        pass
    return keys


def is_netted(state: Any, lane: str, position_id: int) -> bool:
    try:
        return (str(lane), int(position_id)) in netted_keys(state)
    except Exception:  # noqa: BLE001
        log.exception("netted check failed")
        return False


# --------------------------------------------------------------------------- one-index gate


def open_index_contracts(state: Any) -> list[dict]:
    fams = _families(getattr(state.settings, "futures_index_families", "US5,TEK"))
    nk = netted_keys(state)
    out = []
    for lane, led in lane_ledgers(state).items():
        for p in _open_positions(led):
            if family_root(p.product) in fams and (lane, p.id) not in nk:
                out.append({"lane": lane, "id": p.id, "product": p.product, "side": p.side,
                            "contracts": lot_contracts(p.product, p.qty)})
    return out


def parse_index_slot_reservation(state: Any) -> tuple[str, set[str]] | None:
    """INDEX_SLOT_RESERVED_FOR "lane:families" -> (lane, families); "*" = FUTURES_INDEX_FAMILIES."""
    s = state.settings
    raw = str(getattr(s, "index_slot_reserved_for", "") or "").strip()
    if not raw:
        return None
    lane, _, fams = raw.partition(":")
    lane = lane.strip().lower()
    if not lane:
        return None
    idx = _families(getattr(s, "futures_index_families", "US5,TEK"))
    fams = fams.strip()
    if not fams or fams == "*":
        return lane, idx
    return lane, {f for f in _families(fams.replace("+", ",").replace(";", ","))}


def max_index_contracts(state: Any) -> int:
    """FUTURES_MAX_INDEX_CONTRACTS (default 1): total open index contracts across all lanes."""
    try:
        return max(1, int(getattr(state.settings, "futures_max_index_contracts", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _ex_net_blocks(ex: dict | None, max_n: int) -> bool:
    if not ex:
        return False
    tot = sum(abs(float(n)) for n in ex.values())
    return tot > 1e-9 and tot >= max_n - 1e-9


def index_entry_allowed(state: Any, product: str, lane: str | None = None) -> tuple[bool, str]:
    s = state.settings
    if not bool(getattr(s, "futures_one_index_contract", False)):
        return True, "one_index_gate_off"
    fam = family_root(product)
    if fam not in _families(getattr(s, "futures_index_families", "US5,TEK")):
        return True, "not_index"
    # Slot reservation (2026-10-10): only the reserved lane may take the single index slot.
    res = parse_index_slot_reservation(state)
    if res is not None and lane is not None and fam in res[1] and str(lane).lower() != res[0]:
        _count(state, "index_slot_reserved_blocked")
        return False, f"index_slot_reserved:{res[0]}"
    # Open-lot and exchange-net checks apply to EVERY lane, including the reserved one.
    max_n = max_index_contracts(state)
    opens = open_index_contracts(state)
    if len(opens) >= max_n:
        _count(state, "one_index_blocked")
        return False, "one_index_contract_open:" + ",".join(f"{o['lane']}#{o['id']}" for o in opens)
    # Also the REAL exchange NET exposure (Coinbase nets per product), when known & fresh.
    ex = exchange_index_net(state)
    if _ex_net_blocks(ex, max_n):
        _count(state, "one_index_blocked")
        return False, "one_index_exchange_net:" + ",".join(f"{p}={n:g}" for p, n in sorted(ex.items()) if n)
    return True, "ok"


def _norm_pid(product: str) -> str:
    from snowball.futures.market import normalize_futures_product

    try:
        return normalize_futures_product(str(product))
    except Exception:  # noqa: BLE001
        return str(product or "").strip().upper()


def _norm_side(side: str | None) -> str | None:
    if side is None:
        return None
    x = str(side).strip().lower()
    if x in ("short", "sell"):
        return "short"
    if x in ("long", "buy"):
        return "long"
    return None


def opposite_side_holders(state: Any, product: str, side: str) -> list[dict]:
    """Open CFM lots (any lane, non-netted) on the same product with the OPPOSITE side."""
    from snowball.futures.market import is_cfm_product

    pid = _norm_pid(product)
    want = _norm_side(side)
    if want is None or not is_cfm_product(pid):
        return []
    nk = netted_keys(state)
    out = []
    for ln, led in lane_ledgers(state).items():
        for p in _open_positions(led):
            if (ln, int(p.id)) in nk or _norm_pid(p.product) != pid:
                continue
            ps = "short" if str(p.side).lower() == "short" else "long"
            if ps != want:
                out.append({"lane": ln, "id": int(p.id), "product": pid, "side": ps})
    return out


def opposite_side_allowed(state: Any, product: str, side: str | None) -> tuple[bool, str]:
    """(A) 2026-10-10: block opening the opposite side of a CFM product held open by any lane.

    Coinbase nets per product, so an opposite lot silently closes (part of) the other lane's
    position at the exchange and realizes a hidden P/L. Also checks the fresh REAL exchange net.
    """
    s = state.settings
    if not bool(getattr(s, "opposite_side_block", True)):
        return True, "opposite_side_block_off"
    want = _norm_side(side)
    if want is None:
        return True, "side_unknown"
    from snowball.futures.market import is_cfm_product

    pid = _norm_pid(product)
    if not is_cfm_product(pid):
        return True, "not_cfm"
    hold = opposite_side_holders(state, pid, want)
    if hold:
        _count(state, "opposite_side_blocked")
        return False, "opposite_side_open:" + ",".join(f"{h['lane']}#{h['id']}:{h['side']}" for h in hold)
    x = getattr(state, "futures_exchange_net", None)
    if isinstance(x, dict) and time.monotonic() - float(x.get("mono", 0.0)) <= 600.0:
        n = float((x.get("net") or {}).get(pid, 0.0) or 0.0)
        if (want == "long" and n < -1e-9) or (want == "short" and n > 1e-9):
            _count(state, "opposite_side_blocked")
            return False, f"opposite_side_exchange_net:{pid}={n:g}"
    return True, "ok"


_LANE_LIVE = {"futures": "futures_live_orders_permitted", "crash": "crash_live_orders_permitted",
              "fed": "fed_live_orders_permitted", "stocks": "stock_live_orders_permitted"}
_LANE_PAIRS = {"futures": "futures_pairs", "crash": "crash_pairs", "fed": "fed_pairs", "stocks": "stock_pairs"}


def _lane_live(state: Any, lane: str) -> bool:
    fn = getattr(state.settings, _LANE_LIVE.get(str(lane), ""), None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:  # noqa: BLE001
        return False


def _lane_price(state: Any, lane: str, product: str) -> float | None:
    pid = _norm_pid(product)
    for attr in [_LANE_PAIRS.get(str(lane), "")] + list(_LANE_PAIRS.values()):
        pairs = getattr(state, attr, None) if attr else None
        if not isinstance(pairs, dict):
            continue
        snap = pairs.get(product) or pairs.get(pid)
        px = getattr(snap, "last", None) if snap is not None else None
        if px is not None and float(px) > 0:
            return float(px)
    return None


def _cached_liq(state: Any, max_age_s: float = 300.0) -> dict | None:
    """Freshest per-lane liq view written by run_stops (fallback when no market is passed)."""
    best, best_at = None, None
    now = datetime.now(timezone.utc)
    for v in (getattr(state, "futures_stop_views", {}) or {}).values():
        liq = (v or {}).get("liq") or {}
        at = _parse_dt((v or {}).get("at"))
        if not liq.get("available") or at is None or (now - at).total_seconds() > max_age_s:
            continue
        if best_at is None or at > best_at:
            best, best_at = liq, at
    return best


def margin_gate_eval(state: Any, product: str, side: str | None, *, market: Any = None,
                     price: float | None = None, liq: dict | None = None, lane: str = "futures") -> dict:
    """Pure-ish evaluation of the (C) margin gate for ONE new contract. Never sends anything."""
    from snowball.futures.market import is_cfm_product, margin_rate_for_product
    from snowball.sizing import cfm_required_margin_usd

    s = state.settings
    pid = _norm_pid(product)
    out: dict = {"product": pid, "side": _norm_side(side) or "long", "lane": lane}
    if not is_cfm_product(pid):
        return {**out, "ok": True, "why": "not_cfm"}
    if liq is None:
        if market is not None:
            liq = liq_levels(market, True)
        else:
            liq = _cached_liq(state)
    if not liq or not liq.get("available") or liq.get("buffer_usd") is None:
        return {**out, "ok": False, "why": "margin_gate_exchange_data_unavailable"}
    px = price if price is not None and float(price) > 0 else _lane_price(state, lane, pid)
    if px is None:
        return {**out, "ok": False, "why": "margin_gate_no_price"}
    buf = float(liq["buffer_usd"])
    n = float(liq.get("exchange_contracts") or 0.0)
    # SPOT_USD_FLOOR is not futures margin. Coinbase counts spot USD (cbi_usd_balance) in the CFM
    # buffer and sweeps it into the futures balance, so reserve the protected floor first
    # (10/14: TEK long at 7:52 AM CT pulled spot USD under the $300 floor; $385.70 swept 5:30 PM CT).
    floor = float(getattr(s, "spot_usd_floor", 0.0) or 0.0)
    cbi = liq.get("cbi_usd_balance")
    reserve = 0.0
    if floor > 0:
        reserve = floor if cbi is None else min(floor, max(0.0, float(cbi)))
        if cbi is not None and float(cbi) < floor:
            return {**out, "ok": False, "why": f"margin_gate_spot_floor_{float(cbi):.0f}_lt_{floor:.0f}",
                    "spot_usd_floor": floor, "cbi_usd_balance": round(float(cbi), 2)}
    buf -= reserve
    try:
        lev = float(s.cfm_order_leverage())
    except Exception:  # noqa: BLE001
        lev = 1.0
    m_new = cfm_required_margin_usd(float(px), contracts=1, leverage=lev, contract_size=_cs(pid),
                                    margin_rate=margin_rate_for_product(pid, out["side"],
                                                                        float(getattr(s, "cfm_margin_rate", 0.10))))
    after = (buf - m_new) / (n + 1.0)
    dist, leg = stop_distance_usd_per_contract(pid, float(px), s)
    guard = float(getattr(s, "futures_liq_guard_usd", 50.0) or 0.0)
    need = dist + (guard if bool(getattr(s, "futures_margin_gate_include_liq_guard", True)) else 0.0)
    ok = after + 1e-9 >= need
    return {**out, "ok": ok, "why": "ok" if ok else f"margin_gate_headroom_{after:.0f}_lt_{need:.0f}",
            "price": round(float(px), 4), "buffer_usd": round(buf, 2), "exchange_contracts": n,
            "new_contract_margin_usd": round(m_new, 2), "headroom_after_usd_per_contract": round(after, 2),
            "stop_dist_usd_per_contract": round(dist, 2), "stop_leg": leg, "liq_guard_usd": guard,
            "required_usd_per_contract": round(need, 2), "spot_floor_reserve_usd": round(reserve, 2)}


def margin_gate_allowed(state: Any, product: str, lane: str, side: str | None, *, market: Any = None,
                        live: bool | None = None, price: float | None = None) -> tuple[bool, str]:
    s = state.settings
    if not bool(getattr(s, "futures_margin_gate", True)):
        return True, "margin_gate_off"
    live = _lane_live(state, lane) if live is None else bool(live)
    if not live:
        return True, "margin_gate_paper"
    ev = margin_gate_eval(state, product, side, market=market, price=price, lane=lane)
    ev["at"] = datetime.now(timezone.utc).isoformat()
    try:
        state.margin_gate_last = ev
    except Exception:  # noqa: BLE001
        pass
    if not ev["ok"]:
        _count(state, "margin_gate_blocked")
        return False, ev["why"]
    return True, ev["why"]


def reentry_allowed(state: Any, product: str, now: datetime | None = None) -> tuple[bool, str]:
    st = get_store(state)
    if st is None:
        return True, "ok"
    now = now or datetime.now(timezone.utc)
    until = st.family_blocked_until(family_root(product), now)
    if until is not None:
        _count(state, "reentry_blocked")
        return False, f"stop_reentry_block_until:{until.isoformat()}"
    return True, "ok"


def futures_entry_gate(state: Any, product: str, lane: str, now: datetime | None = None, *,
                       side: str | None = None, market: Any = None, live: bool | None = None,
                       price: float | None = None) -> tuple[bool, str]:
    """Single entry check used by every futures lane's lowest-level open function.

    Order: stop re-entry block -> one-index gate (+ slot reservation) -> (A) opposite side ->
    (C) margin gate. New entries only; rolls/exits never call this.
    """
    ok, why = reentry_allowed(state, product, now)
    if ok:
        ok, why = index_entry_allowed(state, product, lane)
    if ok:
        ok, why = opposite_side_allowed(state, product, side)
    if ok:
        ok, why = margin_gate_allowed(state, product, lane, side, market=market, live=live, price=price)
    if not ok and _throttled(("entry_blocked", lane, product, why.split(":", 1)[0]), 300.0):
        log.info("futures entry blocked", extra={"data": {"lane": lane, "product": product, "side": side,
                                                         "reason": why}})
    return ok, why


def _count(state: Any, key: str) -> None:
    c = getattr(state, "stop_counters", None)
    if not isinstance(c, dict):
        c = {}
        try:
            state.stop_counters = c
        except Exception:  # noqa: BLE001
            return
    c[key] = int(c.get(key, 0)) + 1


# --------------------------------------------------------------------------- exchange


_EX_CACHE: dict[tuple[int, str], tuple[float, Any]] = {}


def _ex(market: Any) -> Any:
    return getattr(market, "exchange", None) or getattr(market, "_exchange", None)


def _invalidate_exchange_cache(market: Any) -> None:
    ex = _ex(market)
    for k in ((id(ex), "pos"), (id(ex), "bal")):
        _EX_CACHE.pop(k, None)


def exchange_positions(market: Any, *, max_age: float = 0.0) -> dict[str, dict] | None:
    """{product_id: {"side": "long"/"short", "contracts": float}} from /cfm/positions; None if unknown."""
    ex = _ex(market)
    fn = getattr(ex, "v3PrivateGetBrokerageCfmPositions", None) if ex is not None else None
    if not callable(fn):
        return None
    key = (id(ex), "pos")
    hit = _EX_CACHE.get(key)
    if max_age > 0 and hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    try:
        raw = fn()
    except Exception:  # noqa: BLE001
        log.exception("cfm positions fetch failed")
        return None
    out: dict[str, dict] = {}
    for p in (raw or {}).get("positions") or []:
        try:
            n = float(p.get("number_of_contracts") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        side = str(p.get("side") or "").upper()
        out[str(p.get("product_id"))] = {"side": "short" if side == "SHORT" else "long", "contracts": n}
    _EX_CACHE[key] = (time.monotonic(), out)
    return out


def balance_summary(market: Any, *, max_age: float = 20.0) -> dict | None:
    ex = _ex(market)
    fn = getattr(ex, "v3PrivateGetBrokerageCfmBalanceSummary", None) if ex is not None else None
    if not callable(fn):
        return None
    key = (id(ex), "bal")
    hit = _EX_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    try:
        bs = (fn() or {}).get("balance_summary") or {}
    except Exception:  # noqa: BLE001
        log.exception("cfm balance summary fetch failed")
        return None

    def v(x: Any) -> float | None:
        try:
            return float(x["value"] if isinstance(x, dict) else x)
        except (TypeError, ValueError, KeyError):
            return None

    avail = v(bs.get("available_margin"))
    cur_buf = v(bs.get("liquidation_buffer_amount"))
    on = bs.get("overnight_margin_window_measure") or {}
    on_mm = v(on.get("maintenance_margin"))
    on_buf = (avail - on_mm) if (avail is not None and on_mm is not None) else None
    bufs = [b for b in (cur_buf, on_buf) if b is not None]
    out = {"available_margin": avail, "current_liquidation_buffer": cur_buf,
           "overnight_maintenance": on_mm, "overnight_liquidation_buffer": on_buf,
           "liquidation_buffer_used": min(bufs) if bufs else None,
           "cbi_usd_balance": v(bs.get("cbi_usd_balance")), "cfm_usd_balance": v(bs.get("cfm_usd_balance"))}
    _EX_CACHE[key] = (time.monotonic(), out)
    return out


def verify_close_allowed(market: Any, product: str, lot_side: str, contracts: int) -> tuple[bool, str, float | None]:
    """Refuse any closing order that could OPEN or FLIP a CFM position (no reduce-only on FCM)."""
    from snowball.futures.market import is_cfm_product, normalize_futures_product

    pid = normalize_futures_product(product)
    if not is_cfm_product(pid):
        return True, "not_cfm", None
    ex = _ex(market)
    if ex is None or not callable(getattr(ex, "v3PrivateGetBrokerageCfmPositions", None)):
        # Test fakes / non-Coinbase venues only; the live ccxt Coinbase client always has it.
        return True, "position_check_unsupported", None
    pos = exchange_positions(market)
    if pos is None:
        return False, "exchange_position_unknown", None
    p = pos.get(pid)
    if p is None:
        return False, "exchange_flat_close_would_open", 0.0
    if p["side"] != str(lot_side).lower():
        return False, f"exchange_side_{p['side']}_close_would_flip", p["contracts"]
    if p["contracts"] + 1e-9 < float(contracts):
        return False, f"exchange_contracts_{p['contracts']:g}_lt_{contracts}", p["contracts"]
    return True, "ok", p["contracts"]


def liq_levels(market: Any, live: bool, nk_lanes_lots: list | None = None) -> dict:
    """Headroom per contract from the REAL CFM balance summary (live only)."""
    if not live:
        return {"available": False, "why": "paper"}
    bs = balance_summary(market)
    pos = exchange_positions(market, max_age=20.0)
    if not bs or pos is None or bs.get("liquidation_buffer_used") is None:
        return {"available": False, "why": "exchange_data_unavailable"}
    n = sum(float(p["contracts"]) for p in pos.values())
    buf = float(bs["liquidation_buffer_used"])
    return {"available": True, "buffer_usd": buf, "exchange_contracts": n,
            "headroom_usd_per_contract": (buf / n) if n > 0 else None, **bs}


def reconcile(state: Any, market: Any, live: bool) -> dict:
    """Per CFM product: ledger NET (non-netted lots, all lanes, signed contracts) vs REAL exchange net.

    A live stop only fires for a product whose ledger net equals the exchange net (so stop sizes
    always sum to the real net position). Also records the exchange net for the one-index gate.
    """
    if not live:
        return {"available": False, "why": "paper"}
    ex = _ex(market)
    if ex is None or not callable(getattr(ex, "v3PrivateGetBrokerageCfmPositions", None)):
        return {"available": False, "why": "unsupported"}  # test fakes / non-Coinbase venues only
    pos = exchange_positions(market, max_age=20.0)
    if pos is None:
        return {"available": False, "why": "exchange_positions_unknown"}
    from snowball.futures.market import is_cfm_product, normalize_futures_product

    nk = netted_keys(state)
    ledger: dict[str, int] = {}
    for lane, led in lane_ledgers(state).items():
        for p in _open_positions(led):
            if not is_cfm_product(p.product) or (lane, int(p.id)) in nk:
                continue
            pid = normalize_futures_product(p.product)
            n = lot_contracts(p.product, p.qty)
            ledger[pid] = ledger.get(pid, 0) + (-n if str(p.side).lower() == "short" else n)
    exch = {pid: (-1 if v["side"] == "short" else 1) * float(v["contracts"]) for pid, v in pos.items()}
    prods = {}
    for pid in sorted(set(ledger) | set(exch)):
        ln, en = float(ledger.get(pid, 0)), float(exch.get(pid, 0.0))
        prods[pid] = {"ledger_net": ln, "exchange_net": en, "ok": abs(ln - en) < 1e-9}
    out = {"available": True, "products": prods, "all_ok": all(v["ok"] for v in prods.values()),
           "at": datetime.now(timezone.utc).isoformat()}
    try:
        state.futures_exchange_net = {"mono": time.monotonic(), "at": out["at"], "net": exch}
        state.futures_reconcile = out
    except Exception:  # noqa: BLE001
        pass
    return out


def exchange_index_net(state: Any, max_age: float = 600.0) -> dict[str, float] | None:
    """Fresh REAL exchange net contracts for index families (None if unknown/stale/paper)."""
    x = getattr(state, "futures_exchange_net", None)
    if not isinstance(x, dict) or time.monotonic() - float(x.get("mono", 0.0)) > max_age:
        return None
    fams = _families(getattr(state.settings, "futures_index_families", "US5,TEK"))
    return {p: n for p, n in (x.get("net") or {}).items() if family_root(p) in fams}


def open_index_n(state: Any) -> tuple[int, str]:
    """Count open US5/TEK index contracts (N). Prefer fresh exchange net; else ledger.

    When both are known and disagree, use the larger N so the reserve is not under-sized.
    """
    ledger_n = 0
    try:
        ledger_n = int(round(sum(abs(float(o.get("contracts") or 1.0)) for o in open_index_contracts(state))))
    except Exception:  # noqa: BLE001
        log.exception("open_index_n ledger count failed")
    ex = exchange_index_net(state)
    if ex is not None:
        ex_n = int(round(sum(abs(float(n)) for n in ex.values())))
        if ledger_n and ex_n != ledger_n:
            return max(ex_n, ledger_n), "exchange_net_ledger_max"
        return ex_n, "exchange_net"
    return ledger_n, "ledger"


def spot_index_buffer_eval(state: Any, *, cost_usd: float, pending_usd: float = 0.0,
                           buffer_usd: float | None = None, n: int | None = None) -> dict:
    """Block a spot buy that would leave CFM buffer under N × $300 (Tb via nala 2026-11-11).

    Pure evaluation; never sends an order. Dividend and dip-buy are exempt because they
    never call this. Fails closed in live when N > 0 and the buffer is unknown.
    """
    s = getattr(state, "settings", None)
    on = bool(getattr(s, "spot_index_buffer_reserve_enabled", True))
    per = float(getattr(s, "spot_index_buffer_reserve_usd", 300.0) or 0.0)
    out: dict = {"enabled": on, "per_lot_usd": per, "cost_usd": round(float(cost_usd), 2),
                 "pending_usd": round(float(pending_usd), 2)}
    if not on or per <= 0:
        return {**out, "ok": True, "why": "reserve_off", "n": 0, "need_usd": 0.0}
    n_src = "given"
    if n is None:
        n, n_src = open_index_n(state)
    n = int(n or 0)
    out["n"] = n
    out["n_source"] = n_src
    if n <= 0:
        return {**out, "ok": True, "why": "no_open_index", "need_usd": 0.0}
    need = n * per
    out["need_usd"] = round(need, 2)
    if buffer_usd is None:
        liq = _cached_liq(state)
        if not liq or liq.get("buffer_usd") is None:
            live = bool(getattr(s, "live_enabled", False)) and str(getattr(s, "mode", "")) == "live"
            # Fail closed only when live: a stale/missing buffer must not let a spot buy
            # drain the stop's reserve. Paper/tests without a liq cache stay open unless
            # they pass buffer_usd or a live settings flag.
            if live:
                return {**out, "ok": False, "why": "spot_index_buffer_data_unavailable",
                        "buffer_usd": None, "after_usd": None}
            return {**out, "ok": True, "why": "no_buffer_non_live", "buffer_usd": None, "after_usd": None}
        buffer_usd = float(liq["buffer_usd"])
    after = float(buffer_usd) - float(pending_usd) - float(cost_usd)
    ok = after + 1e-9 >= need
    return {**out, "ok": ok,
            "why": "ok" if ok else f"spot_index_buffer_{after:.0f}_lt_{need:.0f}",
            "buffer_usd": round(float(buffer_usd), 2), "after_usd": round(after, 2)}


# --------------------------------------------------------------------------- per-lot view


def lot_view(state: Any, lane: str, lot: Any, bid: float | None, ask: float | None, mark: float | None,
             liq: dict | None) -> dict:
    s = state.settings
    basis = float(lot.gate_entry_price)
    spx = stop_price(lot.side, basis, lot.product, s)
    dist, leg = stop_distance_usd_per_contract(lot.product, basis, s)
    px = worst_touch(lot.side, bid, ask, mark)
    cs = _cs(lot.product)
    n = lot_contracts(lot.product, lot.qty)
    to_stop_usd = None
    if px is not None:
        to_stop_usd = ((spx - px) if lot.side == "short" else (px - spx)) * cs * n
    out = {"lane": lane, "id": lot.id, "product": lot.product, "side": lot.side, "contracts": n,
           "basis": basis, "stop_px": round(spx, 4), "stop_leg": leg, "stop_dist_usd_per_contract": round(dist, 2),
           "touch_px": px, "distance_to_stop_usd": None if to_stop_usd is None else round(to_stop_usd, 2),
           "netted": False, "liq_px_est": None, "liq_headroom_usd_per_contract": None}
    if liq and liq.get("available") and liq.get("headroom_usd_per_contract") is not None and px is not None:
        h = float(liq["headroom_usd_per_contract"])
        out["liq_headroom_usd_per_contract"] = round(h, 2)
        out["liq_px_est"] = round(px + h / cs if lot.side == "short" else px - h / cs, 2)
        out["liq_guard_usd"] = float(getattr(s, "futures_liq_guard_usd", 50.0))
    return out


# --------------------------------------------------------------------------- runner


@dataclass
class LaneHooks:
    lane: str
    ledger: Any
    market: Any
    pairs: dict
    live: bool
    book: Callable[[Any, float, float, str, datetime], float]  # (lot, fill_px, fee, reason, ts) -> realized
    skip: Callable[[Any], bool] | None = None  # e.g. contract roll in flight for this lot


_LOG_AT: dict[tuple, float] = {}
# execute_stop results after which the next triggered kind (e.g. the liq guard) still tries.
_TRY_NEXT_KIND = ("max_attempts", "retry_wait", "error", "exception", "no_price")


def _throttled(key: tuple, every: float = 300.0) -> bool:
    now = time.monotonic()
    last = _LOG_AT.get(key)
    if last is None or now - last >= every:
        _LOG_AT[key] = now
        return True
    return False


def run_stops(state: Any, hooks: LaneHooks, *, now: datetime, can_trade: bool,
              products: list[str] | None = None) -> list[dict]:
    """Bot-side stop + liquidation-guard check for one lane. Runs during HALT / daily kill."""
    s = state.settings
    views: list[dict] = []
    nk = netted_keys(state)
    liq = liq_levels(hooks.market, hooks.live)
    rec = reconcile(state, hooks.market, hooks.live) if hooks.live else {"available": False, "why": "paper"}
    guard = float(getattr(s, "futures_liq_guard_usd", 50.0) or 0.0)
    enabled = bool(getattr(s, "futures_stops_enabled", True))
    lots = _open_positions(hooks.ledger)
    if products is not None:
        lots = [l for l in lots if l.product in products]
    for lot in lots:
        snap = hooks.pairs.get(lot.product)
        bid = getattr(snap, "bid", None); ask = getattr(snap, "ask", None); mark = getattr(snap, "last", None)
        v = lot_view(state, hooks.lane, lot, bid, ask, mark, liq)
        try:
            v.update(aged_view(state, hooks.lane, hooks.ledger, lot, bid, ask, mark, now))
        except Exception:  # noqa: BLE001
            log.exception("aged-lot view failed", extra={"data": {"lane": hooks.lane, "position_id": lot.id}})
        if (hooks.lane, lot.id) in nk:
            v.update({"netted": True, "stop_px": None, "distance_to_stop_usd": None, "liq_px_est": None,
                      "liq_headroom_usd_per_contract": None, "aged_exit_armed": False,
                      "aged_qualifies": False, "aged_excluded": "netted"})
            views.append(v)
            continue
        views.append(v)
        aged_go = bool(v.get("aged_qualifies"))
        if not enabled and not aged_go:
            continue
        if hooks.skip is not None:
            try:
                if hooks.skip(lot):
                    continue
            except Exception:  # noqa: BLE001
                continue
        # A working (queued after-hours) closing limit is monitored every tick, triggered or not,
        # so its fill is booked before reconcile would see ledger != exchange.
        if hooks.live:
            try:
                wres = poll_working_close(state, hooks, lot, v, now=now)
            except Exception:  # noqa: BLE001
                wres = None
                log.exception("futures working close poll failed", extra={"data": {"lane": hooks.lane,
                                                                                   "position_id": lot.id}})
            if wres in ("live_filled", "working"):
                continue
        # 2026-10-30: stop and liquidation guard are evaluated INDEPENDENTLY (TEK #21: the stop was
        # hit, its record was stuck, and the guard was never even evaluated). Each kind has its own
        # order record; if one cannot send, the next triggered kind still tries.
        kinds: list[str] = []
        if enabled and stop_triggered(lot.side, v["stop_px"], bid, ask, mark):
            kinds.append("stop")
        if (enabled and liq.get("available") and v.get("liq_headroom_usd_per_contract") is not None
                and v["liq_headroom_usd_per_contract"] <= guard):
            kinds.append(LIQ_KIND)
        if aged_go and not kinds:
            kinds.append(AGED_KIND)
        if not kinds:
            continue
        v["kinds_triggered"] = list(kinds)
        if hooks.live and rec.get("why") != "unsupported":
            from snowball.futures.market import normalize_futures_product

            pr = (rec.get("products") or {}).get(normalize_futures_product(lot.product)) if rec.get("available") else None
            if not pr or not pr.get("ok"):
                _count(state, "stop_refused_unreconciled")
                if _throttled(("unrec", hooks.lane, lot.id)):
                    log.critical("futures stop REFUSED: ledger net != exchange net for product (reconcile first)",
                                 extra={"data": {"lane": hooks.lane, "position_id": lot.id, "product": lot.product,
                                                 "kinds": kinds, "reconcile": pr or rec.get("why")}})
                continue
        for kind in kinds:
            if kind == AGED_KIND:
                reason = f"{AGED_REASON_BASE}:{lot.strategy}"
            else:
                reason = f"{STOP_REASON_BASE}:{LIQ_KIND + ':' if kind == LIQ_KIND else ''}{lot.strategy}"
            try:
                res = execute_stop(state, hooks, lot, v, reason=reason, kind=kind, now=now, can_trade=can_trade)
            except Exception:  # noqa: BLE001
                log.exception("futures stop execution failed", extra={"data": {"lane": hooks.lane, "position_id": lot.id,
                                                                               "kind": kind}})
                res = "exception"
            if res not in _TRY_NEXT_KIND:
                break
    try:
        cur = dict(getattr(state, "futures_stop_views", {}) or {})
        cur[hooks.lane] = {"lots": views, "reconcile": rec, "liq": {k: liq.get(k) for k in (
            "available", "why", "buffer_usd", "exchange_contracts", "headroom_usd_per_contract",
            "available_margin", "current_liquidation_buffer", "overnight_liquidation_buffer")},
            "at": now.isoformat()}
        state.futures_stop_views = cur
    except Exception:  # noqa: BLE001
        pass
    return views


def execute_stop(state: Any, hooks: LaneHooks, lot: Any, v: dict, *, reason: str, kind: str,
                 now: datetime, can_trade: bool) -> str:
    s = state.settings
    lane = hooks.lane
    st = get_store(state)
    n = int(v["contracts"])
    data = {"lane": lane, "position_id": lot.id, "product": lot.product, "side": lot.side, "basis": v["basis"],
            "stop_px": v["stop_px"], "touch_px": v["touch_px"], "kind": kind, "reason": reason,
            "est_loss_usd": None if v["distance_to_stop_usd"] is None else
            round(-((abs(v["touch_px"] - v["basis"])) * _cs(lot.product) * n) - round_trip_fee_per_contract(v["basis"], lot.product, s) * n, 2)}
    aged = kind == AGED_KIND
    if aged:
        data.update({"age_days": v.get("age_days"), "aged_target_px": v.get("aged_target_px"),
                     "aged_net_usd_now": v.get("aged_net_usd_now"), "est_loss_usd": None})
    if not can_trade:
        if aged:
            return "trading_disabled"
        if _throttled(("notrade", lane, lot.id)):
            log.critical("futures stop triggered but trading disabled", extra={"data": data})
        return "trading_disabled"
    if _throttled(("trig", kind, lane, lot.id), 60.0):
        log.warning("futures AGED-LOT EXIT triggered (net green after fees)" if aged else "futures STOP triggered",
                    extra={"data": data})
    close_side = "buy" if lot.side == "short" else "sell"
    if not hooks.live:
        from snowball.market import fill_price
        from snowball.models import Ticker

        snap = hooks.pairs.get(lot.product)
        t = Ticker(product=lot.product, last=getattr(snap, "last", None), bid=getattr(snap, "bid", None),
                   ask=getattr(snap, "ask", None), ts=now)
        px = fill_price(t, close_side, float(getattr(s, "slippage_bps", 0.0) or 0.0))
        if px is None or px <= 0:
            return "no_price"
        if aged and not aged_net_ok(state, hooks.ledger, lot, float(px))[0]:
            return "aged_not_green_at_fill"
        realized = hooks.book(lot, float(px), 0.0, reason, now)
        _after_fill(state, st, lane, lot, v, reason, kind, float(px), 0.0, realized, None, now)
        return "paper_filled"
    if st is None:
        log.critical("futures stop refused: stop store unavailable (cannot guarantee one order)", extra={"data": data})
        return "no_store"
    okey = order_key(lane, kind)
    ttl = float(getattr(s, "futures_stop_failed_ttl_sec", 900.0) or 0.0)
    # One closing order per lot across stop, liq guard AND aged exit: the other kinds' rows first.
    for other in lane_order_keys(lane):
        if other == okey:
            continue
        orow = st.order_row(other, lot.id)
        if not orow:
            continue
        if orow.get("status") == "filled":
            okind = _key_kind(lane, other)
            oreason = str(orow.get("reason") or reason)
            realized = hooks.book(lot, float(orow["fill_px"]), float(orow.get("fee_usd") or 0.0), oreason, now)
            st.upsert_order(other, lot.id, status="booked")
            _after_fill(state, st, lane, lot, v, oreason, okind, float(orow["fill_px"]),
                        float(orow.get("fee_usd") or 0.0), realized, orow.get("order_id"), now, record_order=False)
            return "booked_from_store"
        if orow.get("status") in _INFLIGHT:
            if _throttled(("pending_other", okey, lot.id)):
                log.critical("futures closing order already sent for this lot (other kind); not sending another",
                             extra={"data": {**data, "order": orow}})
            return "already_sent"
        if orow.get("status") == "error" and _row_age(orow, now) < 60.0 and not _explicit_reject(orow.get("note")):
            # An ambiguous send error (e.g. timeout) on another kind moments ago: the order may exist.
            return "retry_wait"
    row = st.order_row(okey, lot.id)
    if row and row.get("status") == "filled":
        # Filled before a restart but not booked: book from the stored fill; never resend.
        realized = hooks.book(lot, float(row["fill_px"]), float(row.get("fee_usd") or 0.0), reason, now)
        st.upsert_order(okey, lot.id, status="booked")
        _after_fill(state, st, lane, lot, v, reason, kind, float(row["fill_px"]), float(row.get("fee_usd") or 0.0),
                    realized, row.get("order_id"), now, record_order=False)
        return "booked_from_store"
    if row and row.get("status") in ("sent", "sent_unconfirmed") and row.get("order_id") \
            and str(row.get("order_id")) not in ("None", ""):
        res = poll_working_close(state, hooks, lot, v, now=now, keys=(okey,), force=True)
        if res in ("live_filled", "working"):
            return res
        row = st.order_row(okey, lot.id)
    if row and row.get("status") in ("sending", "sent", "sent_unconfirmed", "booked"):
        if _throttled(("pending", okey, lot.id)):
            log.critical("futures closing order already sent for this lot; not resending (reconcile)",
                         extra={"data": {**data, "order": row}})
        return "already_sent"
    # 2026-10-30 (TEK #21): a failed record (status error / expired, never acknowledged by the
    # exchange) from an EARLIER trigger must not be reused. Past FUTURES_STOP_FAILED_TTL_SEC it
    # expires: attempts reset and this trigger builds a fresh order. The exchange-size check below
    # still guards against an errored send that actually executed.
    if row and row.get("status") in ("error", "expired") and ttl > 0 and _row_age(row, now) >= ttl:
        log.warning("futures stop: stale failed order record expired; building a fresh order",
                    extra={"data": {**data, "stale_order": {k: row.get(k) for k in (
                        "status", "attempts", "order_id", "created_at", "updated_at", "reason")},
                        "ttl_sec": ttl}})
        _count(state, "stop_stale_record_expired")
        st.upsert_order(okey, lot.id, status="expired", attempts=0, order_id=None, reason=reason,
                        note=f"expired stale failed record (last update {row.get('updated_at')}); fresh order")
        row = st.order_row(okey, lot.id)
    if row and row.get("status") == "error":
        if int(row.get("attempts") or 0) >= 3:
            if _throttled(("maxatt", lane, okey, lot.id)):
                log.critical("futures stop gave up after 3 failed attempts (record expires; next trigger "
                             "after FUTURES_STOP_FAILED_TTL_SEC builds a fresh order)",
                             extra={"data": {**data, "order": row, "ttl_sec": ttl}})
            return "max_attempts"
        if _row_age(row, now) < 60.0:
            return "retry_wait"
    if aged:
        # Fresh book right before sending: still >= the aged net floor at the worst touch?
        fresh = _fresh_touch(hooks.market, lot)
        if fresh is False:
            return "aged_no_fresh_book"
        if fresh is not None:
            ok_g, net_g = aged_net_ok(state, hooks.ledger, lot, float(fresh))
            if not ok_g:
                if _throttled(("aged_recheck", lane, lot.id), 300.0):
                    log.info("aged-lot exit held: not net green on fresh book",
                             extra={"data": {**data, "fresh_touch": fresh, "net_usd": net_g}})
                return "aged_not_green_fresh"
    ok, why, ex_n = verify_close_allowed(hooks.market, lot.product, lot.side, n)
    if not ok:
        _count(state, "stop_refused_position_check")
        if _throttled(("refuse", lane, lot.id)):
            log.critical(f"futures {'aged-lot exit' if aged else 'stop'} REFUSED by exchange-position check (would open/flip)",
                         extra={"data": {**data, "why": why, "exchange_contracts": ex_n}})
        return "refused:" + why
    if row and row.get("status") in ("error", "expired") and row.get("pre_exchange_contracts") is not None \
            and ex_n is not None and float(ex_n) < float(row["pre_exchange_contracts"]) - 1e-9:
        st.upsert_order(okey, lot.id, status="sent_unconfirmed", note="exchange size dropped after errored send")
        log.critical("futures stop: exchange size dropped after an errored send; not resending", extra={"data": data})
        return "already_sent"
    st.upsert_order(okey, lot.id, product=lot.product, side=lot.side, contracts=n, reason=reason,
                    status="sending", pre_exchange_contracts=ex_n, order_id=None,
                    attempts=int((row or {}).get("attempts") or 0) + 1)
    from snowball.futures.market import is_cfm_product, parse_futures_order_fill

    lev = s.cfm_order_leverage() if is_cfm_product(lot.product) else 1.0
    try:
        order, otype = _send_close(hooks, lot, close_side, n, lev, v, s)
    except Exception as exc:  # noqa: BLE001
        _invalidate_exchange_cache(hooks.market)
        st.upsert_order(okey, lot.id, status="error", note=str(exc)[:300])
        log.exception("futures stop closing order failed", extra={"data": data})
        return "error"
    _invalidate_exchange_cache(hooks.market)  # positions/margin changed: next read must be fresh
    fill_px, fill_qty, fee = parse_futures_order_fill(order if isinstance(order, dict) else {})
    oid = _order_id(order)
    if fill_px <= 0 or fill_qty <= 0 or not _fully_filled(order, fill_qty):
        st.upsert_order(okey, lot.id, status="sent", order_id=oid,
                        note=(f"{otype} order working (no full fill yet); monitored by order id"
                              if otype == "limit" else "market order returned no fill; reconcile"))
        log.critical("futures stop order sent but not (fully) filled yet; monitoring, not resending",
                     extra={"data": {**data, "order_type": otype, "order_id": oid,
                                     "limit_px": (order or {}).get("limit_px") if isinstance(order, dict) else None}})
        return "sent_no_fill"
    st.upsert_order(okey, lot.id, status="filled", order_id=oid, fill_px=fill_px,
                    fill_qty=fill_qty, fee_usd=fee, note=f"{otype} close")
    realized = hooks.book(lot, float(fill_px), float(fee), reason, now)
    st.upsert_order(okey, lot.id, status="booked")
    if aged and realized is not None and float(realized) < 0:
        log.error("aged-lot exit filled below the net floor (slippage vs checked touch)",
                  extra={"data": {**data, "fill_px": fill_px, "realized": realized}})
    _after_fill(state, st, lane, lot, v, reason, kind, float(fill_px), float(fee), realized, oid, now)
    return "live_filled"


# --------------------------------------------------------------------------- closing order helpers

_MARKET_REJECT_MARKERS = ("AFTER_HOUR_INVALID_ORDER_TYPE", "MARKET_ORDERS_PROHIBITED", "INVALID_FCM_TRADING_SESSION",
                          "NOT_ALLOWED_BY_MARKET_STATE")


def _row_age(row: dict, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(str(row["updated_at"]))).total_seconds()
    except Exception:  # noqa: BLE001
        return 1e12


def _explicit_reject(note: Any) -> bool:
    """True when an error note is an explicit Coinbase rejection (no order was created)."""
    t = str(note or "")
    return '"success":false' in t.replace(" ", "") or "preview_failure_reason" in t or "allow_orders=False" in t


def market_rejected_for_session(exc: BaseException) -> bool:
    t = str(exc).upper()
    return any(m in t for m in _MARKET_REJECT_MARKERS)


def _order_id(order: Any) -> str | None:
    if not isinstance(order, dict):
        return None
    oid = order.get("id") or ((order.get("info") or {}).get("order_id") if isinstance(order.get("info"), dict) else None)
    return None if oid in (None, "", "None") else str(oid)


def _fully_filled(order: Any, fill_qty: float) -> bool:
    if not isinstance(order, dict):
        return False
    status = str(order.get("status") or "").lower()
    rem = order.get("remaining")
    if rem is not None:
        try:
            return float(rem) <= 1e-9
        except (TypeError, ValueError):
            pass
    if status in ("open", "pending", "new"):
        return False
    return fill_qty > 0


def close_limit_price(side: str, bid: float | None, ask: float | None, mark: float | None, collar: float) -> float | None:
    """Marketable closing limit: sell at bid*(1-collar), buy at ask*(1+collar); mark fallback."""
    if str(side).lower() == "sell":
        ref = bid if bid is not None and float(bid) > 0 else mark
        return None if ref is None or float(ref) <= 0 else float(ref) * (1.0 - collar)
    ref = ask if ask is not None and float(ask) > 0 else mark
    return None if ref is None or float(ref) <= 0 else float(ref) * (1.0 + collar)


def _send_close(hooks: LaneHooks, lot: Any, close_side: str, n: int, lev: float, v: dict, s: Any) -> tuple[dict, str]:
    """Build and send a FRESH closing order. Returns (order, "market"|"limit")."""
    mode = str(getattr(s, "futures_stop_order_type", "auto") or "auto").strip().lower()
    collar = float(getattr(s, "futures_stop_limit_collar_pct", 0.005) or 0.0)
    m = hooks.market

    def _limit() -> tuple[dict, str]:
        bid = ask = None
        fb = getattr(m, "fetch_bba", None)
        if callable(fb):
            try:
                bid, ask = fb(lot.product)
            except Exception:  # noqa: BLE001
                bid = ask = None
        snap = hooks.pairs.get(lot.product) if isinstance(hooks.pairs, dict) else None
        if bid is None:
            bid = getattr(snap, "bid", None)
        if ask is None:
            ask = getattr(snap, "ask", None)
        px = close_limit_price(close_side, bid, ask, getattr(snap, "last", None) or v.get("touch_px"), collar)
        if px is None:
            raise ValueError("closing limit refused: no usable book price")
        fn = getattr(m, "create_swap_limit_close_order", None)
        if not callable(fn):
            raise RuntimeError("market client has no create_swap_limit_close_order")
        return fn(lot.product, close_side, float(n), price=px, leverage=lev), "limit"

    if mode == "limit":
        return _limit()
    try:
        return m.create_swap_market_order(lot.product, close_side, float(n), leverage=lev, reduce_only=True), "market"
    except Exception as exc:  # noqa: BLE001
        if mode == "market" or not market_rejected_for_session(exc):
            raise
        log.warning("futures stop: market order rejected for the session; sending a closing LIMIT instead",
                    extra={"data": {"product": lot.product, "side": close_side, "contracts": n,
                                    "error": str(exc)[:200]}})
        return _limit()


def poll_working_close(state: Any, hooks: LaneHooks, lot: Any, v: dict, *, now: datetime,
                       keys: tuple[str, ...] | None = None, force: bool = False) -> str | None:
    """Check a sent-but-unfilled closing order (by order id). Books a fill; frees a dead order.

    Returns "live_filled", "working", "ended" or None (nothing to poll).
    """
    st = get_store(state)
    fetch = getattr(hooks.market, "fetch_order_safe", None)
    if st is None or not callable(fetch):
        return None
    from snowball.futures.market import parse_futures_order_fill

    for key in (keys or lane_order_keys(hooks.lane)):
        row = st.order_row(key, lot.id)
        if not row or row.get("status") not in ("sent", "sent_unconfirmed"):
            continue
        oid = row.get("order_id")
        if not oid or str(oid) in ("None", ""):
            continue
        if not force and not _throttled(("poll_working", key, lot.id), 20.0):
            return "working"
        o = fetch(str(oid), lot.product)
        if not isinstance(o, dict):
            return "working"
        fill_px, fill_qty, fee = parse_futures_order_fill(o)
        status = str(o.get("status") or "").lower()
        kind = _key_kind(hooks.lane, key)
        reason = str(row.get("reason") or f"{STOP_REASON_BASE}:{lot.strategy}")
        if fill_px > 0 and fill_qty > 0 and _fully_filled(o, fill_qty) and status not in ("open",):
            st.upsert_order(key, lot.id, status="filled", fill_px=fill_px, fill_qty=fill_qty, fee_usd=fee,
                            note="working close filled")
            realized = hooks.book(lot, float(fill_px), float(fee), reason, now)
            st.upsert_order(key, lot.id, status="booked")
            _after_fill(state, st, hooks.lane, lot, v, reason, kind, float(fill_px), float(fee), realized, oid, now)
            return "live_filled"
        if status in ("canceled", "cancelled", "rejected", "expired", "failed") and fill_qty <= 0:
            st.upsert_order(key, lot.id, status="error", order_id=None,
                            note=f"working close {oid} ended {status} unfilled; fresh order on next trigger")
            log.critical("futures working closing order ended unfilled; next trigger builds a fresh order",
                         extra={"data": {"lane": hooks.lane, "position_id": lot.id, "order_id": oid, "status": status}})
            return "ended"
        return "working"
    return None


def _after_fill(state: Any, st: StopStore | None, lane: str, lot: Any, v: dict, reason: str, kind: str,
                fill_px: float, fee: float, realized: float | None, order_id: Any, now: datetime,
                record_order: bool = True) -> None:
    _count(state, f"stops_{kind}")
    if kind == AGED_KIND:
        if st is not None:
            st.event(ts=now.isoformat(), lane=lane, position_id=lot.id, product=lot.product, side=lot.side, kind=kind,
                     reason=reason, basis=v.get("basis"), stop_px=v.get("aged_target_px"), trigger_px=v.get("touch_px"),
                     fill_px=fill_px, fee_usd=fee, realized_usd=realized,
                     order_id=None if order_id is None else str(order_id),
                     note=f"aged lot {v.get('age_days')}d; net-green close; no re-entry block")
        log.warning("futures AGED-LOT EXIT filled", extra={"data": {
            "lane": lane, "position_id": lot.id, "product": lot.product, "side": lot.side, "fill_px": fill_px,
            "fee": fee, "realized": realized, "reason": reason, "age_days": v.get("age_days")}})
        return
    until = next_et_midnight(now)
    if st is not None:
        st.block_family(family_root(lot.product), until, lane, lot.id, reason)
        st.event(ts=now.isoformat(), lane=lane, position_id=lot.id, product=lot.product, side=lot.side, kind=kind,
                 reason=reason, basis=v["basis"], stop_px=v["stop_px"], trigger_px=v["touch_px"], fill_px=fill_px,
                 fee_usd=fee, realized_usd=realized, order_id=None if order_id is None else str(order_id),
                 note=f"re-entry blocked until {until.isoformat()}")
    log.warning("futures STOP filled", extra={"data": {"lane": lane, "position_id": lot.id, "product": lot.product,
                                                        "side": lot.side, "fill_px": fill_px, "fee": fee,
                                                        "realized": realized, "reason": reason, "kind": kind,
                                                        "reentry_block_until": until.isoformat()}})


# --------------------------------------------------------------------------- aged-lot exit

AGED_KIND = "aged_exit"
AGED_REASON_BASE = "aged_exit"
AGED_SUFFIX = "#aged"
LIQ_SUFFIX = "#liq"
_INFLIGHT = ("sending", "sent", "sent_unconfirmed")
_ORIG_OPENED: dict[tuple[int, int, str], datetime | None] = {}


def is_aged_reason(reason: str) -> bool:
    return str(reason or "").split(":", 1)[0] == AGED_REASON_BASE


def order_key(lane: str, kind: str) -> str:
    """stop_orders row key: stops use the lane; aged exits '<lane>#aged'; the liquidation guard
    its own '<lane>#liq' row (2026-10-30) so a failed stop record can never block the guard."""
    if kind == AGED_KIND:
        return f"{lane}{AGED_SUFFIX}"
    if kind == LIQ_KIND:
        return f"{lane}{LIQ_SUFFIX}"
    return str(lane)


def lane_order_keys(lane: str) -> tuple[str, ...]:
    return (str(lane), f"{lane}{AGED_SUFFIX}", f"{lane}{LIQ_SUFFIX}")


def _key_kind(lane: str, key: str) -> str:
    if key.endswith(AGED_SUFFIX):
        return AGED_KIND
    if key.endswith(LIQ_SUFFIX):
        return LIQ_KIND
    return "stop"


def close_pending(state: Any, lane: str, position_id: int) -> bool:
    """True when a stop / aged-exit closing order for this lot is in flight or filled-unbooked.

    Lane take-profit paths check this so a lot never gets a second closing order.
    """
    st = get_store(state)
    if st is None:
        return False
    try:
        for key in lane_order_keys(lane):
            r = st.order_row(key, int(position_id))
            if r and r.get("status") in (*_INFLIGHT, "filled"):
                return True
    except Exception:  # noqa: BLE001
        log.exception("close_pending check failed")
    return False


def _parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def original_opened_at(led: Any, lot: Any) -> datetime | None:
    """Entry time of the ORIGINAL lot: follow roll_basis.from_position_id back through rolls."""
    key = (id(led), int(lot.id), str(getattr(lot, "opened_at", None)))
    if key in _ORIG_OPENED:
        return _ORIG_OPENED[key]
    ts = _parse_dt(getattr(lot, "opened_at", None))
    conn = getattr(led, "_conn", None)
    chain_ok = True
    if conn is not None:
        try:
            has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roll_basis'").fetchone()
            pid, seen = int(lot.id), {int(lot.id)}
            while has:
                r = conn.execute("SELECT from_position_id FROM roll_basis WHERE position_id=?", (pid,)).fetchone()
                if not r or r[0] is None or int(r[0]) in seen:
                    break
                pid = int(r[0]); seen.add(pid)
                r2 = conn.execute("SELECT opened_at FROM positions WHERE id=?", (pid,)).fetchone()
                t = _parse_dt(r2[0]) if r2 else None
                if t is not None and (ts is None or t < ts):
                    ts = t
        except Exception:  # noqa: BLE001
            chain_ok = False
            log.exception("original entry lookup failed", extra={"data": {"position_id": lot.id}})
    if chain_ok:
        _ORIG_OPENED[key] = ts
    return ts


def _aged_fees(state: Any, led: Any, lot: Any, exit_px: float) -> tuple[float, float]:
    from snowball.futures.net_green import estimate_side_fee_usd

    s = state.settings
    n = lot_contracts(lot.product, lot.qty)
    kw = dict(contract_size=_cs(lot.product), fee_pct_per_side=float(getattr(s, "futures_fee_pct_per_side", 0.0) or 0.0),
              fee_per_contract=float(getattr(s, "futures_fee_per_contract", 0.12) or 0.0))
    ent = _open_fee(led, lot.id)
    if ent is None or float(ent) <= 0:
        ent = estimate_side_fee_usd(float(lot.gate_entry_price), n, **kw)
    return float(ent), float(estimate_side_fee_usd(float(exit_px), n, **kw))


def aged_net_usd(state: Any, led: Any, lot: Any, exit_px: float) -> float:
    basis = float(lot.gate_entry_price)
    gross = ((basis - float(exit_px)) if str(lot.side).lower() == "short" else (float(exit_px) - basis)) * float(lot.qty)
    ent, ex = _aged_fees(state, led, lot, exit_px)
    return gross - ent - ex


def aged_net_ok(state: Any, led: Any, lot: Any, exit_px: float) -> tuple[bool, float]:
    net = aged_net_usd(state, led, lot, exit_px)
    floor = float(getattr(state.settings, "futures_aged_lot_min_net_usd", 0.50) or 0.0)
    return net >= floor - 1e-9, net


def _fresh_touch(market: Any, lot: Any) -> float | None | bool:
    """Worst touch from a fresh book. None = venue has no book call (fakes); False = unavailable."""
    fn = getattr(market, "fetch_bba", None)
    if not callable(fn):
        return None
    try:
        bid, ask = fn(lot.product)
    except Exception:  # noqa: BLE001
        log.exception("aged-lot exit fresh book failed")
        return False
    px = worst_touch(lot.side, bid, ask, None)
    return False if px is None else float(px)


def aged_view(state: Any, lane: str, led: Any, lot: Any, bid: float | None, ask: float | None,
              mark: float | None, now: datetime) -> dict:
    """Per-lot aged-exit fields for the snapshot/PDF (age, target price, move still needed)."""
    s = state.settings
    on = bool(getattr(s, "futures_aged_exit_enabled", True))
    days = float(getattr(s, "futures_aged_lot_days", 3.0) or 0.0)
    floor = float(getattr(s, "futures_aged_lot_min_net_usd", 0.50) or 0.0)
    t0 = original_opened_at(led, lot)
    age = None if t0 is None else max(0.0, (now - t0).total_seconds() / 86400.0)
    aged = bool(age is not None and days > 0 and age >= days)
    basis = float(lot.gate_entry_price)
    short = str(lot.side).lower() == "short"
    ent, ex = _aged_fees(state, led, lot, basis)
    qty = float(lot.qty) or 1.0
    target = basis - (floor + ent + ex) / qty if short else basis + (floor + ent + ex) / qty
    px = worst_touch(lot.side, bid, ask, mark)
    net_now = None if px is None else aged_net_usd(state, led, lot, px)
    move = None if px is None else ((px - target) if short else (target - px))
    armed = bool(on and aged)
    return {"original_opened_at": None if t0 is None else t0.isoformat(),
            "age_days": None if age is None else round(age, 2), "aged": aged,
            "aged_exit_armed": armed, "aged_min_net_usd": floor, "aged_target_px": round(target, 2),
            "aged_net_usd_now": None if net_now is None else round(net_now, 2),
            "aged_move_needed": None if move is None else round(max(0.0, move), 2),
            "aged_qualifies": bool(armed and net_now is not None and net_now >= floor - 1e-9),
            "aged_excluded": None if armed else ("disabled" if not on else "young")}



def stops_snapshot(state: Any) -> dict:
    s = state.settings
    st = get_store(state)
    now = datetime.now(timezone.utc)
    nk = netted_keys(state)
    opens = open_index_contracts(state)
    one_on = bool(getattr(s, "futures_one_index_contract", False))
    return {
        "enabled": bool(getattr(s, "futures_stops_enabled", True)),
        "rule": (f"stop at the closer of {float(getattr(s, 'futures_stop_pct', 0.08)) * 100:g}% of roll-adjusted basis "
                 f"({getattr(s, 'futures_stop_pct_families', 'US5,TEK')}) or "
                 f"${float(getattr(s, 'futures_stop_usd_per_contract', 250.0)):g}/contract loss net of fees; "
                 f"liq guard ${float(getattr(s, 'futures_liq_guard_usd', 50.0)):g}/contract; market close; bot-side"),
        "lanes": getattr(state, "futures_stop_views", {}) or {},
        "reentry_blocks": st.blocks(now) if st else [],
        "events": st.events(20) if st else [],
        "counters": dict(getattr(state, "stop_counters", {}) or {}),
        "netted": {"keys": sorted(f"{a}:{b}" for a, b in nk), "pairs": getattr(state, "netted_report", []),
                   "marked": st.netted_rows() if st else []},
        "reconcile": getattr(state, "futures_reconcile", None),
        "one_index_gate": {"enabled": one_on, "open_index_contracts": opens,
                           "reserved_for": str(getattr(s, "index_slot_reserved_for", "") or "") or None,
                           "reserved_lane": (parse_index_slot_reservation(state) or (None,))[0],
                           "reserved_families": sorted((parse_index_slot_reservation(state) or (None, set()))[1]),
                           "exchange_index_net": exchange_index_net(state),
                           "max_index_contracts": max_index_contracts(state),
                           "blocking_new_index_entries": bool(one_on and (
                               len(opens) >= max_index_contracts(state)
                               or _ex_net_blocks(exchange_index_net(state), max_index_contracts(state)))),
                           "note": "netted pairs count as 0; rolls are not entries"},
        "entry_gates": entry_gates_snapshot(state),
        "aged_exit": {"enabled": bool(getattr(s, "futures_aged_exit_enabled", True)),
                      "days": float(getattr(s, "futures_aged_lot_days", 3.0) or 0.0),
                      "min_net_usd": float(getattr(s, "futures_aged_lot_min_net_usd", 0.50) or 0.0),
                      "rule": (f"lots older than {float(getattr(s, 'futures_aged_lot_days', 3.0) or 0.0):g} days "
                               "(original entry, through rolls) close once net P&L after real round-trip fees >= "
                               f"${float(getattr(s, 'futures_aged_lot_min_net_usd', 0.50) or 0.0):.2f}; "
                               "never red; netted pairs excluded")},
        "spot_usd_floor": {"floor_usd": float(getattr(s, "spot_usd_floor", 0.0) or 0.0),
                           **(getattr(state, "spot_floor_stats", None) or {})},
    }


def entry_gates_snapshot(state: Any) -> dict:
    """Status of the 2026-10-10 entry gates (A opposite side, C margin; B lives in crash/fed)."""
    s = state.settings
    opens = []
    for ln, led in lane_ledgers(state).items():
        for p in _open_positions(led):
            try:
                from snowball.futures.market import is_cfm_product

                if is_cfm_product(_norm_pid(p.product)):
                    opens.append({"lane": ln, "id": int(p.id), "product": _norm_pid(p.product),
                                  "side": "short" if str(p.side).lower() == "short" else "long"})
            except Exception:  # noqa: BLE001
                continue
    blocked_sides = [{"product": o["product"], "blocked_side": "long" if o["side"] == "short" else "short",
                      "because": f"{o['lane']}#{o['id']}"} for o in opens]
    preview = []
    liq = _cached_liq(state)
    for pid in sorted({o["product"] for o in opens} | set(_families_products(state))):
        try:
            preview.append(margin_gate_eval(state, pid, "long", liq=liq))
        except Exception:  # noqa: BLE001
            continue
    return {
        "opposite_side_block": {"enabled": bool(getattr(s, "opposite_side_block", True)),
                                "auto_net_overlaps": bool(getattr(s, "futures_auto_net_overlaps", False)),
                                "blocked_new_sides": blocked_sides,
                                "rule": "no lane may open the opposite side of a CFM product any lane holds open"},
        "margin_gate": {"enabled": bool(getattr(s, "futures_margin_gate", True)),
                        "include_liq_guard": bool(getattr(s, "futures_margin_gate_include_liq_guard", True)),
                        "rule": ("live new entry needs CFM liquidation-buffer headroom per contract AFTER the entry "
                                 ">= stop distance" + (" + liq guard" if bool(getattr(
                                     s, "futures_margin_gate_include_liq_guard", True)) else "")),
                        "last": getattr(state, "margin_gate_last", None),
                        "preview_one_contract": preview},
        "crash": {"min_triggers": int(getattr(s, "crash_min_triggers", 2)),
                  "require_below_sma": bool(getattr(s, "crash_require_below_sma", True)),
                  "sma_days": int(getattr(s, "crash_trend_sma_days", 20)),
                  "cover_pct_net": float(getattr(s, "crash_cover_pct", 0.025) or 0.0)},
        "fed": {"min_odds_shift": float(getattr(s, "fed_min_odds_shift", 0.10) or 0.0),
                "require_trend": bool(getattr(s, "fed_require_trend", True)),
                "sma_days": int(getattr(s, "fed_trend_sma_days", 20)),
                "cover_pct_net": float(getattr(s, "fed_cover_pct", 0.025) or 0.0)},
        "counters": {k: v for k, v in (getattr(state, "stop_counters", {}) or {}).items()
                     if k in ("opposite_side_blocked", "margin_gate_blocked", "index_slot_reserved_blocked",
                              "one_index_blocked", "reentry_blocked")},
    }


def _families_products(state: Any) -> list[str]:
    raw = str(getattr(state.settings, "futures_products", "") or "")
    return [_norm_pid(x) for x in raw.split(",") if x.strip()]
