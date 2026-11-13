"""Bitcoin treasury ledger — separate from CRYPTO trader (backstop).

Policy (locked):
  - Holdings are NOT the live CRYPTO trading lane.
  - Soft-reserve: treasury BTC mark/cost is subtracted from the CRYPTO
    allocation base so strategies cannot spend it as deployable bankroll.
  - Hard-block: live BTC sells must leave wallet >= reserved treasury qty
    when NEVER_SELL_TREASURY is on (default).
  - Daily profit dividend — Robo Trader → Treasury, 3pm America/Chicago
    (Task 13, 2026-09-30; cutoff 2pm -> 3pm CT from 10/2, snowball.treasury.cutoff):
    100% of NET live closed realized P/L across all lanes for the window
    (yesterday's cutoff, today's cutoff] CT (kind
    ``dividend``); red/flat day -> flat $50 floor buy (kind ``red_day_floor``).
    tools/treasury_dca.py, run by Tb's routine. Replaces the flat $10 daily
    (``daily_dca``), the weekly $50 Friday (``friday_50``) and the 50% P/L
    sweep (``pnl_sweep``, retired; legacy rows still display).
  - Contributions are ADD-only — never withdraw/sell treasury here.
  - NEVER_SELL_RED stays on for trader lots.

Seed note: the 2026-09-27 ~$620 BTC Coinbase Advanced deposit is CRYPTO-lane
bankroll fuel per SnowBall profile — it is **not** a treasury seed.
"""

from snowball.treasury.ledger import (
    Contribution,
    TreasurySnapshot,
    default_treasury_db,
    ensure_schema,
    holdings_and_cost,
    record_contribution,
    snapshot,
)
from snowball.treasury.reserve import (
    is_btc_product,
    treasury_btc_sell_allowed,
    treasury_soft_reserve,
)

__all__ = [
    "Contribution",
    "TreasurySnapshot",
    "default_treasury_db",
    "ensure_schema",
    "holdings_and_cost",
    "is_btc_product",
    "record_contribution",
    "snapshot",
    "treasury_btc_sell_allowed",
    "treasury_soft_reserve",
]
