"""REAL Coinbase balances for reports (2026-10-10).

Snowball's lane ledgers (e.g. the futures "equity" ~$11k) are internal BUDGET figures, not money.
This module reads the actual Coinbase numbers, read-only (no orders, allow_orders=False):

* CFM (futures) balance summary: cfm_usd_balance, unrealized_pnl, liquidation_buffer_amount,
  liquidation_buffer_percentage, available_margin, total_usd_balance, cbi_usd_balance.
* Spot USD free: ccxt fetch_balance()["USD"]["free"] (falls back to cbi_usd_balance).

Never raises; returns {"available": False, "why": ...} on any failure. Never returns secrets.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("snowball.reports.real_balances")

_CFM_FIELDS = ("cfm_usd_balance", "unrealized_pnl", "liquidation_buffer_amount", "liquidation_buffer_percentage",
               "available_margin", "total_usd_balance", "cbi_usd_balance", "initial_margin",
               "futures_buying_power", "daily_realized_pnl", "liquidation_threshold")


def _v(x: Any) -> float | None:
    try:
        return float(x["value"] if isinstance(x, dict) else x)
    except (TypeError, ValueError, KeyError):
        return None


def parse_cfm_summary(raw: dict | None) -> dict:
    bs = (raw or {}).get("balance_summary") or {}
    out = {k: _v(bs.get(k)) for k in _CFM_FIELDS}
    on = bs.get("overnight_margin_window_measure") or {}
    out["overnight_maintenance_margin"] = _v(on.get("maintenance_margin"))
    return out


def spot_usd_free(balance: dict | None) -> float | None:
    b = balance or {}
    usd = b.get("USD")
    if isinstance(usd, dict) and usd.get("free") is not None:
        try:
            return float(usd["free"])
        except (TypeError, ValueError):
            return None
    free = b.get("free") if isinstance(b.get("free"), dict) else {}
    try:
        return float(free["USD"]) if "USD" in free else None
    except (TypeError, ValueError):
        return None


def real_balances_from_exchange(exchange: Any) -> dict:
    """Read-only. `exchange` is a ccxt coinbase client (or a fake with the same two calls)."""
    out: dict = {"available": False, "at": datetime.now(timezone.utc).isoformat(), "source": "coinbase"}
    try:
        fn = getattr(exchange, "v3PrivateGetBrokerageCfmBalanceSummary", None)
        if not callable(fn):
            return {**out, "why": "cfm_balance_summary_unsupported"}
        cfm = parse_cfm_summary(fn())
    except Exception as exc:  # noqa: BLE001
        return {**out, "why": f"cfm_balance_summary_failed:{type(exc).__name__}"}
    spot = None
    try:
        spot = spot_usd_free(exchange.fetch_balance())
    except Exception as exc:  # noqa: BLE001
        log.warning("spot balance fetch failed: %s", type(exc).__name__)
    if spot is None:
        spot = cfm.get("cbi_usd_balance")
        out["spot_usd_free_source"] = "cbi_usd_balance"
    else:
        out["spot_usd_free_source"] = "fetch_balance.USD.free"
    return {**out, "available": True, "cfm": cfm, "spot_usd_free": spot}


def fetch_real_balances(settings: Any) -> dict:
    """Build a read-only Coinbase client from settings and read the real balances."""
    try:
        from snowball.futures.market import CoinbaseFuturesMarket

        m = CoinbaseFuturesMarket(api_key=getattr(settings, "coinbase_api_key", ""),
                                  api_secret=getattr(settings, "coinbase_api_secret", ""),
                                  api_passphrase=getattr(settings, "coinbase_api_passphrase", ""),
                                  allow_orders=False)
        return real_balances_from_exchange(m.exchange)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "why": f"client_failed:{type(exc).__name__}",
                "at": datetime.now(timezone.utc).isoformat()}
