"""Fed Desk — CME FedWatch research + educated FOMC directional bets.

Dual-gated live: FED_MODE=live AND FED_LIVE_ENABLED=true.
Research (FedWatch poll) always runs when FED_ENABLED.
Educated bets only inside the 14-day pre-FOMC window through decision day,
and only when max(P_hold, P_hike, P_cut) >= 70%.
"""

from snowball.fed.market import DEFAULT_FED_PRODUCTS
from snowball.fed.probs import BET_SKEW_THRESHOLD, WINDOW_DAYS, classify_meeting_probs

__all__ = [
    "DEFAULT_FED_PRODUCTS",
    "BET_SKEW_THRESHOLD",
    "WINDOW_DAYS",
    "classify_meeting_probs",
]
