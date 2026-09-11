"""Parse Nasdaq earnings calendar rows (research only)."""

from __future__ import annotations

from typing import Any, Iterable

from snowball.stocks.universe import normalize_symbol

TIME_TO_SESSION = {
    "time-pre-market": "BMO",
    "time-after-hours": "AMC",
    "time-not-supplied": "UNK",
}


def map_session(time_token: str | None) -> str:
    raw = (time_token or "").strip().lower()
    return TIME_TO_SESSION.get(raw, "UNK")


def _parse_eps(raw: object) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in {"N/A", "NA", "--", "-"}:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def _parse_ests(raw: object) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s.upper() in {"N/A", "NA", "--"}:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_calendar_payload(payload: dict[str, Any], *, report_date: str) -> list[dict[str, Any]]:
    """Normalize Nasdaq /api/calendar/earnings JSON into event dicts."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = (data or {}).get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = normalize_symbol(str(row.get("symbol") or ""))
        if not sym:
            continue
        time_tok = str(row.get("time") or "")
        out.append(
            {
                "ticker": sym,
                "name": (str(row.get("name") or "").strip() or None),
                "report_date": report_date,
                "time_token": time_tok,
                "session": map_session(time_tok),
                "eps_forecast": _parse_eps(row.get("epsForecast")),
                "no_of_ests": _parse_ests(row.get("noOfEsts")),
                "fiscal_quarter_ending": (str(row.get("fiscalQuarterEnding") or "").strip() or None),
                "market_cap": (str(row.get("marketCap") or "").strip() or None),
                "last_year_eps": _parse_eps(row.get("lastYearEPS")),
                "last_year_rpt_dt": (str(row.get("lastYearRptDt") or "").strip() or None),
            }
        )
    return out


def filter_watchlist(events: Iterable[dict[str, Any]], watch: set[str]) -> list[dict[str, Any]]:
    wl = {normalize_symbol(s) for s in watch}
    return [e for e in events if normalize_symbol(str(e.get("ticker") or "")) in wl]
