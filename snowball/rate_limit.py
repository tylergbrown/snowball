"""Process-wide spacing for Coinbase public REST (shared across lanes)."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_next_ok = 0.0


def wait_turn(min_interval_sec: float) -> None:
    """Block until at least min_interval_sec since the last public call."""
    global _next_ok
    gap = max(0.0, float(min_interval_sec))
    with _lock:
        now = time.monotonic()
        delay = _next_ok - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _next_ok = now + gap


def penalize(extra_sec: float) -> None:
    """Push the next allowed call further out after a 429."""
    global _next_ok
    with _lock:
        _next_ok = max(_next_ok, time.monotonic() + max(0.0, float(extra_sec)))
