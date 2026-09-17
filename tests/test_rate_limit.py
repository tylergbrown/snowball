
from __future__ import annotations

import time

from snowball import rate_limit as rl


def test_wait_turn_spaces_calls(monkeypatch):
    # Reset module state
    rl._next_ok = 0.0
    sleeps: list[float] = []
    real_sleep = time.sleep
    monkeypatch.setattr(rl.time, "sleep", lambda s: sleeps.append(s))
    # First call should not sleep
    rl.wait_turn(0.2)
    assert sleeps == []
    # Second immediate call should sleep ~0.2
    rl.wait_turn(0.2)
    assert sleeps and sleeps[0] >= 0.0


def test_penalize_extends_next_ok():
    rl._next_ok = 0.0
    rl.penalize(5.0)
    assert rl._next_ok > time.monotonic()
