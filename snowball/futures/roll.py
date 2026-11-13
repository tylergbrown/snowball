"""Dated CFM contract catalog, front-month resolution and the contract-roll path.

2026-09-30 (Tb): every dated futures contract on every lane (futures, crash, fed,
stocks) is rolled near expiry instead of being cash-settled/force-closed.

* ``ContractCatalog`` lists the account's FUTURE products (Coinbase
  ``/brokerage/products?product_type=FUTURE``), groups them into families by the
  product-id root (``NOL-19OCT26-CDE`` -> ``NOL``) and resolves the FRONT tradable
  month for new entries: the first tradable month whose expiry is further away
  than ``FUTURES_EXPIRY_ENTRY_BLOCK_DAYS``.
* ``roll_decision`` is the pure decision for an OPEN lot.
* ``ContractRoller`` executes a roll: open the next month first when margin
  allows, else close then open immediately.  The roll is the ONLY path allowed to
  close a red lot (reason ``contract_roll``); ordinary exits, fades, HALT and kill
  paths keep refusing red exits exactly as before.
* Cost basis is carried forward: the new lot's ``roll_basis`` =
  old basis + (new open price - old close price).  Exit gates use
  ``Position.gate_entry_price`` (roll_basis when present) so NEVER_SELL_RED /
  never-cover-red still judge the rolled lot against what Tb actually paid.  The
  realized P/L of the closed leg is booked normally on the old lot (honest PDF)
  and also recorded in the ``contract_rolls`` table.

Spot BTC (the Bitcoin treasury) is never a dated contract and is never touched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from snowball.futures.market import (
    CFM_CONTRACT_SIZE,
    CFM_DISPLAY_NAMES,
    CFM_EXPIRY_UTC,
    CFM_OVERNIGHT_MARGIN_RATE,
    CFM_TICK_SIZE,
    PRODUCT_TO_CCXT,
    contract_size_for_product,
    contracts_from_qty,
    expiry_for_product,
    is_cfm_product,
    margin_rate_for_product,
    normalize_futures_product,
    parse_futures_order_fill,
)

log = logging.getLogger("snowball.futures.roll")

ROLL_REASON = "contract_roll"
# Expiries this far out are perp-style listings (BIP/ETP/SLP list 2089).
PERP_STYLE_AFTER_YEAR = 2080


def family_of(product: str) -> str:
    pid = normalize_futures_product(product)
    return pid.split("-", 1)[0] if pid.endswith("-CDE") else pid


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class ContractInfo:
    product_id: str
    family: str
    expiry: datetime | None
    tradable: bool
    contract_size: float | None = None
    tick: float | None = None
    price: float | None = None

    @property
    def dated(self) -> bool:
        return self.expiry is not None and self.expiry.year < PERP_STYLE_AFTER_YEAR


def parse_products_payload(payload: Any) -> list[ContractInfo]:
    out: list[ContractInfo] = []
    prods = payload.get("products") if isinstance(payload, dict) else payload
    for p in prods or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("product_id") or "").upper()
        if not pid.endswith("-CDE"):
            continue
        fd = p.get("future_product_details") or {}
        tradable = not (
            bool(p.get("trading_disabled"))
            or bool(p.get("is_disabled"))
            or bool(p.get("view_only"))
            or str(p.get("status") or "").lower() in ("offline", "delisted")
        )
        def _f(v: Any) -> float | None:
            try:
                x = float(v)
                return x if x > 0 else None
            except (TypeError, ValueError):
                return None
        out.append(
            ContractInfo(
                product_id=pid,
                family=pid.split("-", 1)[0],
                expiry=_parse_ts(fd.get("contract_expiry")),
                tradable=tradable,
                contract_size=_f(fd.get("contract_size")),
                tick=_f(p.get("quote_increment") or p.get("price_increment")),
                price=_f(p.get("price")),
            )
        )
    return out


def front_month(
    contracts: list[ContractInfo],
    family: str,
    now: datetime,
    block_days: float,
) -> ContractInfo | None:
    """First tradable month of ``family`` whose expiry is beyond the entry block."""
    fam = sorted(
        (c for c in contracts if c.family == family),
        key=lambda c: c.expiry or datetime.max.replace(tzinfo=timezone.utc),
    )
    horizon = float(block_days) * 86400.0
    for c in fam:
        if not c.tradable:
            continue
        if c.expiry is None or not c.dated:
            return c
        if (c.expiry - now).total_seconds() > horizon:
            return c
    return None


def next_month_after(
    contracts: list[ContractInfo], product: str, now: datetime, block_days: float
) -> ContractInfo | None:
    """Next tradable month after ``product`` (same family) not itself inside the block."""
    pid = normalize_futures_product(product)
    fam = family_of(pid)
    cur = next((c for c in contracts if c.product_id == pid), None)
    cur_exp = cur.expiry if cur else expiry_for_product(pid)
    cand = front_month(contracts, fam, now, block_days)
    if cand is None or cand.product_id == pid:
        return None
    if cur_exp is not None and cand.expiry is not None and cand.expiry <= cur_exp:
        return None
    return cand


def in_roll_window(product: str, now: datetime, block_days: float, expiry: datetime | None = None) -> bool:
    exp = expiry or expiry_for_product(product)
    if exp is None or exp.year >= PERP_STYLE_AFTER_YEAR:
        return False
    return (exp - now).total_seconds() <= float(block_days) * 86400.0


def carried_basis(old_basis: float, old_close_px: float, new_open_px: float) -> float:
    """New-month basis = original basis shifted by the roll spread (both sides)."""
    return float(old_basis) + (float(new_open_px) - float(old_close_px))


def roll_leg_pnl_usd(side: str, entry_px: float, close_px: float, qty_units: float, fee_usd: float = 0.0) -> float:
    if side == "short":
        return (float(entry_px) - float(close_px)) * float(qty_units) - float(fee_usd)
    return (float(close_px) - float(entry_px)) * float(qty_units) - float(fee_usd)


@dataclass
class RollDecision:
    action: str  # none | roll | blocked
    reason: str
    next_product: str | None = None


def roll_decision(
    *,
    product: str,
    now: datetime,
    block_days: float,
    auto_roll: bool,
    contracts: list[ContractInfo],
    halted: bool = False,
    can_trade: bool = True,
) -> RollDecision:
    """Pure: should an OPEN lot in ``product`` be rolled now?

    Deliberately does NOT look at P/L: a roll is allowed red (Tb 2026-09-30).
    """
    exp = None
    cur = next((c for c in contracts if c.product_id == normalize_futures_product(product)), None)
    if cur is not None:
        exp = cur.expiry
    if not in_roll_window(product, now, block_days, exp):
        return RollDecision("none", "outside_roll_window")
    if not auto_roll:
        return RollDecision("blocked", "auto_roll_disabled")
    if halted or not can_trade:
        return RollDecision("blocked", "halt_or_trading_disabled")
    nxt = next_month_after(contracts, product, now, block_days)
    if nxt is None:
        return RollDecision("blocked", "no_tradable_next_month")
    return RollDecision("roll", "expiry_window", nxt.product_id)


# --- catalog ----------------------------------------------------------------


class ContractCatalog:
    """Cached FUTURE product list (refresh hourly; one tiny REST call)."""

    def __init__(self, fetch: Callable[[], Any] | None, ttl_sec: float = 3600.0) -> None:
        self._fetch = fetch
        self._ttl = float(ttl_sec)
        self._lock = threading.Lock()
        self._contracts: list[ContractInfo] = []
        self._loaded_at = 0.0
        self._last_error: str | None = None

    @classmethod
    def from_exchange(cls, exchange: Any, ttl_sec: float = 3600.0) -> "ContractCatalog":
        def _fetch() -> Any:
            return exchange.v3PrivateGetBrokerageProducts(
                {"product_type": "FUTURE", "limit": 500}
            )

        return cls(_fetch, ttl_sec)

    @classmethod
    def static(cls, contracts: list[ContractInfo]) -> "ContractCatalog":
        cat = cls(None)
        cat._contracts = list(contracts)
        cat._loaded_at = float("inf")
        return cat

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def contracts(self) -> list[ContractInfo]:
        with self._lock:
            fresh = (time.monotonic() - self._loaded_at) < self._ttl
            if fresh or self._fetch is None:
                return list(self._contracts)
            try:
                items = parse_products_payload(self._fetch())
                if items:
                    self._contracts = items
                    register_contracts(items)
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 — never break a lane
                self._last_error = str(exc)[:200]
                log.warning("contract catalog refresh failed", extra={"data": {"error": self._last_error}})
            # back off even on failure so we never hammer the API
            self._loaded_at = time.monotonic()
            return list(self._contracts)

    def front_for(self, product: str, now: datetime, block_days: float) -> str:
        """Resolve a configured product to the front tradable month of its family.

        Perps / undated / unknown families resolve to themselves.  When no month
        qualifies (e.g. next month still view-only) the configured product is
        returned unchanged — the lane's own expiry entry block then refuses entries.
        """
        pid = normalize_futures_product(product)
        if not is_cfm_product(pid):
            return pid
        items = self.contracts()
        fam = family_of(pid)
        if not any(c.family == fam for c in items):
            return pid
        c = front_month(items, fam, now, block_days)
        return c.product_id if c is not None else pid


_SHARED: dict[int, ContractCatalog] = {}
_SHARED_LOCK = threading.Lock()


def shared_catalog(exchange: Any) -> ContractCatalog:
    """One catalog per process (all lanes share one hourly fetch)."""
    with _SHARED_LOCK:
        if _SHARED:
            return next(iter(_SHARED.values()))
        cat = ContractCatalog.from_exchange(exchange)
        _SHARED[0] = cat
        return cat


def register_contracts(items: list[ContractInfo], exchange: Any | None = None) -> None:
    """Teach the static tables about newly listed months (never overwrite verified rows)."""
    for c in items:
        pid = c.product_id
        if c.expiry is not None and c.dated and pid not in CFM_EXPIRY_UTC:
            CFM_EXPIRY_UTC[pid] = c.expiry.isoformat()
        if c.contract_size and pid not in CFM_CONTRACT_SIZE:
            CFM_CONTRACT_SIZE[pid] = float(c.contract_size)
        if c.tick and pid not in CFM_TICK_SIZE:
            CFM_TICK_SIZE[pid] = float(c.tick)
        fam_rates = next(
            (v for k, v in CFM_OVERNIGHT_MARGIN_RATE.items() if k.split("-", 1)[0] == c.family),
            None,
        )
        if fam_rates and pid not in CFM_OVERNIGHT_MARGIN_RATE:
            CFM_OVERNIGHT_MARGIN_RATE[pid] = fam_rates
        if pid not in PRODUCT_TO_CCXT:
            sibling = next(
                (v for k, v in PRODUCT_TO_CCXT.items() if k.split("-", 1)[0] == c.family),
                None,
            )
            if sibling and c.expiry is not None:
                base = sibling.rsplit("-", 1)[0]
                PRODUCT_TO_CCXT[pid] = f"{base}-{c.expiry.strftime('%y%m%d')}"
        if pid not in CFM_DISPLAY_NAMES:
            CFM_DISPLAY_NAMES[pid] = pid


# --- roll basis persistence (per lane DB) -----------------------------------

ROLL_SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_rolls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    lane TEXT NOT NULL,
    old_position_id INTEGER NOT NULL,
    new_position_id INTEGER,
    old_product TEXT NOT NULL,
    new_product TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    old_entry_price REAL NOT NULL,
    old_basis_price REAL NOT NULL,
    old_close_price REAL,
    new_open_price REAL,
    new_basis_price REAL,
    realized_roll_pnl_usd REAL,
    fees_usd REAL,
    status TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS roll_basis (
    position_id INTEGER PRIMARY KEY,
    basis_price REAL NOT NULL,
    from_position_id INTEGER,
    ts TEXT NOT NULL
);
"""


