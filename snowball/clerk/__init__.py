"""The Clerk: House Clerk PTR research sidecar.

Research-only congressional disclosure logger. Never places orders, never
writes HALT, never touches live crypto or stock paper ledgers.
"""

__all__ = ["ClerkSidecar"]


def __getattr__(name: str):
    if name == "ClerkSidecar":
        from snowball.clerk.poller import ClerkSidecar

        return ClerkSidecar
    raise AttributeError(name)
