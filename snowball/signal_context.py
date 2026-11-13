"""Per-fill signal-context snapshots (Task 15) + research proposal link (Task 16).

Engines call :func:`stage` right before they hand an order to a ledger, with
the exact values their decision used (indicators, rule, floor/target, gates).
Every ledger calls :func:`attach_fill_context` right after it inserts a fill,
inside the same SQLite transaction, which writes ``fills.context_json`` (and
``fills.proposal_id`` when a research proposal was linked).

Guarantees:
* never raises into the trading path (all failures are logged and swallowed);
* never invents values: fields the engine did not stage are simply absent; a
  fill with no staged context gets ``{"source": "fill_only"}`` plus the gate
  snapshot that can be read at fill time;
* a staged context is only used for a fill on the same product whose reason
  starts with the staged reason, within ``STAGE_TTL_SEC``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("snowball.signal_context")

SCHEMA_VERSION = 1
STAGE_TTL_SEC = 900.0

_LOCK = threading.Lock()
_STAGED: dict[str, tuple[float, dict[str, Any]]] = {}
_CFG: dict[str, Any] = {"settings": None, "watcher": None, "proposals": None}

# Strategy -> (timeframe, indicator fields read from PairSnapshot)
_STRATEGY_FIELDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "sma_15m": ("15m", ("sma_fast", "sma_slow", "signal"), "candle_ts"),
    "sma_5m": ("5m", ("sma_fast_5m", "sma_slow_5m", "signal_5m"), "candle_ts_5m"),
    "sma_1d": ("1d", ("sma_fast_1d", "sma_slow_1d", "signal_1d"), "candle_ts_1d"),
    "ema_15m": ("15m", ("ema_fast_15m", "ema_slow_15m", "signal_ema_15m"), "candle_ts"),
    "donchian_1d": ("1d", ("donchian_high_1d", "donchian_low_1d", "signal_donchian_1d"), "candle_ts_1d"),
    "rsi_15m": ("15m", ("rsi_15m", "signal_rsi_15m"), "candle_ts"),
    "rsi_1d": ("1d", ("rsi_1d", "signal_rsi_1d"), "candle_ts_1d"),
    "bb_15m": ("15m", ("bb_upper_15m", "bb_mid_15m", "bb_lower_15m", "signal_bb_15m"), "candle_ts"),
    "bb_1d": ("1d", ("bb_upper_1d", "bb_mid_1d", "bb_lower_1d", "signal_bb_1d"), "candle_ts_1d"),
}


def configure(*, settings: Any = None, watcher: Any = None, proposals: Any = None) -> None:
    if settings is not None:
        _CFG["settings"] = settings
    if watcher is not None:
        _CFG["watcher"] = watcher
    if proposals is not None:
        _CFG["proposals"] = proposals


def reset_for_tests() -> None:
    with _LOCK:
        _STAGED.clear()
    _CFG.update({"settings": None, "watcher": None, "proposals": None})


def _num(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 8)
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return v


def strategy_params(strategy_id: str, settings: Any = None) -> dict[str, Any]:
    s = settings if settings is not None else _CFG.get("settings")
    g = (lambda k, d: getattr(s, k, d)) if s is not None else (lambda k, d: d)
    if strategy_id.startswith("sma_"):
        return {"fast_period": g("sma_fast", 20), "slow_period": g("sma_slow", 50)}
    if strategy_id == "ema_15m":
        return {"fast_period": 12, "slow_period": 26}
    if strategy_id == "donchian_1d":
        return {"entry_lookback": 20, "exit_lookback": 10}
    if strategy_id.startswith("rsi_"):
        return {"period": g("rsi_period", 14), "oversold": 30.0, "overbought": 70.0}
    if strategy_id.startswith("bb_"):
        return {"period": g("bb_period", 20), "std_mult": g("bb_std_mult", 2.0)}
    return {}


def indicators_for(snap: Any, strategy_id: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Exact indicator values on the PairSnapshot the decision used (no recompute)."""
    out: dict[str, Any] = {}
    if snap is None:
        return out, None, None
    for k in ("last", "bid", "ask"):
        v = getattr(snap, k, None)
        if v is not None:
            out[k] = _num(v)
    spec = _STRATEGY_FIELDS.get(strategy_id)
    if spec is None:
        return out, None, None
    tf, fields, candle_attr = spec
    for f in fields:
        v = getattr(snap, f, None)
        if v is not None:
            out[f] = _num(v)
    out.update({f"param_{k}": v for k, v in strategy_params(strategy_id).items()})
    candle = getattr(snap, candle_attr, None)
    return out, tf, (_num(candle) if candle is not None else None)


def research_gates(now: datetime | None = None) -> dict[str, Any]:
    """Watcher / FOMC gate state readable at decision time (never raises)."""
    out: dict[str, Any] = {}
    s = _CFG.get("settings")
    now = now or datetime.now(timezone.utc)
    try:
        if s is not None:
            halt_file = Path(getattr(s, "halt_file", "./data/HALT"))
            flag = halt_file.parent / "WATCHER_HALT"
            out["halt_active"] = halt_file.exists()
            out["watcher_halt_flag"] = flag.exists()
            if flag.exists():
                try:
                    out["watcher_halt_reason"] = flag.read_text(encoding="utf-8").strip()[:200]
                except OSError:
                    pass
            out["watcher_fomc_gate_enabled"] = bool(getattr(s, "watcher_halt_around_fomc", False))
    except Exception as exc:  # noqa: BLE001
        out["gate_error"] = str(exc)[:120]
    w = _CFG.get("watcher")
    if w is not None:
        try:
            cal = [
                ev for ev in w.upcoming_calendar(now, hours=72)
                if {"fomc", "rate_decision"} & set(getattr(ev, "tags", []) or [])
            ]
            out["fomc_calendar_events_72h"] = len(cal)
            if cal:
                ev = cal[0]
                out["fomc_next"] = {
                    "title": ev.title[:120], "source": ev.source,
                    "at": _num(ev.published_at), "importance": ev.importance,
                }
        except Exception as exc:  # noqa: BLE001
            out["fomc_calendar_error"] = str(exc)[:120]
    return out