def ensure_roll_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ROLL_SCHEMA)
    conn.commit()


from snowball.roll_basis import attach_roll_basis  # noqa: E402,F401  (re-export)


# --- executor ---------------------------------------------------------------


class _Tmpl:
    """Minimal lot template for orphan bookings."""

    def __init__(self, st: dict[str, Any]) -> None:
        self.id = st.get("old_position_id")
        self.product = st.get("old_product")
        self.side = st.get("side")
        self.strategy = st.get("strategy") or ROLL_REASON
        self.extra = st.get("extra") or {}


@dataclass
class LegFill:
    price: float
    contracts: float
    fee_usd: float
    order_id: str | None = None


@dataclass
class RollLaneAdapter:
    """Lane-specific ledger hooks. ``open_lot`` returns the new Position."""

    lane: str
    conn: sqlite3.Connection
    lock: Any
    open_positions: Callable[[], list]
    open_lot: Callable[..., Any]
    close_lot: Callable[..., Any]
    get_meta: Callable[[str], str | None]
    set_meta: Callable[[str, str], None]
    live: bool = False
    strategy_for: Callable[[Any], str] = field(default=lambda lot: getattr(lot, "strategy", "roll"))


class ContractRoller:
    """Executes rolls for one lane. Status for API/snapshot in ``self.status``."""

    def __init__(
        self,
        adapter: RollLaneAdapter,
        market: Any,
        catalog: ContractCatalog,
        *,
        block_days: float,
        auto_roll: bool,
        leg_timeout_sec: float = 60.0,
        margin_headroom: float = 1.25,
        leg_attempts: int = 3,
        leg_retry_sleep_sec: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.leg_attempts = int(leg_attempts)
        self.leg_retry_sleep_sec = float(leg_retry_sleep_sec)
        self._sleep = sleep
        self.a = adapter
        self.market = market
        self.catalog = catalog
        self.block_days = float(block_days)
        self.auto_roll = bool(auto_roll)
        self.leg_timeout_sec = float(leg_timeout_sec)
        self.margin_headroom = float(margin_headroom)
        self.status: dict[str, dict[str, Any]] = {}
        self._last_alert: dict[str, float] = {}
        with self.a.lock:
            ensure_roll_schema(self.a.conn)

    # -- helpers --
    def _alert(self, key: str, msg: str, data: dict[str, Any], *, level: int = logging.ERROR, every_sec: float = 900.0) -> None:
        now_m = time.monotonic()
        if now_m - self._last_alert.get(key, -1e18) >= every_sec:
            self._last_alert[key] = now_m
            log.log(level, msg, extra={"data": {"lane": self.a.lane, **data}})

    def _state_key(self, lot_id: int) -> str:
        return f"contract_roll:{int(lot_id)}"

    def _load_state(self, lot_id: int) -> dict[str, Any]:
        raw = self.a.get_meta(self._state_key(lot_id))
        if not raw:
            return {}
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _save_state(self, lot_id: int, st: dict[str, Any]) -> None:
        self.a.set_meta(self._state_key(lot_id), json.dumps(st, separators=(",", ":")))
        ids = set(self._inflight_ids())
        if st.get("stage") == "done":
            ids.discard(int(lot_id))
        else:
            ids.add(int(lot_id))
        self.a.set_meta("contract_roll:inflight", json.dumps(sorted(ids)))

    def _inflight_ids(self) -> list[int]:
        raw = self.a.get_meta("contract_roll:inflight")
        try:
            v = json.loads(raw) if raw else []
            return [int(x) for x in v] if isinstance(v, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def in_flight(self, lot_id: int) -> bool:
        """Lanes skip ordinary exits for a lot whose roll is mid-flight."""
        return int(lot_id) in set(self._inflight_ids())

    def _resolve_orphans(self, open_ids: set[int], now: datetime) -> None:
        """An in-flight roll whose old lot is no longer open (closed elsewhere)."""
        for lot_id in self._inflight_ids():
            if lot_id in open_ids:
                continue
            st = self._load_state(lot_id)
            if not st:
                self._save_state(lot_id, {"stage": "done"})
                continue
            if st.get("new_fill") and not st.get("new_booked"):
                nf = st["new_fill"]
                tmpl = _Tmpl(st)
                qty_new = float(nf["contracts"]) * contract_size_for_product(st["new_product"])
                new_pos = self.a.open_lot(
                    template=tmpl, product=st["new_product"], side=st["side"],
                    fill_px=float(nf["price"]), qty_units=qty_new, fee_usd=float(nf["fee_usd"]),
                    reason=f"{ROLL_REASON}:from={lot_id}:orphan", ts=now,
                )
                with self.a.lock:
                    self.a.conn.execute(
                        "INSERT OR REPLACE INTO roll_basis (position_id, basis_price, from_position_id, ts) VALUES (?,?,?,?)",
                        (int(new_pos.id), float(st.get("old_basis") or nf["price"]), int(lot_id), now.isoformat()),
                    )
                    self.a.conn.commit()
                log.error("contract roll orphan: old lot closed elsewhere; booked new leg",
                          extra={"data": {"lane": self.a.lane, "old_position_id": lot_id, "new_position_id": new_pos.id}})
                st["new_booked"] = True
            st["stage"] = "done"
            self._save_state(lot_id, st)

    def _available_margin_usd(self) -> float | None:
        ex = getattr(self.market, "exchange", None)
        if ex is None:
            return None
        try:
            r = ex.v3PrivateGetBrokerageCfmBalanceSummary()
            bs = r.get("balance_summary") or {}
            val = bs.get("available_margin") or bs.get("futures_buying_power") or {}
            if isinstance(val, dict):
                val = val.get("value")
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    def _book(self, product: str) -> tuple[float | None, float | None]:
        try:
            return self.market.fetch_bba(product)
        except Exception:  # noqa: BLE001
            return None, None

    def _leg(self, product: str, side: str, contracts: int, *, reduce_only: bool) -> LegFill | None:
        for attempt in range(max(1, self.leg_attempts)):
            try:
                f = self._leg_once(product, side, contracts, reduce_only=reduce_only)
            except Exception as exc:  # noqa: BLE001
                log.error("contract roll leg error", extra={"data": {"lane": self.a.lane, "product": product,
                          "side": side, "attempt": attempt + 1, "error": str(exc)[:300]}})
                f = None
            if f is not None:
                return f
            if attempt + 1 < self.leg_attempts:
                self._sleep(self.leg_retry_sleep_sec)
        return None

    def _leg_once(self, product: str, side: str, contracts: int, *, reduce_only: bool) -> LegFill | None:
        bid, ask = self._book(product)
        if not self.a.live:
            px = ask if side == "buy" else bid
            px = px or bid or ask
            if not px:
                return None
            return LegFill(price=float(px), contracts=float(contracts), fee_usd=0.0, order_id="paper")
        far = ask if side == "buy" else bid
        if far is None or far <= 0:
            return None
        order = self.market.create_swap_maker_limit_order(
            product,
            side,
            float(contracts),
            price=float(far),
            bid=bid,
            ask=ask,
            leverage=1.0,
            reduce_only=reduce_only,
            timeout_sec=self.leg_timeout_sec,
            post_only=False,
        )
        px, qty, fee = parse_futures_order_fill(order)
        if px <= 0 or qty <= 0:
            return None
        return LegFill(price=float(px), contracts=float(qty), fee_usd=float(fee), order_id=str(order.get("id")))

    # -- main entry --
    def maybe_roll(self, now: datetime, *, halted: bool, can_trade: bool) -> dict[str, dict[str, Any]]:
        try:
            contracts = self.catalog.contracts()
        except Exception:  # noqa: BLE001
            contracts = []
        seen: set[str] = set()
        lots = list(self.a.open_positions())
        try:
            self._resolve_orphans({int(l.id) for l in lots}, now)
        except Exception:  # noqa: BLE001
            log.exception("contract roll orphan scan failed")
        excl = getattr(self, "exclude", None)
        for lot in lots:
            if not is_cfm_product(lot.product):
                continue
            if excl is not None and excl(lot):
                continue  # netted pair (2026-10-01): never place a closing/roll order for it
            key = f"{lot.product}#{lot.id}"
            st = self._load_state(lot.id)
            dec = roll_decision(
                product=lot.product,
                now=now,
                block_days=self.block_days,
                auto_roll=self.auto_roll,
                contracts=contracts,
                halted=halted,
                can_trade=can_trade,
            )
            if st and st.get("stage") not in (None, "done"):
                # an in-flight roll always resumes (never leave a leg hanging)
                dec = RollDecision("roll", "resume", st.get("new_product"))
            if dec.action == "none":
                continue
            seen.add(key)
            exp = expiry_for_product(lot.product)
            info = {
                "lane": self.a.lane,
                "position_id": lot.id,
                "product": lot.product,
                "side": lot.side,
                "expiry": exp.isoformat() if exp else None,
                "decision": dec.action,
                "reason": dec.reason,
                "next_product": dec.next_product,
                "ts": now.isoformat(),
            }
            if dec.action == "blocked":
                info["alert"] = True
                self._alert(
                    f"blocked:{key}",
                    "CONTRACT ROLL BLOCKED — position will settle/force-close at expiry unless resolved",
                    info,
                )
                self.status[key] = info
                continue
            try:
                result = self._execute(lot, dec.next_product or "", now, st)
            except Exception as exc:  # noqa: BLE001
                result = {"stage": "error", "error": str(exc)[:300], "alert": True}
                log.exception("contract roll failed", extra={"data": info})
            info.update(result)
            self.status[key] = info
        for k in list(self.status):
            if k not in seen and self.status[k].get("stage") in (None, "done", "error") and k.split("#")[0]:
                # keep completed/failed entries visible for a day
                ts = _parse_ts(self.status[k].get("ts"))
                if ts is None or (now - ts).total_seconds() > 86400:
                    self.status.pop(k, None)
        return self.status

    def _execute(self, lot: Any, new_product: str, now: datetime, st: dict[str, Any]) -> dict[str, Any]:
        new_product = normalize_futures_product(new_product or st.get("new_product") or "")
        if not new_product:
            return {"stage": "blocked", "alert": True, "error": "no next product"}
        contracts = max(1, contracts_from_qty(lot.product, float(lot.qty)))
        open_side = "buy" if lot.side == "long" else "sell"
        close_side = "sell" if lot.side == "long" else "buy"
        old_basis = float(getattr(lot, "roll_basis", None) or lot.entry_price)
        if not st:
            st = {
                "stage": "start",
                "old_position_id": lot.id,
                "old_product": lot.product,
                "new_product": new_product,
                "side": lot.side,
                "contracts": contracts,
                "old_basis": old_basis,
                "strategy": getattr(lot, "strategy", None),
                "started_at": now.isoformat(),
            }
            bid, ask = self._book(new_product)
            ref = (ask if open_side == "buy" else bid) or bid or ask or 0.0
            need = (
                float(ref)
                * contract_size_for_product(new_product)
                * contracts
                * margin_rate_for_product(new_product, lot.side, 0.10)
            )
            avail = self._available_margin_usd() if self.a.live else float("inf")
            st["open_first"] = bool(avail is not None and ref > 0 and avail >= need * self.margin_headroom)
            st["margin_need_usd"] = round(need, 2)
            st["margin_available_usd"] = None if avail in (None, float("inf")) else round(float(avail), 2)
            self._save_state(lot.id, st)
            log.warning("contract roll start", extra={"data": {"lane": self.a.lane, **st}})

        order = ["open", "close"] if st.get("open_first") else ["close", "open"]
        for leg in order:
            if leg == "open" and not st.get("new_fill"):
                f = self._leg(new_product, open_side, contracts, reduce_only=False)
                if f is None:
                    st["open_attempts"] = int(st.get("open_attempts", 0)) + 1
                    self._save_state(lot.id, st)
                    unhedged = bool(st.get("old_fill"))
                    self._alert(
                        f"open:{lot.id}",
                        "CONTRACT ROLL OPEN LEG FAILED — retrying"
                        + (" (old leg already closed: exposure currently FLAT)" if unhedged else ""),
                        {"position_id": lot.id, "new_product": new_product, "attempts": st["open_attempts"]},
                        every_sec=0.0,
                    )
                    return {"stage": "open_pending", "alert": True, "unhedged": unhedged}
                st["new_fill"] = f.__dict__
                self._save_state(lot.id, st)
            if leg == "close" and not st.get("old_fill"):
                f = self._leg(lot.product, close_side, contracts, reduce_only=True)
                if f is None:
                    st["close_attempts"] = int(st.get("close_attempts", 0)) + 1
                    self._save_state(lot.id, st)
                    self._alert(
                        f"close:{lot.id}",
                        "CONTRACT ROLL CLOSE LEG FAILED — retrying",
                        {"position_id": lot.id, "product": lot.product, "attempts": st["close_attempts"]},
                        every_sec=0.0,
                    )
                    return {"stage": "close_pending", "alert": True}
                st["old_fill"] = f.__dict__
                self._save_state(lot.id, st)
        return self._book_ledger(lot, st, now)

    def _book_ledger(self, lot: Any, st: dict[str, Any], now: datetime) -> dict[str, Any]:
        old_f = st["old_fill"]
        new_f = st["new_fill"]
        cs_old = contract_size_for_product(st["old_product"])
        cs_new = contract_size_for_product(st["new_product"])
        qty_old = float(old_f["contracts"]) * cs_old
        qty_new = float(new_f["contracts"]) * cs_new
        # 1) close old leg at its real price (realized roll P/L booked on the old lot)
        close_fill = self.a.close_lot(
            lot,
            fill_px=float(old_f["price"]),
            fee_usd=float(old_f["fee_usd"]),
            reason=f"{ROLL_REASON}:to={st['new_product']}",
            ts=now,
        )
        realized = roll_leg_pnl_usd(lot.side, float(lot.entry_price), float(old_f["price"]), qty_old, float(old_f["fee_usd"]))
        # 2) open new leg at its real price
        new_pos = self.a.open_lot(
            template=lot,
            product=st["new_product"],
            side=lot.side,
            fill_px=float(new_f["price"]),
            qty_units=qty_new,
            fee_usd=float(new_f["fee_usd"]),
            reason=f"{ROLL_REASON}:from={lot.id}",
            ts=now,
        )
        basis = carried_basis(float(st["old_basis"]), float(old_f["price"]), float(new_f["price"]))
        with self.a.lock:
            self.a.conn.execute(
                "INSERT OR REPLACE INTO roll_basis (position_id, basis_price, from_position_id, ts) VALUES (?,?,?,?)",
                (int(new_pos.id), basis, int(lot.id), now.isoformat()),
            )
            self.a.conn.execute(
                """INSERT INTO contract_rolls (ts, lane, old_position_id, new_position_id, old_product,
                   new_product, side, qty, old_entry_price, old_basis_price, old_close_price,
                   new_open_price, new_basis_price, realized_roll_pnl_usd, fees_usd, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now.isoformat(), self.a.lane, int(lot.id), int(new_pos.id), st["old_product"],
                    st["new_product"], lot.side, qty_new, float(lot.entry_price), float(st["old_basis"]),
                    float(old_f["price"]), float(new_f["price"]), basis, realized,
                    float(old_f["fee_usd"]) + float(new_f["fee_usd"]), "done",
                    json.dumps({"open_first": st.get("open_first"), "old_order": old_f.get("order_id"),
                                "new_order": new_f.get("order_id")}),
                ),
            )
            self.a.conn.commit()
        st["stage"] = "done"
        st["new_booked"] = True
        st["new_position_id"] = int(new_pos.id)
        st["new_basis"] = basis
        st["realized_roll_pnl_usd"] = realized
        self._save_state(lot.id, st)
        log.warning(
            "contract roll done",
            extra={"data": {"lane": self.a.lane, "old_position_id": lot.id, "new_position_id": new_pos.id,
                            "old_product": st["old_product"], "new_product": st["new_product"],
                            "old_close": old_f["price"], "new_open": new_f["price"],
                            "carried_basis": basis, "realized_roll_pnl_usd": round(realized, 4)}},
        )
        _ = close_fill
        return {"stage": "done", "new_position_id": int(new_pos.id), "carried_basis": basis,
                "realized_roll_pnl_usd": round(realized, 4), "alert": realized < 0}


def lane_products(configured: list[str], open_products: list[str], catalog: ContractCatalog | None,
                  now: datetime, block_days: float) -> list[str]:
    """Front-month-resolved configured products + any product with an open lot."""
    out: list[str] = []
    for p in configured:
        pid = normalize_futures_product(p)
        if catalog is not None and is_cfm_product(pid):
            try:
                pid = catalog.front_for(pid, now, block_days)
            except Exception:  # noqa: BLE001
                pass
        if pid not in out:
            out.append(pid)
    for p in open_products:
        if p not in out:
            out.append(p)
    return out


# --- lane adapters ----------------------------------------------------------


def _ensure_cash(store: Any, need: float, ts: datetime) -> None:
    """Live lanes keep a book-keeping cash figure; top it up like the entry paths do."""
    try:
        if store.cash_usd() + 1e-9 < need:
            store.set_cash_usd(need * 2, ts=ts)
    except Exception:  # noqa: BLE001
        log.exception("roll ledger cash sync failed")


def _row_extra(store: Any, position_id: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    try:
        with store._lock:
            have = {r[1] for r in store._conn.execute("PRAGMA table_info(positions)")}
            use = [c for c in cols if c in have]
            if not use or position_id is None:
                return {}
            row = store._conn.execute(
                f"SELECT {', '.join(use)} FROM positions WHERE id = ?", (int(position_id),)
            ).fetchone()
            return dict(zip(use, row)) if row else {}
    except Exception:  # noqa: BLE001
        return {}


def ledger_adapter(lane: str, store: Any, *, live: bool) -> RollLaneAdapter:
    """Build the roll adapter for a lane store (PaperLedger/FuturesLedger, CrashStore, FedStore)."""

    def open_positions() -> list:
        return list(store.open_positions())

    def open_lot(*, template: Any, product: str, side: str, fill_px: float, qty_units: float,
                 fee_usd: float, reason: str, ts: datetime) -> Any:
        notional = float(qty_units) * float(fill_px)
        _ensure_cash(store, notional + float(fee_usd), ts)
        strategy = getattr(template, "strategy", None) or ROLL_REASON
        if lane == "fed":
            extra = _row_extra(store, getattr(template, "id", None), ("meeting_date", "dominant"))
            extra.update({k: v for k, v in (getattr(template, "extra", {}) or {}).items() if v})
            fn = store.open_short if side == "short" else store.open_long
            pos, _ = fn(product=product, fill_px=fill_px, notional_usd=notional, slippage_bps=0.0,
                        fee_usd=fee_usd, reason=reason, ts=ts, strategy=strategy,
                        meeting_date=extra.get("meeting_date"), dominant=extra.get("dominant"),
                        direction=side)
            return pos
        if side == "short":
            pos, _ = store.open_short(product=product, fill_px=fill_px, notional_usd=notional,
                                      slippage_bps=0.0, fee_usd=fee_usd, reason=reason, ts=ts,
                                      strategy=strategy)
            return pos
        pos, _ = store.open_buy(product=product, fill_px=fill_px, notional_usd=notional,
                                slippage_bps=0.0, fee_usd=fee_usd, reason=reason, ts=ts,
                                strategy=strategy)
        return pos

    def close_lot(lot: Any, *, fill_px: float, fee_usd: float, reason: str, ts: datetime) -> Any:
        if lot.side == "short":
            return store.cover_short(lot.id, fill_px, 0.0, fee_usd, reason, ts)
        if lane == "fed":
            return store.close_long(lot.id, fill_px, 0.0, fee_usd, reason, ts)
        return store.close_position(position_id=lot.id, fill_px=fill_px, slippage_bps=0.0,
                                    fee_usd=fee_usd, reason=reason, ts=ts)

    return RollLaneAdapter(
        lane=lane,
        conn=store._conn,
        lock=store._lock,
        open_positions=open_positions,
        open_lot=open_lot,
        close_lot=close_lot,
        get_meta=store.get_meta,
        set_meta=store.set_meta,
        live=bool(live),
    )


class LaneRollSupport:
    """Mixin-style helper a lane engine owns: front-month products + roll executor."""

    def __init__(self, lane: str, state: Any, store_getter: Callable[[], Any], market: Any,
                 live_getter: Callable[[], bool]) -> None:
        self.lane = lane
        self.state = state
        self._store_getter = store_getter
        self.market = market
        self._live_getter = live_getter
        self._roller: ContractRoller | None = None
        self._catalog: ContractCatalog | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.state.settings, "futures_auto_roll", True))

    @property
    def block_days(self) -> float:
        return float(getattr(self.state.settings, "futures_expiry_entry_block_days", 3.0) or 3.0)

    def catalog(self) -> ContractCatalog | None:
        if self._catalog is None:
            ex = getattr(self.market, "exchange", None)
            if ex is None or not hasattr(ex, "v3PrivateGetBrokerageProducts"):
                return None
            self._catalog = shared_catalog(ex)
        return self._catalog

    def products(self, configured: list[str], now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        store = self._store_getter()
        open_products: list[str] = []
        if store is not None:
            try:
                open_products = [p.product for p in store.open_positions() if is_cfm_product(p.product)]
            except Exception:  # noqa: BLE001
                open_products = []
        cat = self.catalog() if self.enabled else None
        return lane_products(configured, open_products, cat, now, self.block_days)

    def tick(self, now: datetime, *, halted: bool, can_trade: bool) -> dict[str, dict[str, Any]]:
        store = self._store_getter()
        if store is None:
            return {}
        cat = self.catalog()
        if cat is None:
            return {}
        if self._roller is None:
            self._roller = ContractRoller(
                ledger_adapter(self.lane, store, live=self._live_getter()),
                self.market,
                cat,
                block_days=self.block_days,
                auto_roll=self.enabled,
            )
        if getattr(self._roller, "exclude", None) is None:
            from snowball.futures.stops import is_netted

            lane, st_ = self.lane, self.state
            self._roller.exclude = lambda lot: is_netted(st_, lane, lot.id)
        self._roller.a.live = bool(self._live_getter())
        self._roller.auto_roll = self.enabled
        self._roller.block_days = self.block_days
        status = self._roller.maybe_roll(now, halted=halted, can_trade=can_trade)
        bucket = getattr(self.state, "contract_roll_status", None)
        if isinstance(bucket, dict):
            bucket[self.lane] = {k: dict(v) for k, v in status.items()}
        return status

    def in_flight(self, lot_id: int) -> bool:
        return bool(self._roller is not None and self._roller.in_flight(lot_id))


def same_family_member(product: str, configured: list[str]) -> bool:
    fam = family_of(product)
    return any(family_of(p) == fam for p in configured)
