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
