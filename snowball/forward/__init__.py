"""Forward Guidance (Tb 8:58 AM CT 10/3): regime + trend-continuation probabilities per market.

Transparent and history-based. Every probability is a counted frequency from multi-year DAILY
price history, conditioned on today's regime (and the short-rate macro state), shrunk toward the
unconditional base rate, and checked out-of-sample (walk-forward, no look-ahead). Every daily
forecast is persisted (snowball_forward.db) and graded automatically once its horizon passes.
"""