def stage(
    product: str,
    reason: str,
    *,
    lane: str,
    strategy: str,
    action: str,
    side: str | None = None,
    rule: str | None = None,
    price: float | None = None,
    snap: Any = None,
    indicators: dict[str, Any] | None = None,
    timeframe: str | None = None,
    candle_ts: Any = None,
    floor: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
    proposal: dict[str, Any] | None = None,
    now: datetime | None = None,
    match_proposal: bool = True,
) -> dict[str, Any]:
    """Record what the engine saw right before it sends an order. Returns the context."""
    try:
        ind, tf, candle = indicators_for(snap, strategy) if snap is not None else ({}, None, None)
        if indicators:
            ind.update({k: _num(v) for k, v in indicators.items()})
        ctx: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "source": "engine",
            "recorded_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "lane": lane,
            "product": product,
            "strategy": strategy,
            "action": action,
            "side": side,
            "rule": rule or reason,
            "reason": reason,
            "price": _num(price),
            "timeframe": timeframe or tf,
            "candle_ts": _num(candle_ts) if candle_ts is not None else candle,
            "indicators": ind,
        }
        if floor:
            ctx["floor"] = {k: _num(v) for k, v in floor.items()}
        if target:
            ctx["target"] = {k: _num(v) for k, v in target.items()}
        ctx["gates"] = {k: _num(v) if not isinstance(v, dict) else v for k, v in (gates or {}).items()}
        ctx["research_gates"] = research_gates(now)
        if research:
            ctx["research"] = research
        if proposal:
            ctx["proposal"] = proposal
        elif match_proposal and action == "entry" and side:
            store = _CFG.get("proposals")
            if store is not None:
                try:
                    p = store.match_live(product=product, side=side, lane=lane, now=now)
                    if p is not None:
                        ctx["proposal"] = p
                except Exception as exc:  # noqa: BLE001
                    log.warning("proposal match failed: %s", exc)
        ctx = {k: v for k, v in ctx.items() if v is not None}
        with _LOCK:
            _STAGED[product] = (time.monotonic(), ctx)
        return ctx
    except Exception as exc:  # noqa: BLE001 — never break the trading path
        log.warning("signal context stage failed: %s", exc)
        return {}


def _reason_base(reason: str) -> str:
    return str(reason or "").split(":live")[0]


def take(product: str, reason: str) -> dict[str, Any] | None:
    with _LOCK:
        item = _STAGED.get(product)
    if item is None:
        return None
    ts, ctx = item
    if time.monotonic() - ts > STAGE_TTL_SEC:
        return None
    staged = str(ctx.get("reason") or "")
    if staged and not str(reason or "").startswith(_reason_base(staged)):
        return None
    return dict(ctx)


def ensure_fill_context_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(fills)").fetchall()}
    if "context_json" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN context_json TEXT")
    if "proposal_id" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN proposal_id INTEGER")


def attach_fill_context(conn: sqlite3.Connection, fill_id: int) -> dict[str, Any] | None:
    """Write context_json/proposal_id onto a just-inserted fill (same transaction)."""
    try:
        row = conn.execute(
            "SELECT product, side, reason, price, ts, strategy, position_id FROM fills WHERE id = ?",
            (int(fill_id),),
        ).fetchone()
        if row is None:
            return None
        product, side, reason, price, ts, strategy, position_id = (row[i] for i in range(7))
        ctx = take(str(product), str(reason or ""))
        if ctx is None:
            ctx = {
                "v": SCHEMA_VERSION,
                "source": "fill_only",
                "note": "no engine context staged for this fill (reconciled/adopted/emergency path)",
                "product": product,
                "strategy": strategy,
                "rule": reason,
                "research_gates": research_gates(),
            }
        ctx["fill"] = {"id": int(fill_id), "side": side, "price": _num(price), "ts": ts,
                       "position_id": position_id}
        prop = ctx.get("proposal") if isinstance(ctx.get("proposal"), dict) else None
        pid = int(prop["id"]) if prop and prop.get("id") is not None else None
        conn.execute(
            "UPDATE fills SET context_json = ?, proposal_id = ? WHERE id = ?",
            (json.dumps(ctx, default=str, separators=(",", ":")), pid, int(fill_id)),
        )
        if pid is not None and ctx.get("action") == "entry":
            store = _CFG.get("proposals")
            if store is not None:
                try:
                    store.mark_taken(pid, lane=str(ctx.get("lane") or ""), fill_id=int(fill_id),
                                     position_id=position_id, strategy=str(strategy or ""),
                                     price=price, ts=ts)
                except Exception as exc:  # noqa: BLE001
                    log.warning("proposal mark_taken failed: %s", exc)
        return ctx
    except Exception as exc:  # noqa: BLE001 — never break the trading path
        log.warning("attach fill context failed: %s", exc)
        return None


def load_context(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None
