"""Bitcoin treasury ledger — separate from CRYPTO trader (backstop).

Policy (locked):
  - Holdings are NOT the live CRYPTO trading lane.
  - Soft-reserve: treasury BTC mark/cost is subtracted from the CRYPTO
    allocation base so strategies cannot spend it as deployable bankroll.
  - Hard-block: live BTC sells must leave wallet >= reserved treasury qty
    when NEVER_SELL_TREASURY is on (default).
  - Weekly $50 Fridays 2pm America/Chicago (contribution kind ``friday_50``).
  - 50% of positive closed realized P/L → treasury (``pnl_sweep``);
    remaining 50% pro-rata STOCK / FT / Crash / Fed (handled outside this
    ledger; we only record the BTC buy into treasury).
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
