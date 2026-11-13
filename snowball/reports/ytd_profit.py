"""Rolling year-to-date average daily net closed profit — "beat the YTD average" (display only).

Tb 2026-10-01 12:41 AM CT. Definition
* Day = the bot's daily window, the same one the treasury dividend and the daily profit
  minimum use: (end(D-1), end(D)], labelled by its end date D (America/Chicago). Since Tb
  9:39 AM CT 10/2 the cutoff is 3 PM CT (DIVIDEND_CUTOFF_HOUR_CT; 2 PM before 10/2, the 10/2
  day is the 25 h transition window 10/1 2 PM -> 10/2 3 PM); see snowball.treasury.cutoff.
* Profit = live closed realized P/L in that window, summed over every lane (crypto, stocks
  archive, futures/FT, crash, fed) — exactly ``snowball.treasury.dca.net_closed_in_window``
  (paper/sim closes excluded, unrealized never counts).
* Counted days = every completed window from the first day with live closed P/L this
  year through the last window whose cutoff (3 PM CT) has passed. Red, flat and no-trade days all
  count (as negative or $0). The window still running ("today") is shown against the
  average but is not in it until it closes.
* Year = windows whose end date D is on/after the YEAR_RESET month-day (CT) of the year;
  default ``01-01`` (the 24/7 crypto lanes trade on Jan 1), so the first window of a new
  year is (Dec 31 15:00, Jan 1 15:00] CT and the average starts over.
* Beat = a completed day's profit is strictly greater than the YTD average of the days
  before it (the target it was chasing). The first day of the year has no target.
* The daily profit minimum (``DAILY_PROFIT_MIN_USD``, $100 since Tb 2026-10-01 12:45 AM CT)
  is a separate goal, shown separately.
* $100 streak: consecutive completed days (same day definition) with profit >= the minimum.
  ``TOPUP_STREAK_DAYS`` (5) in a row flags ``topup_unlocked`` for Tb's $1,000 top-up
  (a display flag only — nothing is bought, moved or sized from it).
* Nothing here changes trading, sizing or safety behaviour.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
TOPUP_STREAK_DAYS = 5  # consecutive days at/above the minimum
TOPUP_USD = 1000.0  # Tb's top-up (display only)
YEAR_RESET_DEFAULT = "01-01"  # MM-DD, America/Chicago; env/config YEAR_RESET
# None = the dividend cutoff schedule (snowball.treasury.cutoff: 2 PM before 10/2, then 3 PM CT).
DAY_END: dtime | None = None


def _definition() -> str:
    from snowball.treasury.cutoff import current_label

    h = current_label()
    return (f"YTD avg = live closed P/L per {h}\u2192{h} CT day (all lanes), every day counts "
            "incl. red/flat/no-trade days; today's running window is compared, not averaged")


DEFINITION = _definition()


def year_reset_value(value: str | None = None) -> tuple[str, bool]:
    """Normalised ``MM-DD`` and whether the configured value was valid (falls back to 01-01)."""
    raw = value if value is not None else os.environ.get("YEAR_RESET")
    raw = (raw or YEAR_RESET_DEFAULT).strip()
    try:
        m, d = (int(x) for x in raw.split("-"))
        date(2000, m, d)  # leap year so 02-29 is accepted
        return f"{m:02d}-{d:02d}", True
    except (ValueError, TypeError):
        return YEAR_RESET_DEFAULT, False


def year_start(year: int, reset: str) -> date:
    m, d = (int(x) for x in reset.split("-"))
    try:
        return date(year, m, d)
    except ValueError:  # 02-29 in a non-leap year
        return date(year, 3, 1)


def day_of(t: datetime, day_end: dtime | None = DAY_END) -> date:
    """CT end date D of the (end(D-1), end(D)] window containing *t* (cutoff schedule)."""
    from snowball.treasury.cutoff import day_of as _day_of

    return _day_of(t if t.tzinfo else t.replace(tzinfo=timezone.utc), day_end)


def _live_closes(data_dir: Path) -> Iterable[tuple[datetime, float, str]]:
    from snowball.reports.closed_pnl import LANE_DB_FILES, _closed_rows, parse_closed_at

    for lane, fn in LANE_DB_FILES:
        for closed_at, pnl, live in _closed_rows(Path(data_dir) / fn):
            t = parse_closed_at(closed_at)
            if t is None or not live:
                continue
            yield t, float(pnl or 0.0), lane


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _window_text(d: date, day_end: dtime | None) -> str:
    from snowball.treasury.cutoff import window_text

    return window_text(d, day_end)


def ytd_profit_status(data_dir: Path, now: datetime | None = None, *, year_reset: str | None = None,
                      target_usd: float | None = None, day_end: dtime | None = DAY_END,
                      history_days: int = 30, closes: Iterable[tuple[datetime, float, str]] | None = None
                      ) -> dict[str, Any]:
    """Read-only YTD average daily profit block (snapshot / PDF / plugins)."""
    if target_usd is None:
        from snowball.treasury.dca import DAILY_TARGET_USD as target_usd  # noqa: N811
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    reset, reset_ok = year_reset_value(year_reset)
    cur = day_of(now, day_end)  # the window still running
    year = cur.year
    ys = year_start(year, reset)
    if cur < ys:
        year -= 1
        ys = year_start(year, reset)
    next_reset = year_start(year + 1, reset)
    buckets: dict[date, float] = {}
    counts: dict[date, int] = {}
    for t, pnl, _lane in (closes if closes is not None else _live_closes(data_dir)):
        d = day_of(t, day_end)
        if d < ys or d > cur:
            continue
        buckets[d] = buckets.get(d, 0.0) + pnl
        counts[d] = counts.get(d, 0) + 1
    first = min(buckets) if buckets else None
    out: dict[str, Any] = {
        "label": "YTD average daily profit \u2014 beat it every day",
        "definition": DEFINITION, "day_window": f"{_window_text(cur, day_end)} (dividend window)",
        "year": year, "year_reset": reset, "year_reset_valid": reset_ok,
        "year_start": ys.isoformat(), "next_reset": next_reset.isoformat(),
        "first_day": first.isoformat() if first else None,
        "as_of_ct": now.astimezone(CT).isoformat(),
        "days": 0, "total_usd": 0.0, "avg_usd": None,
        "green_days": 0, "red_days": 0, "flat_days": 0,
        "days_beaten": 0, "days_compared": 0, "beat_streak": 0, "best_streak": 0,
        "today": None, "yesterday": None, "series": [],
        "minimum_usd": float(target_usd), "days_over_minimum": 0,
        "min_streak": 0, "best_min_streak": 0, "topup_streak_days": TOPUP_STREAK_DAYS,
        "topup_usd": TOPUP_USD, "topup_unlocked": False, "min_streak_text": "",
    }
    days: list[dict[str, Any]] = []
    total = 0.0
    if first is not None:
        d = first
        while d < cur:
            net = round(buckets.get(d, 0.0), 4)
            n = len(days)
            avg_before = (total / n) if n else None
            beat = None if avg_before is None else net > avg_before + 1e-9
            total += net
            days.append({"date": d.isoformat(), "net_usd": _r(net), "closes": counts.get(d, 0),
                         "avg_before_usd": _r(avg_before), "avg_after_usd": _r(total / (n + 1)),
                         "beat": beat, "over_minimum": net >= float(target_usd) - 1e-9})
            d += timedelta(days=1)
    n = len(days)
    avg = (total / n) if n else None
    streak = best = run = 0
    for row in days:
        run = run + 1 if row["beat"] else 0
        best = max(best, run)
    for row in reversed(days):
        if row["beat"]:
            streak += 1
        else:
            break
    mrun = mbest = mcur = 0
    for row in days:
        mrun = mrun + 1 if row["over_minimum"] else 0
        mbest = max(mbest, mrun)
    for row in reversed(days):
        if row["over_minimum"]:
            mcur += 1
        else:
            break
    unlocked = mcur >= TOPUP_STREAK_DAYS
    mtxt = (f"Streak {min(mcur, TOPUP_STREAK_DAYS)}/{TOPUP_STREAK_DAYS} toward Tb's ${TOPUP_USD:,.0f} top-up"
            + (" \u2014 UNLOCKED" if unlocked else "")
            + (f" ({mcur} days)" if mcur > TOPUP_STREAK_DAYS else "")
            + f" \u00b7 best {mbest} YTD \u00b7 ${float(target_usd):,.0f}+ days")
    out.update({
        "days": n, "total_usd": _r(total) or 0.0, "avg_usd": _r(avg),
        "green_days": sum(1 for r in days if (r["net_usd"] or 0) > 0),
        "red_days": sum(1 for r in days if (r["net_usd"] or 0) < 0),
        "flat_days": sum(1 for r in days if (r["net_usd"] or 0) == 0),
        "days_beaten": sum(1 for r in days if r["beat"]),
        "days_compared": sum(1 for r in days if r["beat"] is not None),
        "beat_streak": streak, "best_streak": best,
        "days_over_minimum": sum(1 for r in days if r["over_minimum"]),
        "min_streak": mcur, "best_min_streak": mbest, "topup_unlocked": unlocked, "min_streak_text": mtxt,
        "series": days[-max(1, int(history_days)):],
    })
    if days:
        y = days[-1]
        tgt = y["avg_before_usd"]
        out["yesterday"] = {"date": y["date"], "net_usd": y["net_usd"], "vs_usd": tgt,
                            "diff_usd": None if tgt is None else _r(y["net_usd"] - tgt), "beat": y["beat"]}
    if first is not None or avg is not None:
        t_net = round(buckets.get(cur, 0.0), 4)
        from snowball.treasury.cutoff import window as _window

        start, end = _window(cur, day_end)
        out["today"] = {"date": cur.isoformat(), "net_usd": _r(t_net), "closes": counts.get(cur, 0),
                        "window_start": start.isoformat(), "window_end": end.isoformat(),
                        "vs_usd": _r(avg), "diff_usd": None if avg is None else _r(t_net - avg),
                        "beating": None if avg is None else t_net > avg + 1e-9, "running": True,
                        "at_minimum": t_net >= float(target_usd) - 1e-9}
    goal = (f"Goal: beat the YTD average (${avg:,.2f}/day) and clear the ${float(target_usd):,.0f}/day minimum"
            if avg is not None else f"Goal: clear the ${float(target_usd):,.0f}/day minimum (YTD average starts after the first full day)")
    out["goal"] = goal + " \u2014 display only; trading, sizing and safety rules unchanged"
    return out


def vs_text(diff: float | None, beat: bool | None, *, running: bool = False) -> str:
    """'beat by $3.20' / 'missed by $4.10' / 'ahead by $1.00 so far' / '—'."""
    if diff is None or beat is None:
        return "\u2014"
    amt = f"${abs(float(diff)):,.2f}"
    if running:
        return f"ahead by {amt} so far" if beat else f"behind by {amt} so far"
    return f"beat by {amt}" if beat else f"missed by {amt}"
