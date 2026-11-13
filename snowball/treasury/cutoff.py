"""Dividend / daily-profit day cutoff (America/Chicago) — one schedule for every reader.

Tb 9:39 AM CT 10/2: the daily window moves from 2 PM -> 2 PM CT to 3 PM -> 3 PM CT, paid at
3 PM CT (config ``DIVIDEND_CUTOFF_HOUR_CT``, default 15). The transition has no gap and no
double count because every window is built from consecutive day ENDS:

    end(D)      = D at 14:00 CT                 for D <  DIVIDEND_CUTOFF_EFFECTIVE_CT_DATE
                = D at DIVIDEND_CUTOFF_HOUR_CT  for D >= DIVIDEND_CUTOFF_EFFECTIVE_CT_DATE
    window(D)   = (end(D-1), end(D)]            labelled by its end date D

With the default effective date 2026-10-02: the 10/1 window (9/30 2 PM, 10/1 2 PM] (already
paid $8.28) is unchanged; the 10/2 window is (10/1 2 PM, 10/2 3 PM] = 25 h; from 10/3 every
window is 24 h, 3 PM -> 3 PM. If image n were NOT live before 10/2 2 PM CT, set
DIVIDEND_CUTOFF_EFFECTIVE_CT_DATE=2026-10-03 so 10/2 keeps the old 2 PM window (paid by the
old code) and the 25 h transition window is (10/2 2 PM, 10/3 3 PM].

An explicit fixed ``dtime`` passed by a caller (legacy ``not_before`` / ``day_end``
arguments) means that hour for every date (old behaviour, used by tests and manual tools).
No heavy imports: the treasury tool, the PDF and the plugins' snapshot all read this.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
OLD_CUTOFF_HOUR = 14
DEFAULT_CUTOFF_HOUR = 15
DEFAULT_EFFECTIVE = "2026-10-02"


def cutoff_hour() -> int:
    """DIVIDEND_CUTOFF_HOUR_CT (0-23), default 15; invalid -> 15."""
    raw = os.environ.get("DIVIDEND_CUTOFF_HOUR_CT", "")
    try:
        h = int(str(raw).strip()) if str(raw).strip() else DEFAULT_CUTOFF_HOUR
    except ValueError:
        return DEFAULT_CUTOFF_HOUR
    return h if 0 <= h <= 23 else DEFAULT_CUTOFF_HOUR


def effective_date() -> date:
    """First CT date whose window ENDS at the new hour (DIVIDEND_CUTOFF_EFFECTIVE_CT_DATE)."""
    raw = str(os.environ.get("DIVIDEND_CUTOFF_EFFECTIVE_CT_DATE", "") or "").strip() or DEFAULT_EFFECTIVE
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.fromisoformat(DEFAULT_EFFECTIVE)


def _d(x: str | date) -> date:
    return date.fromisoformat(x) if isinstance(x, str) else x


def cutoff_time(d: str | date, fixed: dtime | None = None) -> dtime:
    if fixed is not None:
        return fixed
    return dtime(OLD_CUTOFF_HOUR, 0) if _d(d) < effective_date() else dtime(cutoff_hour(), 0)


def day_end(d: str | date, fixed: dtime | None = None) -> datetime:
    dd = _d(d)
    return datetime.combine(dd, cutoff_time(dd, fixed), tzinfo=CT)


def window(d: str | date, fixed: dtime | None = None) -> tuple[datetime, datetime]:
    """(start, end] of the window labelled CT date *d*."""
    dd = _d(d)
    return day_end(dd - timedelta(days=1), fixed), day_end(dd, fixed)


def day_of(t: datetime, fixed: dtime | None = None) -> date:
    """CT end date D of the window (end(D-1), end(D)] containing *t*."""
    c = t.astimezone(CT)
    return c.date() if c <= day_end(c.date(), fixed) else c.date() + timedelta(days=1)


def hour_label(t: dtime | int) -> str:
    h = t if isinstance(t, int) else t.hour
    m = 0 if isinstance(t, int) else t.minute
    suf = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suf}" if m == 0 else f"{h12}:{m:02d}{suf}"


def current_label(fixed: dtime | None = None) -> str:
    """'3pm' (the cutoff for windows from the effective date on)."""
    return hour_label(fixed if fixed is not None else dtime(cutoff_hour(), 0))


def window_text(d: str | date, fixed: dtime | None = None) -> str:
    """'3pm->3pm CT' or, for the transition, '10/1 2pm -> 10/2 3pm CT (25h transition)'."""
    s, e = window(d, fixed)
    hours = (e - s).total_seconds() / 3600.0
    if s.time() == e.time():  # normal day (a DST change day is 23/25 h but still 3pm->3pm)
        return f"{hour_label(e.time())}\u2192{hour_label(e.time())} CT"
    return (f"{s.month}/{s.day} {hour_label(s.time())} \u2192 {e.month}/{e.day} {hour_label(e.time())} CT "
            f"({hours:g}h transition)")
