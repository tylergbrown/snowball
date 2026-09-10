"""Crash Guard — short hedge lane on SPY/QQQ INTX perps.

Dual-gated live: CRASH_MODE=live AND CRASH_LIVE_ENABLED=true.
Isolated ledger (never mixes with crypto/stock/futures long books).
Never cover red shorts; only close when short PnL >= min TP + fee buffer.
"""

from snowball.crash.market import DEFAULT_CRASH_PRODUCTS

__all__ = ["DEFAULT_CRASH_PRODUCTS"]
