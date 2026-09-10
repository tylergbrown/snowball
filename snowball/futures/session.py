"""America/New_York session windows for Future Trader day-trade engine.

Entry window (preferred): 09:25–09:30 ET — place one long per index if flat.
Late catch-up: if the bot was down during the preferred window, the first tick
after 09:25 ET and before the exit window may still enter (once per ET day).

Exit window: 15:55–16:00 ET — close only if green (mark >= entry); otherwise
hold overnight. Weekday M–F only for v1 (US holidays not calendared).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Documented defaults (overridable via Settings)
DEFAULT_ENTRY_START = time(9, 25)
DEFAULT_ENTRY_END = time(9, 30)
DEFAULT_EXIT_START = time(15, 55)
DEFAULT_EXIT_END = time(16, 0)


def _parse_hhmm(raw: str, fallback: time) -> time:
    try:
        parts = (raw or "").strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError, IndexError):
        return fallback


def to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET)


def et_date_str(now: datetime) -> str:
    return to_et(now).date().isoformat()


def is_us_weekday(now: datetime) -> bool:
    """Mon–Fri in America/New_York. Does not skip US market holidays (v1 limitation)."""
    return to_et(now).weekday() < 5


def in_entry_preferred_window(
    now: datetime,
    *,
    start: time = DEFAULT_ENTRY_START,
    end: time = DEFAULT_ENTRY_END,
) -> bool:
    t = to_et(now).time()
    return start <= t < end


def entry_allowed(
    now: datetime,
    *,
    entry_start: time = DEFAULT_ENTRY_START,
    exit_start: time = DEFAULT_EXIT_START,
) -> bool:
    """True from entry_start ET until exit_start ET on weekdays (late catch-up OK)."""
    if not is_us_weekday(now):
        return False
    t = to_et(now).time()
    return entry_start <= t < exit_start


def in_exit_window(
    now: datetime,
    *,
    start: time = DEFAULT_EXIT_START,
    end: time = DEFAULT_EXIT_END,
) -> bool:
    if not is_us_weekday(now):
        return False
    t = to_et(now).time()
    return start <= t < end


def session_times_from_settings(settings: object) -> dict[str, time]:
    return {
        "entry_start": _parse_hhmm(
            getattr(settings, "futures_entry_start_et", "09:25"), DEFAULT_ENTRY_START
        ),
        "entry_end": _parse_hhmm(
            getattr(settings, "futures_entry_end_et", "09:30"), DEFAULT_ENTRY_END
        ),
        "exit_start": _parse_hhmm(
            getattr(settings, "futures_exit_start_et", "15:55"), DEFAULT_EXIT_START
        ),
        "exit_end": _parse_hhmm(
            getattr(settings, "futures_exit_end_et", "16:00"), DEFAULT_EXIT_END
        ),
    }


def classify_session_state(
    *,
    open_lots: list,
    now: datetime,
) -> str:
    """Return flat | open_today | holding_overnight for dashboard."""
    if not open_lots:
        return "flat"
    today = et_date_str(now)
    # Any lot opened on a prior ET day ⇒ overnight hold
    for lot in open_lots:
        opened = lot.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if et_date_str(opened) < today:
            return "holding_overnight"
    return "open_today"
