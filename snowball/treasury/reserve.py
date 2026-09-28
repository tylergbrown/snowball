"""Soft reserve: exclude treasury BTC mark/cost from CRYPTO deployable capital.

Hard-block: live BTC sells must leave wallet BTC >= reserved treasury qty.
Contributions (friday_50 / pnl_sweep / manual) remain ADD-only — no withdraw.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from snowball.treasury.ledger import default_treasury_db, holdings_and_cost


def treasury_db_path(data_dir_or_db: Path) -> Path:
    """Accept either a data dir or a full path to snowball_treasury.db."""
    p = Path(data_dir_or_db)
    if p.name == "snowball_treasury.db" or p.suffix == ".db":
        return p
    return default_treasury_db(p)


def latest_mark_btc_usd(db_path: Path) -> float | None:
    """Most recent stored BTC-USD mark price, or None."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT btc_usd FROM marks ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    px = float(row[0])
    return px if px > 0 else None


def treasury_soft_reserve(
    db_path: Path,
    *,
    mark_btc_usd: float | None = None,
) -> dict[str, float]:
    """Read treasury holdings and return soft-reserve amounts.

    Returns keys: btc_qty, mark_usd, cost_usd, reserve_usd.
    ``reserve_usd`` prefers mark value; falls back to cost basis so the CRYPTO
    lane still excludes treasury when spot is briefly unavailable.
    """
    path = treasury_db_path(db_path)
    qty, cost, n = holdings_and_cost(path)
    if n == 0 or qty <= 1e-12:
        return {
            "btc_qty": 0.0,
            "mark_usd": 0.0,
            "cost_usd": 0.0,
            "reserve_usd": 0.0,
        }
    px = float(mark_btc_usd) if mark_btc_usd is not None and mark_btc_usd > 0 else None
    if px is None:
        px = latest_mark_btc_usd(path)
    mark_usd = float(qty * px) if px is not None and px > 0 else 0.0
    reserve_usd = mark_usd if mark_usd > 0 else max(0.0, float(cost))
    return {
        "btc_qty": float(qty),
        "mark_usd": float(mark_usd),
        "cost_usd": float(cost),
        "reserve_usd": float(reserve_usd),
    }


def is_btc_product(product: str) -> bool:
    p = (product or "").strip().upper()
    return p == "BTC-USD" or p.startswith("BTC-")


def treasury_btc_sell_allowed(
    *,
    product: str,
    sell_qty: float,
    wallet_btc: float | None,
    reserved_btc: float,
    enabled: bool = True,
    eps: float = 1e-10,
) -> tuple[bool, str]:
    """Hard-block BTC sells that would dip wallet below reserved treasury qty.

    Non-BTC products always allowed. When reserve is empty or disabled, allow.
    When wallet balance is unknown and reserved_btc > 0, refuse (fail closed).
    """
    if not enabled:
        return True, "ok"
    if not is_btc_product(product):
        return True, "ok"
    reserved = max(0.0, float(reserved_btc))
    if reserved <= eps:
        return True, "ok"
    qty = max(0.0, float(sell_qty))
    if qty <= eps:
        return True, "ok"
    if wallet_btc is None:
        return False, "never_sell_treasury_no_balance"
    remaining = float(wallet_btc) - qty
    if remaining + eps < reserved:
        return False, "never_sell_treasury"
    return True, "ok"
