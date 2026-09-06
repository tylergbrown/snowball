"""Optional FOMC/rate-decision HALT window for The Watcher.

Default off. The Watcher only clears HALT if it created it (sibling WATCHER_HALT).
Yolo Demon must never call this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from snowball.halt import clear_halt, write_halt
from snowball.watcher.store import ResearchEvent

log = logging.getLogger("snowball.watcher")

HALT_TAGS = frozenset({"fomc", "rate_decision"})


def watcher_halt_flag(halt_file: Path) -> Path:
    return halt_file.parent / "WATCHER_HALT"


def in_halt_window(
    event: ResearchEvent,
    now: datetime,
    minutes_before: int,
    minutes_after: int,
) -> bool:
    if event.kind != "calendar" or event.published_at is None:
        return False
    if event.importance is None or int(event.importance) < 3:
        return False
    if not (HALT_TAGS & set(event.tags)):
        return False
    start = event.published_at - timedelta(minutes=minutes_before)
    end = event.published_at + timedelta(minutes=minutes_after)
    return start <= now <= end


def matching_halt_event(
    events: list[ResearchEvent],
    now: datetime,
    minutes_before: int,
    minutes_after: int,
) -> ResearchEvent | None:
    for ev in events:
        if in_halt_window(ev, now, minutes_before, minutes_after):
            return ev
    return None


def apply_watcher_halt(
    halt_file: Path,
    should_halt: bool,
    reason: str | None = None,
) -> str:
    """Write or clear HALT. Never deletes a user/dashboard HALT.

    Returns a short status string for logs/tests.
    """
    halt_file = Path(halt_file)
    flag = watcher_halt_flag(halt_file)
    if should_halt:
        if not halt_file.exists():
            write_halt(halt_file)
            flag.write_text((reason or "The Watcher FOMC/rate window") + "\n", encoding="utf-8")
            log.info(
                "The Watcher wrote HALT for high-importance rate window",
                extra={"data": {"reason": reason}},
            )
            return "wrote"
        if flag.exists():
            return "held"
        log.info("The Watcher: HALT already present (not ours); leaving in place")
        return "user_halt"
    # Window ended: only clear if we created it
    if flag.exists():
        try:
            clear_halt(halt_file)
        except IsADirectoryError:
            log.warning("The Watcher: HALT path is a directory; not removing")
            return "halt_is_dir"
        try:
            flag.unlink()
        except OSError:
            pass
        log.info("The Watcher cleared HALT (window ended, WATCHER_HALT owned)")
        return "cleared"
    return "idle"
