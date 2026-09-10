"""FedWatch probability bucket math + entry gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

BET_SKEW_THRESHOLD = 0.70  # 70% clear skew required to bet
WINDOW_DAYS = 14

_BUCKET_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*%?\s*[-–]\s*(?P<hi>\d+(?:\.\d+)?)\s*%?"
)


def parse_bucket_bounds(label: str) -> tuple[float, float] | None:
    """Parse '3.50%-3.75%' → (3.50, 3.75)."""
    if not label:
        return None
    m = _BUCKET_RE.search(str(label).strip())
    if not m:
        return None
    lo = float(m.group("lo"))
    hi = float(m.group("hi"))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def bucket_mid(label: str) -> float | None:
    bounds = parse_bucket_bounds(label)
    if bounds is None:
        return None
    return (bounds[0] + bounds[1]) / 2.0


def compare_bucket_to_target(bucket: str, current_target: str) -> str:
    """Return 'hold' | 'hike' | 'cut' vs current Fed target range label."""
    b = parse_bucket_bounds(bucket)
    t = parse_bucket_bounds(current_target)
    if b is None or t is None:
        # Exact string match fallback
        if str(bucket).strip() == str(current_target).strip():
            return "hold"
        return "hold"
    # Equal ranges → hold; strictly higher midpoint → hike; lower → cut
    b_mid = (b[0] + b[1]) / 2.0
    t_mid = (t[0] + t[1]) / 2.0
    # Prefer exact bound equality for hold when labels match current target
    if abs(b[0] - t[0]) < 1e-9 and abs(b[1] - t[1]) < 1e-9:
        return "hold"
    if b_mid > t_mid + 1e-9:
        return "hike"
    if b_mid < t_mid - 1e-9:
        return "cut"
    return "hold"


@dataclass(frozen=True)
class MeetingProbSummary:
    meeting_date: str
    current_target: str
    p_hold: float
    p_hike: float
    p_cut: float
    dominant: str  # hold | hike | cut
    max_prob: float
    bet_eligible: bool
    direction: str | None  # long | short | None (flat)
    raw_probabilities: dict[str, float]


def normalize_prob_mass(probabilities: Mapping[str, Any]) -> dict[str, float]:
    """Convert percent (0-100) or fraction (0-1) masses to fractions summing ~1."""
    out: dict[str, float] = {}
    for k, v in (probabilities or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        out[str(k)] = fv
    if not out:
        return {}
    total = sum(out.values())
    if total <= 0:
        return {k: 0.0 for k in out}
    # CME FedWatch returns 0-100 percentages
    if total > 1.5:
        return {k: v / 100.0 for k, v in out.items()}
    return dict(out)


def classify_meeting_probs(
    probabilities: Mapping[str, Any],
    current_target: str,
    *,
    threshold: float = BET_SKEW_THRESHOLD,
) -> MeetingProbSummary:
    """Bucket FedWatch probs into hold/hike/cut vs current_target."""
    masses = normalize_prob_mass(probabilities)
    p_hold = p_hike = p_cut = 0.0
    for label, mass in masses.items():
        kind = compare_bucket_to_target(label, current_target)
        if kind == "hold":
            p_hold += mass
        elif kind == "hike":
            p_hike += mass
        else:
            p_cut += mass
    # Dominant by mass; ties prefer hold then hike then cut for safety (no bet on hold)
    ranked = sorted(
        (("hold", p_hold), ("hike", p_hike), ("cut", p_cut)),
        key=lambda x: (-x[1], {"hold": 0, "hike": 1, "cut": 2}[x[0]]),
    )
    dominant = ranked[0][0]
    max_prob = float(ranked[0][1])
    bet_eligible = max_prob + 1e-12 >= float(threshold) and dominant in ("hike", "cut")
    direction: str | None = None
    if bet_eligible:
        direction = "long" if dominant == "cut" else "short"
    # hold always flat in v1 even if >=70%
    if dominant == "hold":
        bet_eligible = False
        direction = None
    return MeetingProbSummary(
        meeting_date="",
        current_target=str(current_target),
        p_hold=p_hold,
        p_hike=p_hike,
        p_cut=p_cut,
        dominant=dominant,
        max_prob=max_prob,
        bet_eligible=bet_eligible,
        direction=direction,
        raw_probabilities={k: float(v) for k, v in masses.items()},
    )


def days_until_meeting(meeting: date | str, today: date | None = None) -> int | None:
    if isinstance(meeting, str):
        try:
            meeting = date.fromisoformat(meeting[:10])
        except ValueError:
            return None
    today = today or datetime.now(timezone.utc).date()
    return (meeting - today).days


def in_fomc_bet_window(
    meeting: date | str,
    today: date | None = None,
    *,
    window_days: int = WINDOW_DAYS,
) -> bool:
    """True from window_days before meeting through decision day (days_left >= 0)."""
    d = days_until_meeting(meeting, today=today)
    if d is None:
        return False
    return 0 <= d <= int(window_days)


def prefer_exit_after_fomc(
    meeting: date | str,
    today: date | None = None,
) -> bool:
    """True on the calendar day after FOMC (preferred green exit day)."""
    if isinstance(meeting, str):
        try:
            meeting = date.fromisoformat(meeting[:10])
        except ValueError:
            return False
    today = today or datetime.now(timezone.utc).date()
    return today == (meeting + timedelta(days=1))


def history_deltas(
    current: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    current_target: str,
) -> dict[str, float]:
    """Delta of hold/hike/cut masses vs a prior snapshot."""
    cur = classify_meeting_probs(current, current_target)
    if not prior:
        return {"d_hold": 0.0, "d_hike": 0.0, "d_cut": 0.0}
    prev = classify_meeting_probs(prior, current_target)
    return {
        "d_hold": cur.p_hold - prev.p_hold,
        "d_hike": cur.p_hike - prev.p_hike,
        "d_cut": cur.p_cut - prev.p_cut,
    }


def summarize_fedwatch_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize get_probabilities() dict into dashboard-friendly structure."""
    current_target = str(payload.get("current_target") or "")
    meetings_out: list[dict[str, Any]] = []
    for m in payload.get("meetings") or []:
        date_s = str(m.get("date") or "")
        probs = m.get("probabilities") or {}
        summary = classify_meeting_probs(probs, current_target)
        meetings_out.append(
            {
                "date": date_s,
                "contract": m.get("contract"),
                "probabilities": dict(summary.raw_probabilities),
                "p_hold": summary.p_hold,
                "p_hike": summary.p_hike,
                "p_cut": summary.p_cut,
                "dominant": summary.dominant,
                "max_prob": summary.max_prob,
                "days_left": days_until_meeting(date_s),
                "in_window": in_fomc_bet_window(date_s),
            }
        )
    next_m = meetings_out[0] if meetings_out else None
    next_summary = None
    bet_eligible = False
    direction = None
    if next_m:
        next_summary = classify_meeting_probs(
            next_m.get("probabilities") or {}, current_target
        )
        in_win = bool(next_m.get("in_window"))
        bet_eligible = bool(next_summary.bet_eligible and in_win)
        direction = next_summary.direction if bet_eligible else None
    return {
        "effr": payload.get("effr"),
        "current_target": current_target,
        "schedule_status": payload.get("schedule_status"),
        "meetings": meetings_out,
        "next_meeting": next_m,
        "p_hold": next_summary.p_hold if next_summary else None,
        "p_hike": next_summary.p_hike if next_summary else None,
        "p_cut": next_summary.p_cut if next_summary else None,
        "skew": next_summary.dominant if next_summary else None,
        "max_prob": next_summary.max_prob if next_summary else None,
        "bet_eligible": bet_eligible,
        "direction": direction,
        "in_window": bool(next_m.get("in_window")) if next_m else False,
        "days_left": next_m.get("days_left") if next_m else None,
        "next_meeting_date": next_m.get("date") if next_m else None,
    }
