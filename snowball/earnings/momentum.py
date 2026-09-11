"""Pre-momentum and post-reaction returns from Yahoo daily closes.

Heuristic research labels only — NEVER an order signal.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

FLAT_BAND = 0.005  # ±0.5% treated as flat for pre_momentum label


def parse_yahoo_closes(payload: dict[str, Any]) -> list[tuple[date, float]]:
    """Extract (trade_date, close) ascending from Yahoo v8 chart JSON."""
    chart = payload.get("chart") if isinstance(payload, dict) else None
    result = (chart or {}).get("result") if isinstance(chart, dict) else None
    if not result:
        return []
    block = result[0] if isinstance(result, list) and result else None
    if not isinstance(block, dict):
        return []
    stamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    out: list[tuple[date, float]] = []
    for ts, cl in zip(stamps, closes):
        if cl is None:
            continue
        try:
            px = float(cl)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        out.append((dt, px))
    out.sort(key=lambda x: x[0])
    return out


def _ret(latest: float, earlier: float) -> float | None:
    if earlier <= 0 or latest <= 0:
        return None
    return (latest / earlier) - 1.0


def pre_momentum(closes: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """ret_5d / ret_20d into the latest close; heuristic pre_momentum label."""
    if len(closes) < 2:
        return {
            "ret_5d": None,
            "ret_20d": None,
            "pre_momentum": "flat",
            "asof_date": None,
            "last_close": None,
        }
    asof, last = closes[-1]
    ret5 = _ret(last, closes[-6][1]) if len(closes) >= 6 else None
    ret20 = _ret(last, closes[-21][1]) if len(closes) >= 21 else None
    # Prefer 5d for the label when present; else 20d.
    anchor = ret5 if ret5 is not None else ret20
    if anchor is None:
        label = "flat"
    elif anchor > FLAT_BAND:
        label = "up"
    elif anchor < -FLAT_BAND:
        label = "down"
    else:
        label = "flat"
    return {
        "ret_5d": ret5,
        "ret_20d": ret20,
        "pre_momentum": label,
        "asof_date": asof.isoformat(),
        "last_close": last,
    }


def _index_on_or_before(closes: Sequence[tuple[date, float]], day: date) -> int | None:
    idx = None
    for i, (d, _) in enumerate(closes):
        if d <= day:
            idx = i
        else:
            break
    return idx


def _index_on_or_after(closes: Sequence[tuple[date, float]], day: date) -> int | None:
    for i, (d, _) in enumerate(closes):
        if d >= day:
            return i
    return None


def post_reaction(
    closes: Sequence[tuple[date, float]],
    *,
    report_date: date,
    session: str,
) -> dict[str, Any]:
    """Post-print returns vs session-appropriate anchor.

    BMO: anchor = close before print day (prior session close).
    AMC/UNK: anchor = close on print day (reaction starts next session).
    """
    empty = {
        "ret_0d": None,
        "ret_1d": None,
        "ret_5d": None,
        "anchor_date": None,
        "anchor_close": None,
    }
    if not closes:
        return empty
    sess = (session or "UNK").upper()
    if sess == "BMO":
        # Prior close before report_date
        print_idx = _index_on_or_after(closes, report_date)
        if print_idx is None or print_idx < 1:
            return empty
        anchor_idx = print_idx - 1
        # ret_0d = print-day close vs prior close
        # ret_1d = next day vs prior close
        # ret_5d = +5 trading days from print vs prior
        base_i = print_idx
    else:
        # AMC / UNK: anchor is close ON print day
        anchor_idx = _index_on_or_before(closes, report_date)
        if anchor_idx is None:
            return empty
        # Prefer exact print day when present
        if closes[anchor_idx][0] != report_date:
            # No bar on print day yet — cannot compute reaction
            return empty
        base_i = anchor_idx + 1  # first reaction bar is next session

    anchor_date, anchor_px = closes[anchor_idx]
    out = {
        "ret_0d": None,
        "ret_1d": None,
        "ret_5d": None,
        "anchor_date": anchor_date.isoformat(),
        "anchor_close": anchor_px,
    }

    def _at_offset(off: int) -> float | None:
        j = base_i + off
        if j < 0 or j >= len(closes):
            return None
        return _ret(closes[j][1], anchor_px)

    if sess == "BMO":
        # off 0 = print day close
        out["ret_0d"] = _at_offset(0)
        out["ret_1d"] = _at_offset(1)
        out["ret_5d"] = _at_offset(5)
    else:
        # AMC: no same-session close reaction; ret_0d = first session after print
        out["ret_0d"] = _at_offset(0)
        out["ret_1d"] = _at_offset(1)
        out["ret_5d"] = _at_offset(5)
    return out
