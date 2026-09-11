"""Shared per-leg notional sizing (auto-scales with total account value).

Formula when ``autoscale`` is on:
  per_leg = max(BASE, BASE * (1 + (scale_per_100_usd_pct/100) * floor(AV/100)))

With defaults BASE=100 and scale_per_100_usd_pct=1:
  per_leg = max(100, 100 + floor(AV/100))
  e.g. AV=1000 → 110; AV=2000 → 120; AV=50 → 100 (floor at BASE).

When ``autoscale`` is false, returns the fixed BASE (backward-compatible fixed size).
Uses total account value (same AV the lanes use for budget allocation), not free cash.
"""

from __future__ import annotations

import math
from typing import Any


def per_leg_notional_usd(
    account_value_usd: float,
    *,
    base_usd: float = 100.0,
    scale_per_100_usd_pct: float = 1.0,
    autoscale: bool = True,
) -> float:
    """Return the effective per-leg notional cap in USD."""
    base = max(0.0, float(base_usd))
    if not autoscale:
        return base
    av = max(0.0, float(account_value_usd))
    blocks = math.floor(av / 100.0)
    # scale_per_100_usd_pct=1 → +1% of base per $100 of AV
    scale_frac = max(0.0, float(scale_per_100_usd_pct)) / 100.0
    raw = base * (1.0 + scale_frac * float(blocks))
    # Round to cents so USD sizes are stable (avoid 110.00000000000001).
    return max(base, round(raw, 2))


def effective_per_leg_from_settings(
    settings: Any, account_value_usd: float
) -> float:
    """Convenience wrapper reading PER_LEG_* knobs off a Settings-like object."""
    return per_leg_notional_usd(
        account_value_usd,
        base_usd=float(getattr(settings, "per_leg_base_usd", 100.0)),
        scale_per_100_usd_pct=float(
            getattr(settings, "per_leg_scale_per_100_usd_pct", 1.0)
        ),
        autoscale=bool(getattr(settings, "per_leg_autoscale", True)),
    )


def cfm_required_margin_usd(
    price: float,
    *,
    contracts: int = 1,
    leverage: float = 1.0,
    contract_size: float = 1.0,
    margin_rate: float = 0.10,
) -> float:
    """USD margin needed for CFM CDE contracts.

    CFM posts exchange margin (~5–7% overnight). We use ``margin_rate`` (default
    10% buffer) when leverage<=1. When leverage>1, require notional/leverage
    (never less than margin_rate * notional).
    """
    px = abs(float(price))
    n = max(0, int(contracts))
    if px <= 0 or n <= 0:
        return 0.0
    notional = px * float(contract_size) * float(n)
    rate = max(0.0, float(margin_rate))
    lev = max(1.0, float(leverage))
    by_rate = notional * rate
    by_lev = notional / lev
    # Conservative: post at least the CFM-style rate, and notional/lev when levered.
    return max(by_rate, by_lev if lev > 1.0 else by_rate)


def cfm_contract_count(
    *,
    price: float,
    budget_usd: float,
    available_margin_usd: float,
    max_contracts: int = 1,
    leverage: float = 1.0,
    contract_size: float = 1.0,
    margin_rate: float = 0.10,
) -> int:
    """Integer CFM contracts (0..max) that fit budget + available margin.

    Floors at whole contracts — never opens a fractional CDE size. Returns 0 when
    a single contract's required margin exceeds either gate.
    """
    import math

    max_c = max(0, int(max_contracts))
    if max_c < 1 or float(price) <= 0:
        return 0
    req1 = cfm_required_margin_usd(
        price,
        contracts=1,
        leverage=leverage,
        contract_size=contract_size,
        margin_rate=margin_rate,
    )
    if req1 <= 0:
        return 0
    aff_budget = math.floor(float(budget_usd) / req1 + 1e-12)
    aff_margin = math.floor(float(available_margin_usd) / req1 + 1e-12)
    return max(0, min(max_c, int(aff_budget), int(aff_margin)))

