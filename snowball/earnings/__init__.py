"""Earnings Scout: quarterly earnings research sidecar.

Research-only. Logs upcoming earnings, pre-release momentum, and post-release
reaction. Never places orders, never writes HALT, never touches crypto/stock/
futures ledgers.
"""

__all__ = ["EarningsScout"]


def __getattr__(name: str):
    if name == "EarningsScout":
        from snowball.earnings.poller import EarningsScout

        return EarningsScout
    raise AttributeError(name)
