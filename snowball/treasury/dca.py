"""Daily profit dividend — Robo Trader → Treasury, 3:00 PM America/Chicago (was 2:00 PM).

Tb 9:39 AM CT 10/2: cutoff 2 PM -> 3 PM CT (DIVIDEND_CUTOFF_HOUR_CT=15), transition with no
gap / no double count: 10/1 window (9/30 2 PM, 10/1 2 PM] unchanged (paid $8.28); 10/2 window
(10/1 2 PM, 10/2 3 PM] = 25 h; then 3 PM -> 3 PM. Schedule: snowball.treasury.cutoff (the
"14:00" text below is history; every window now comes from cutoff.window()).

History: Task 11 (2026-09-30) renamed the flat $10 daily DCA to a "dividend";
Task 13 (2026-09-30, Tb) replaced the flat $10 AND the (never-automated) 50%
P/L sweep with one daily profit dividend; the amendment added a red-day floor.
Tb (2026-09-30 9:25 PM CT): red-day floor lowered $50 -> $10 from the 10/1 2 PM CT
dividend on (9/30's $49.99 floor buy stays as paid). The daily closed-profit
*minimum goal* (``DAILY_PROFIT_MIN_USD`` / ``DAILY_TARGET_USD``, display only) is a
separate value: Tb 2026-10-01 12:45 AM CT raised it $50 -> $100/day (config
DAILY_PROFIT_MIN_USD, default 100). It never changes the dividend rule or trading.

The bot does NOT schedule this; Tb's external routine runs, daily at the cutoff (15:00 CT)::

    docker exec snowball python /app/tools/treasury_dca.py --execute

Rule for CT date D
* Window: (end(D-1), end(D)] — ending at the cutoff on D (3 PM CT from 10/2; 2 PM CT
  before; the 10/2 window is 25 h), independent of when the routine actually runs
  (closes after the cutoff belong to the next window).
* Net = sum of ``realized_pnl`` of **live** positions whose ``closed_at`` is in
  the window, across every lane DB (crypto, stocks archive, futures, crash,
  fed). Live/paper classification is the same as the PDF closed-P/L headline
  (``snowball.reports.closed_pnl``). Unrealized P/L never counts.
* Green (net > 0): buy 100% of net (+ any positive carry rolled from earlier
  days), capped at free USD. Kind ``dividend``.
  - amount below Coinbase's minimum order -> no buy, amount rolls forward as
    carry to the next green day (day consumed, status ``rolled_below_min``).
* Red / flat (net <= 0): buy a flat $10 floor from trading cash. Kind
  ``red_day_floor``. Losses are never taken back out of the treasury and are
  never netted against later days (each window stands alone).
* Free USD below what the rule needs (green: the Coinbase minimum; red: $10)
  -> skip with a logged reason; the day is NOT consumed (a later same-day run
  may buy; the window is fixed so it computes the same amount).
* Idempotent per CT date via meta claim ``daily_dca:<YYYY-MM-DD>`` (key name
  kept from the original DCA); any dividend-kind contribution already noted
  with ``ct_date=D`` also counts as done.
* Catch-up (Task 17, ``--execute --catch-up``): every unpaid CT date from
  2026-09-30 whose cutoff has passed is paid oldest first, each with its own
  window and rule, the same per-date claim/in-doubt guard and the
  free-USD cap. Low cash stops the run and logs the remaining unpaid dates.
  Without ``--catch-up`` only today's date is attempted (unchanged).
* A claim left ``claimed`` (crash mid-order) -> ``in_doubt``; never auto-retried.
* Receive-only: only BUYS BTC-USD spot. Never sells.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone, date as _date
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from snowball.treasury import cutoff as _cut
from snowball.treasury.ledger import ensure_schema, record_contribution

CT = ZoneInfo("America/Chicago")

KIND = "dividend"  # green-day profit dividend
KIND_RED = "red_day_floor"  # red / flat day flat floor buy
LEGACY_KIND = "daily_dca"  # flat $10 rows (before Task 13)
DIVIDEND_KINDS = (KIND, KIND_RED, LEGACY_KIND)
# Meta claim-key prefix: unchanged so the once-per-CT-date guard stays continuous.
CLAIM_PREFIX = "daily_dca"
CARRY_KEY = "dividend_carry"
# Red/flat-day floor (Tb 2026-09-30 9:25 PM CT: $50 -> $10, effective 10/1 2 PM CT).
# Independent of DAILY_TARGET_USD below.
RED_DAY_FLOOR_USD = 10.0
DEFAULT_USD = RED_DAY_FLOOR_USD  # tool --usd = red-day floor
DEFAULT_MIN_ORDER_USD = 1.0  # Coinbase BTC-USD quote_min_size fallback
# None = the cutoff schedule (snowball.treasury.cutoff): 14:00 before the effective date, then
# DIVIDEND_CUTOFF_HOUR_CT. An explicit dtime = that fixed hour for every date (legacy/tests).
DEFAULT_NOT_BEFORE: dtime | None = None


def _daily_profit_min_usd() -> float:
    """Config DAILY_PROFIT_MIN_USD (env), default $100; bad / non-positive values -> 100."""
    import os

    try:
        v = float(os.environ.get("DAILY_PROFIT_MIN_USD", "100") or 100)
    except ValueError:
        return 100.0
    return v if v > 0 else 100.0


# Daily closed-profit minimum the bots try to clear (display / goal only; rules unchanged).
# Tb 2026-10-01 12:45 AM CT: $50 -> $100. NOT the dividend floor (that is RED_DAY_FLOOR_USD).
DAILY_PROFIT_MIN_USD = _daily_profit_min_usd()
DAILY_TARGET_USD = DAILY_PROFIT_MIN_USD  # older name kept for callers
LABEL = f"Daily profit dividend \u2014 Robo Trader \u2192 Treasury ({_cut.current_label()} CT)"


def rule_text(floor_usd: float = RED_DAY_FLOOR_USD, window_label: str | None = None) -> str:
    f = float(floor_usd)
    amt = f"${f:,.0f}" if f == int(f) else f"${f:,.2f}"
    wl = window_label or f"{_cut.current_label()}\u2192{_cut.current_label()} CT"
    return (f"100% of net closed live profit, {wl} (all lanes); "
            f"red/flat day: flat {amt} floor (skipped if free cash < {amt}); never sells")


RULE_TEXT = rule_text(RED_DAY_FLOOR_USD)

KIND_LABELS = {
    "dividend": "Profit dividend",
    "red_day_floor": "Red-day floor",
    "dip_buy": "Dip buy (BTC -5% / 60 min)",
    "daily_dca": "Dividend (flat $10)",
    "pnl_sweep": "P/L sweep (retired)",
    "friday_50": "Weekly $50 (legacy)",
    "manual": "Manual",
    "seed": "Seed",
}
KIND_TAGS = {"dividend": "green", "red_day_floor": "red", "daily_dca": "flat", "dip_buy": "flat"}

log = logging.getLogger("snowball.treasury.dca")


def kind_label(kind: str | None) -> str:
    k = str(kind or "")
    return KIND_LABELS.get(k) or (k.replace("_", " ").title() if k else "\u2014")


@dataclass
class DcaResult:
    status: str  # bought | already_done | in_doubt | rolled_below_min | skipped_insufficient_cash | too_early | dry_run | error
    ct_date: str
    usd: float
    detail: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"status": self.status, "ct_date": self.ct_date, "usd": self.usd, **self.detail},
                          default=str)


def ct_date_of(now: datetime) -> str:
    return now.astimezone(CT).date().isoformat()


def window_for(ct_date: str | _date, not_before: dtime | None = DEFAULT_NOT_BEFORE) -> tuple[datetime, datetime]:
    """(start, end] of the dividend window for CT date *ct_date* (tz-aware CT).

    start = end of the previous date's window, so the 2 PM -> 3 PM switch has no gap and no
    overlap (snowball.treasury.cutoff)."""
    return _cut.window(ct_date, not_before)


def net_closed_in_window(data_dir: Path, start: datetime, end: datetime) -> dict[str, Any]:
    """Live closed realized P/L with closed_at in (start, end], every lane."""
    from snowball.reports.closed_pnl import LANE_DB_FILES, _closed_rows, parse_closed_at

    by_lane: dict[str, float] = {}
    n = 0
    paper = 0.0
    wins = losses = 0
    for lane, fn in LANE_DB_FILES:
        s = 0.0
        for closed_at, pnl, live in _closed_rows(Path(data_dir) / fn):
            t = parse_closed_at(closed_at)
            if t is None or not (start < t <= end):
                continue
            if not live:
                paper += pnl
                continue
            s += pnl
            n += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        by_lane[lane] = round(s, 4)
    net = round(sum(by_lane.values()), 4)
    return {"net_usd": net, "by_lane": by_lane, "closes": n, "wins": wins, "losses": losses,
            "paper_excluded_usd": round(paper, 4),
            "window_start": start.astimezone(CT).isoformat(), "window_end": end.astimezone(CT).isoformat()}


def plan_amount(net: float, carry: float, *, floor_usd: float = RED_DAY_FLOOR_USD,
                min_order_usd: float = DEFAULT_MIN_ORDER_USD) -> dict[str, Any]:
    """Which rule applies and how much (before the free-USD cap)."""
    if net > 0:
        amount = round(net + max(0.0, carry), 2)
        if amount + 1e-9 < min_order_usd:
            return {"rule": "profit", "kind": KIND, "amount_usd": amount, "below_min": True}
        return {"rule": "profit", "kind": KIND, "amount_usd": amount, "below_min": False}
    return {"rule": "red_day_floor", "kind": KIND_RED, "amount_usd": round(float(floor_usd), 2), "below_min": False}


def _key(ct_date: str) -> str:
    return f"{CLAIM_PREFIX}:{ct_date}"


def _get(con: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        v = json.loads(row[0])
        return v if isinstance(v, dict) else {"state": str(row[0])}
    except json.JSONDecodeError:
        return {"state": str(row[0])}


def _recorded_for_date(con: sqlite3.Connection, ct_date: str) -> dict[str, Any] | None:
    """Secondary guard: a dividend-kind contribution already recorded for this CT date."""
    try:
        row = con.execute(
            f"SELECT id, kind, ts FROM contributions WHERE kind IN ({','.join('?' * len(DIVIDEND_KINDS))})"
            " AND note LIKE ? ORDER BY id LIMIT 1",
            (*DIVIDEND_KINDS, f"ct_date={ct_date};%"),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"state": "done", "contribution_id": int(row[0]), "kind": row[1], "at": row[2],
            "source": "contributions"}


def _carry(con: sqlite3.Connection) -> dict[str, Any]:
    v = _get(con, CARRY_KEY) or {}
    try:
        v["usd"] = max(0.0, float(v.get("usd") or 0.0))
    except (TypeError, ValueError):
        v["usd"] = 0.0
    return v


def _claim(db: Path, ct_date: str, now: datetime, final: dict[str, Any] | None = None,
           carry: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any] | None]:
    """Atomically claim the day (state claimed, or *final* directly). Optionally set carry."""
    con = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = _get(con, _key(ct_date)) or _recorded_for_date(con, ct_date)
        if existing is not None:
            con.execute("ROLLBACK")
            return False, existing
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                    (_key(ct_date), json.dumps(final or {"state": "claimed", "at": now.isoformat()}, default=str)))
        if carry is not None:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (CARRY_KEY, json.dumps(carry, default=str)))
        con.execute("COMMIT")
        return True, None
    finally:
        con.close()


def _set(db: Path, ct_date: str, value: dict[str, Any] | None, carry: dict[str, Any] | None = None) -> None:
    con = sqlite3.connect(str(db), timeout=30)
    try:
        if value is None:
            con.execute("DELETE FROM meta WHERE key = ?", (_key(ct_date),))
        else:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (_key(ct_date), json.dumps(value, default=str)))
        if carry is not None:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (CARRY_KEY, json.dumps(carry, default=str)))
        con.commit()
    finally:
        con.close()


def _log_skip(db: Path, ct_date: str, reason: str, detail: dict[str, Any]) -> None:
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (f"{CLAIM_PREFIX}_last_skip", json.dumps({"ct_date": ct_date, **detail, "code": reason}, default=str)))
        con.commit()
    finally:
        con.close()


def run_daily_dca(
    db: Path,
    *,
    data_dir: Path | None = None,
    usd: float = RED_DAY_FLOOR_USD,
    now: datetime | None = None,
    execute: bool = False,
    any_time: bool = False,
    not_before: dtime | None = DEFAULT_NOT_BEFORE,
    free_usd: Callable[[], float],
    buy: Callable[[float], tuple[float, float, float, str | None]] | None = None,
    min_order_usd: float | Callable[[], float] = DEFAULT_MIN_ORDER_USD,
    net_closed: Callable[[datetime, datetime], dict[str, Any]] | None = None,
    ct_date: str | None = None,
    catch_up: bool = False,
) -> DcaResult:
    """One idempotent daily profit-dividend attempt for today's CT date.

    ``ct_date`` (catch-up only) attempts that earlier CT date instead; it is
    only allowed once its own cutoff (3 PM CT; 2 PM before the switch) has passed and uses its own window.

    ``usd`` is the red-day floor ($10). ``buy(usd)`` places the spot buy and
    returns (btc_qty, avg_price, fee_usd, order_id). ``net_closed(start, end)``
    overrides the lane-DB P/L read (tests).
    """
    now = now or datetime.now(timezone.utc)
    ct_now = now.astimezone(CT)
    d = ct_now.date().isoformat() if ct_date is None else _date.fromisoformat(ct_date).isoformat()
    floor = float(usd)
    if floor <= 0:
        return DcaResult("error", d, floor, {"reason": "red-day floor usd must be positive"})
    ensure_schema(db)
    con = sqlite3.connect(str(db), timeout=30)
    try:
        existing = _get(con, _key(d)) or _recorded_for_date(con, d)
        carry = _carry(con)
    finally:
        con.close()
    if existing is not None:
        st = "already_done" if existing.get("state") == "done" else "in_doubt"
        return DcaResult(st, d, 0.0, {"existing": existing})
    d_end = window_for(d, not_before)[1]
    if ct_date is not None:
        if ct_now < d_end:
            return DcaResult("too_early", d, 0.0, {"ct_time": ct_now.strftime("%Y-%m-%d %H:%M"),
                                                  "not_before": f"{d} {d_end.strftime('%H:%M')}"})
    elif not any_time and ct_now < d_end:
        return DcaResult("too_early", d, 0.0, {"ct_time": ct_now.strftime("%H:%M"),
                                              "not_before": d_end.strftime("%H:%M")})
    start, end = window_for(d, not_before)
    try:
        if net_closed is not None:
            pnl = net_closed(start, end)
        else:
            pnl = net_closed_in_window(Path(data_dir) if data_dir else db.parent, start, end)
        net = float(pnl["net_usd"])
    except Exception as exc:  # noqa: BLE001
        return DcaResult("error", d, 0.0, {"reason": f"net closed P/L read failed: {exc}"[:300]})
    try:
        min_usd = float(min_order_usd() if callable(min_order_usd) else min_order_usd)
    except Exception:  # noqa: BLE001
        min_usd = DEFAULT_MIN_ORDER_USD
    min_usd = max(min_usd, 0.01)
    plan = plan_amount(net, carry["usd"], floor_usd=floor, min_order_usd=min_usd)
    info = {"rule": plan["rule"], "kind": plan["kind"], "net_closed_usd": net, "carry_in_usd": carry["usd"],
            "window_start": pnl.get("window_start"), "window_end": pnl.get("window_end"),
            "closes": pnl.get("closes"), "by_lane": pnl.get("by_lane"), "min_order_usd": min_usd}
    if catch_up:
        info["catch_up"] = True
    if plan["below_min"]:
        new_carry = {"usd": plan["amount_usd"], "since": carry.get("since") or d, "updated": d}
        if not execute:
            return DcaResult("dry_run", d, 0.0, {**info, "would": "roll_below_min", "carry_out_usd": plan["amount_usd"]})
        ok, existing = _claim(db, d, now, final={"state": "done", "outcome": "rolled_below_min", "at": now.isoformat(),
                                                  **info, "carry_out_usd": plan["amount_usd"]}, carry=new_carry)
        if not ok:
            st = "already_done" if (existing or {}).get("state") == "done" else "in_doubt"
            return DcaResult(st, d, 0.0, {"existing": existing})
        log.warning("treasury profit dividend rolled (below Coinbase minimum)", extra={"data": {"ct_date": d, **info}})
        return DcaResult("rolled_below_min", d, 0.0, {**info, "carry_out_usd": plan["amount_usd"]})
    target = plan["amount_usd"]
    try:
        cash = float(free_usd())
    except Exception as exc:  # noqa: BLE001
        return DcaResult("error", d, target, {**info, "reason": f"free USD lookup failed: {exc}"[:300]})
    if plan["rule"] == "profit":
        spend = round(min(target, cash), 2)
        need = min_usd
    else:
        spend = target
        need = target
    if cash + 1e-9 < need or spend + 1e-9 < min_usd:
        detail = {**info, "reason": f"free USD ${cash:.2f} < ${need:.2f} needed ({plan['rule']})",
                  "free_usd": round(cash, 2), "target_usd": target}
        log.warning("treasury dividend skipped: insufficient free USD", extra={"data": {"ct_date": d, **detail}})
        _log_skip(db, d, "insufficient_cash", detail)
        return DcaResult("skipped_insufficient_cash", d, target, detail)
    capped = spend + 1e-9 < target
    info.update({"target_usd": target, "spend_usd": spend, "capped_at_free_usd": capped, "free_usd": round(cash, 2)})
    if not execute:
        return DcaResult("dry_run", d, spend, {**info, "would_buy_usd": spend})
    if buy is None:
        return DcaResult("error", d, spend, {"reason": "no buy function"})
    claimed, existing = _claim(db, d, now)
    if not claimed:
        st = "already_done" if (existing or {}).get("state") == "done" else "in_doubt"
        return DcaResult(st, d, spend, {"existing": existing})
    try:
        btc, px, fee, order_id = buy(spend)
    except Exception as exc:  # noqa: BLE001
        _set(db, d, {"state": "in_doubt", "at": now.isoformat(), "error": str(exc)[:300], **info})
        log.error("treasury dividend order error (claim kept; check Coinbase)",
                  extra={"data": {"ct_date": d, "error": str(exc)[:300]}})
        return DcaResult("in_doubt", d, spend, {"error": str(exc)[:300], **info})
    if btc <= 0 or px <= 0:
        _set(db, d, None)
        _log_skip(db, d, "unfilled", {"order_id": order_id})
        return DcaResult("error", d, spend, {"reason": "order not filled", "order_id": order_id})
    cost = btc * px + fee
    note = (f"ct_date={d}; rule={plan['rule']}; net_closed={net:.4f}; carry_in={carry['usd']:.2f}; "
            f"window={info['window_start']}..{info['window_end']}; order={order_id}; fee_usd={fee:.4f}; "
            f"target_usd={target:.2f}; spend_usd={spend:.2f}; capped={int(capped)}"
            + ("; catch_up=1" if catch_up else ""))
    c = record_contribution(db, usd_amount=cost, btc_qty=btc, btc_price=px, kind=plan["kind"], ts=now, note=note)
    new_carry = {"usd": 0.0, "cleared": d} if plan["rule"] == "profit" else None
    _set(db, d, {"state": "done", "outcome": "bought", "at": now.isoformat(), "contribution_id": c.id,
                 "order_id": order_id, "btc": btc, "price": px, "usd": cost, **info}, carry=new_carry)
    log.warning("treasury dividend bought (Robo Trader -> Treasury)",
                extra={"data": {"ct_date": d, "rule": plan["rule"], "btc": btc, "price": px, "usd": cost,
                                "order_id": order_id}})
    return DcaResult("bought", d, spend, {**info, "contribution_id": c.id, "btc": btc, "price": px,
                                          "usd_cost": round(cost, 4), "fee_usd": fee, "order_id": order_id})


# ---------------------------------------------------------------- catch-up (Task 17)

CATCH_UP_SINCE = "2026-09-30"  # first CT date under the Task 13 dividend rule that catch-up may pay
CATCHUP_META = f"{CLAIM_PREFIX}_catchup_last"


def due_dates(now: datetime, *, since: str = CATCH_UP_SINCE, not_before: dtime | None = DEFAULT_NOT_BEFORE) -> list[str]:
    """CT dates from *since* (never before CATCH_UP_SINCE) whose cutoff has passed, oldest first."""
    ct_now = now.astimezone(CT)
    d = max(_date.fromisoformat(since), _date.fromisoformat(CATCH_UP_SINCE))
    out: list[str] = []
    while d <= ct_now.date():
        if ct_now >= _cut.day_end(d, not_before):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def date_states(db: Path, dates: list[str]) -> dict[str, dict[str, Any]]:
    """Per CT date: {'state': 'done'|'in_doubt'|'unpaid', ...} from the claim/contribution guard."""
    out: dict[str, dict[str, Any]] = {d: {"state": "unpaid"} for d in dates}
    if not Path(db).exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for d in dates:
            ex = _get(con, _key(d)) or _recorded_for_date(con, d)
            if ex is None:
                continue
            st = "done" if ex.get("state") == "done" else "in_doubt"
            out[d] = {**ex, "state": st}
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def run_catch_up(
    db: Path,
    *,
    data_dir: Path | None = None,
    usd: float = RED_DAY_FLOOR_USD,
    now: datetime | None = None,
    execute: bool = False,
    since: str = CATCH_UP_SINCE,
    not_before: dtime | None = DEFAULT_NOT_BEFORE,
    free_usd: Callable[[], float],
    buy: Callable[[float], tuple[float, float, float, str | None]] | None = None,
    min_order_usd: float | Callable[[], float] = DEFAULT_MIN_ORDER_USD,
    net_closed: Callable[[datetime, datetime], dict[str, Any]] | None = None,
) -> DcaResult:
    """Pay every unpaid due CT date oldest first (each date: own window, rule, claim, cash cap).

    * done dates are skipped; ``in_doubt`` dates are never retried (reported, run continues);
    * insufficient free USD, an order error / new in-doubt, or any error stops the run and
      the remaining unpaid dates are logged (meta ``daily_dca_catchup_last`` + log warning);
    * dry run projects free USD down by each would-be buy so later dates see realistic cash.
    """
    now = now or datetime.now(timezone.utc)
    ensure_schema(db)
    try:
        min_usd = float(min_order_usd() if callable(min_order_usd) else min_order_usd)
    except Exception:  # noqa: BLE001
        min_usd = DEFAULT_MIN_ORDER_USD
    dates = due_dates(now, since=since, not_before=not_before)
    before = date_states(db, dates)
    unpaid = [d for d in dates if before[d]["state"] == "unpaid"]
    results: list[dict[str, Any]] = []
    projected = {"spent": 0.0}

    def _cash() -> float:
        c = float(free_usd())
        return c - projected["spent"] if not execute else c

    stop: dict[str, Any] | None = None
    for d in dates:
        st = before[d]["state"]
        if st != "unpaid":
            results.append({"ct_date": d, "status": "already_done" if st == "done" else "in_doubt",
                            "existing": {k: before[d].get(k) for k in ("outcome", "contribution_id", "usd", "at", "error")
                                         if before[d].get(k) is not None}})
            continue
        res = run_daily_dca(db, data_dir=data_dir, usd=usd, now=now, execute=execute, not_before=not_before,
                            free_usd=_cash, buy=buy, min_order_usd=min_usd, net_closed=net_closed,
                            ct_date=d, catch_up=True)
        item = {"ct_date": d, "status": res.status, "usd": res.usd, **res.detail}
        results.append(item)
        if res.status == "dry_run":
            projected["spent"] += float(res.detail.get("would_buy_usd") or 0.0)
            continue
        if res.status in ("bought", "rolled_below_min", "already_done"):
            continue
        if res.status == "in_doubt" and "existing" in res.detail:
            continue  # claimed by a concurrent run: never retried, keep going
        stop = {"at_ct_date": d, "status": res.status,
                "reason": res.detail.get("reason") or res.detail.get("error") or res.status}
        break
    after = date_states(db, dates) if execute else before
    remaining = [d for d in dates if after[d]["state"] == "unpaid"] if execute else         [r["ct_date"] for r in results if r["status"] not in ("dry_run", "already_done", "in_doubt")]
    paid = [r for r in results if r["status"] in ("bought", "rolled_below_min")]
    if not dates or not unpaid:
        status = "nothing_due"
    elif not execute:
        status = "dry_run"
    elif stop is None:
        status = "caught_up"
    elif stop["status"] == "skipped_insufficient_cash":
        status = "stopped_insufficient_cash"
    else:
        status = "stopped_error"
    summary = {"status": status, "at": now.isoformat(), "due_dates": dates, "unpaid_before": unpaid,
               "paid": [{k: r.get(k) for k in ("ct_date", "status", "rule", "usd_cost", "btc", "price", "order_id",
                                               "contribution_id", "net_closed_usd", "capped_at_free_usd")} for r in paid],
               "remaining_unpaid": remaining, "stopped": stop,
               "in_doubt": [d for d in dates if (after if execute else before)[d]["state"] == "in_doubt"]}
    if execute and unpaid:
        con = sqlite3.connect(str(db), timeout=30)
        try:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (CATCHUP_META, json.dumps(summary, default=str)))
            con.commit()
        finally:
            con.close()
    if stop is not None and execute:
        log.warning("treasury dividend catch-up stopped; unpaid dates remain",
                    extra={"data": {"stopped": stop, "remaining_unpaid": remaining}})
    elif paid:
        log.warning("treasury dividend catch-up paid missed dates",
                    extra={"data": {"paid": [p["ct_date"] for p in paid]}})
    total = round(sum(float(r.get("usd_cost") or r.get("would_buy_usd") or 0.0) for r in results
                      if r["status"] in ("bought", "dry_run")), 4)
    return DcaResult(status, now.astimezone(CT).date().isoformat(), total,
                     {"catch_up": True, "results": results, **{k: v for k, v in summary.items() if k != "status"},
                      "min_order_usd": min_usd})


# ---------------------------------------------------------------- read-only status


def target_progress(net: float | None, target: float = DAILY_TARGET_USD) -> dict[str, Any]:
    """Closed profit vs the $100/day minimum: "$32 / $100", bar, beat (>= minimum), overage."""
    if net is None:
        return {"target_usd": target, "net_usd": None, "pct": None, "bar": 0.0, "beat": None, "text": "\u2014",
                "overage_usd": None, "short_usd": None}
    n = float(net)
    pct = 100.0 * n / target if target > 0 else None
    return {"target_usd": target, "net_usd": round(n, 2), "pct": None if pct is None else round(pct, 1),
            "bar": max(0.0, min(1.0, n / target)) if target > 0 else 0.0,
            "beat": n >= target, "text": f"${n:,.2f} / ${target:,.0f}",
            "overage_usd": round(n - target, 2) if n >= target else 0.0,
            "short_usd": round(target - n, 2) if n < target else 0.0}


def funded_from(kind: str | None) -> str:
    return {"dividend": "closed profit", "red_day_floor": "free cash (red-day floor)",
            "daily_dca": "free cash (flat $10)"}.get(str(kind or ""), "\u2014")


def _parse_note(note: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(note or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def dividend_status(db: Path, data_dir: Path | None = None, *, now: datetime | None = None,
                    free_usd: float | None = None, recent: int = 7,
                    not_before: dtime | None = DEFAULT_NOT_BEFORE, floor_usd: float = RED_DAY_FLOOR_USD,
                    min_order_usd: float = DEFAULT_MIN_ORDER_USD, year_reset: str | None = None) -> dict[str, Any]:
    """Everything the snapshot / PDF / plugin show about the daily dividend (read-only)."""
    now = now or datetime.now(timezone.utc)
    ct_now = now.astimezone(CT)
    today = ct_now.date().isoformat()
    data_dir = Path(data_dir) if data_dir else Path(db).parent
    out: dict[str, Any] = {
        "label": LABEL, "rule_text": rule_text(floor_usd), "time_ct": _cut.day_end(ct_now.date(), not_before).strftime("%H:%M"),
        "red_day_floor_usd": floor_usd, "daily_target_usd": DAILY_TARGET_USD, "daily_profit_min_usd": DAILY_PROFIT_MIN_USD, "min_order_usd": min_order_usd, "as_of_ct": ct_now.isoformat(),
        "today": {"ct_date": today, "state": "pending"}, "last_skip": None, "carry_usd": 0.0,
        "last": None, "recent": [], "totals": {}, "week": {}, "month": {},
        "treasury_btc": 0.0, "dividend_btc_share_pct": None, "pnl_sweep": {"count": 0, "usd": 0.0, "btc": 0.0,
                                                                           "status": "retired (replaced by daily profit dividend)"},
        "unpaid": [], "in_doubt_dates": [], "caught_up": [], "catch_up_last": None,
    }
    rows: list[tuple] = []
    today_state = None
    if Path(db).exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            today_state = _get(con, _key(today)) or _recorded_for_date(con, today)
            skip = _get(con, f"{CLAIM_PREFIX}_last_skip")
            out["last_skip"] = skip
            out["carry_usd"] = _carry(con)["usd"]
            rows = con.execute("SELECT id, ts, kind, usd_amount, btc_qty, btc_price, note FROM contributions ORDER BY id").fetchall()
            out["catch_up_last"] = _get(con, CATCHUP_META)
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if today_state:
        out["today"] = {"ct_date": today, **today_state}
    elif out["last_skip"] and out["last_skip"].get("ct_date") == today:
        out["today"] = {"ct_date": today, "state": "skipped", "reason": out["last_skip"].get("reason"),
                        "code": out["last_skip"].get("code")}
    st = out["today"].get("state")
    if st == "claimed":
        out["today"]["state"] = "in_doubt"
    # next dividend: today's cutoff (3 PM CT; 2 PM before the switch) unless today's is settled
    done_today = st == "done"
    nd = ct_now.date() + timedelta(days=1) if done_today else ct_now.date()
    nxt = _cut.day_end(nd, not_before)
    out["next_ct"] = nxt.isoformat()
    out["time_ct"] = nxt.strftime("%H:%M")
    out["cutoff_label"] = _cut.hour_label(nxt.time())
    out["cutoff_hour_ct"] = nxt.hour
    out["cutoff_effective_ct_date"] = _cut.effective_date().isoformat()
    out["window_text"] = _cut.window_text(nd, not_before)
    out["rule_text"] = rule_text(floor_usd, window_label=out["window_text"])
    # Task 17: due dates (cutoff passed) that are unpaid / in doubt; today's pending one counts too
    try:
        _states = date_states(Path(db), due_dates(now, not_before=not_before))
        out["unpaid"] = [d for d, v in _states.items() if v["state"] == "unpaid"]
        out["in_doubt_dates"] = [d for d, v in _states.items() if v["state"] == "in_doubt"]
    except Exception:  # noqa: BLE001
        pass
    out["next_due_now"] = (not done_today) and ct_now >= nxt
    # pending window for the next dividend (net so far)
    start, end = window_for(nd, not_before)
    try:
        so_far = net_closed_in_window(data_dir, start, min(end, now.astimezone(CT)))
        so_far["window_end"] = end.isoformat()
        so_far["window_hours"] = round((end - start).total_seconds() / 3600.0, 2)
        so_far["window_text"] = out["window_text"]
    except Exception as exc:  # noqa: BLE001
        so_far = {"net_usd": None, "error": str(exc)[:200]}
    out["window"] = so_far
    if so_far.get("net_usd") is not None:
        plan = plan_amount(float(so_far["net_usd"]), out["carry_usd"], floor_usd=floor_usd, min_order_usd=min_order_usd)
        out["pending"] = {**plan, "for_ct_date": nd.isoformat(), "final_at_ct": end.isoformat(),
                          "funded_from": funded_from(plan["kind"])}
    else:
        out["pending"] = None
    # "today's" net closed profit = the running cutoff->cutoff window that feeds the next dividend
    out["today_net_closed_usd"] = so_far.get("net_usd")
    out["target"] = target_progress(out.get("today_net_closed_usd"))
    # YTD average daily profit (display / goal only; snowball.reports.ytd_profit)
    try:
        from snowball.reports.ytd_profit import ytd_profit_status

        out["ytd"] = ytd_profit_status(data_dir, now, year_reset=year_reset, target_usd=DAILY_TARGET_USD,
                                       day_end=not_before)
        out["goal"] = out["ytd"].get("goal")
        out["topup_unlocked"] = bool(out["ytd"].get("topup_unlocked"))
    except Exception as exc:  # noqa: BLE001
        out["ytd"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    if free_usd is not None:
        pend = out.get("pending") or {}
        need = floor_usd if pend.get("rule") == "red_day_floor" else min_order_usd
        out["cash_check"] = {"free_usd": round(float(free_usd), 2), "needed_usd": round(float(need), 2),
                             "ok": float(free_usd) + 1e-9 >= need, "rule": pend.get("rule")}
    # history
    total_btc = 0.0
    tot = {"count": 0, "usd": 0.0, "btc": 0.0}
    by_kind: dict[str, dict[str, float]] = {}
    week_start = ct_now.date() - timedelta(days=ct_now.weekday())
    month_start = ct_now.date().replace(day=1)
    wk = {"count": 0, "usd": 0.0, "btc": 0.0}
    mo = {"count": 0, "usd": 0.0, "btc": 0.0}
    divs = []
    for cid, ts, kind, usd, btc, px, note in rows:
        btc = float(btc or 0)
        usd = float(usd or 0)
        total_btc += btc
        if kind == "pnl_sweep":
            ps = out["pnl_sweep"]
            ps["count"] += 1
            ps["usd"] = round(ps["usd"] + usd, 2)
            ps["btc"] = round(ps["btc"] + btc, 8)
        if kind not in DIVIDEND_KINDS:
            continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except ValueError:
            t = None
        n = _parse_note(note)
        ct_d = n.get("ct_date") or (t.astimezone(CT).date().isoformat() if t else None)
        item = {"id": cid, "ct_date": ct_d, "at_ct": t.astimezone(CT).isoformat() if t else None,
                "kind": kind, "kind_label": kind_label(kind), "tag": KIND_TAGS.get(kind, "flat"),
                "funded_from": funded_from(kind),
                "usd": round(usd, 2), "btc": btc, "price": float(px or 0),
                "net_closed_usd": float(n["net_closed"]) if n.get("net_closed") not in (None, "") else None,
                "catch_up": n.get("catch_up") == "1"}
        item["beat_target"] = (item["net_closed_usd"] >= DAILY_TARGET_USD) if item["net_closed_usd"] is not None else None
        divs.append(item)
        for agg in (tot,):
            agg["count"] += 1
            agg["usd"] += usd
            agg["btc"] += btc
        bk = by_kind.setdefault(kind, {"count": 0, "usd": 0.0, "btc": 0.0})
        bk["count"] += 1
        bk["usd"] += usd
        bk["btc"] += btc
        if ct_d:
            dd = _date.fromisoformat(ct_d)
            if dd >= week_start:
                wk["count"] += 1; wk["usd"] += usd; wk["btc"] += btc  # noqa: E702
            if dd >= month_start:
                mo["count"] += 1; mo["usd"] += usd; mo["btc"] += btc  # noqa: E702
    rnd = lambda a: {"count": int(a["count"]), "usd": round(a["usd"], 2), "btc": round(a["btc"], 8)}  # noqa: E731
    out["totals"] = {**rnd(tot), "by_kind": {k: rnd(v) for k, v in by_kind.items()}}
    out["week"] = {**rnd(wk), "since": week_start.isoformat()}
    out["month"] = {**rnd(mo), "since": month_start.isoformat()}
    out["treasury_btc"] = round(total_btc, 8)
    out["dividend_btc_share_pct"] = round(100.0 * tot["btc"] / total_btc, 2) if total_btc > 0 else None
    out["recent"] = list(reversed(divs[-recent:]))
    out["caught_up"] = [{"ct_date": x["ct_date"], "at_ct": x["at_ct"], "usd": x["usd"], "btc": x["btc"],
                         "price": x["price"], "kind": x["kind"]} for x in divs if x.get("catch_up")][-recent:]
    out["last"] = divs[-1] if divs else None
    return out
