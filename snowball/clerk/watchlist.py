"""Priority House members. Still ingest other P filings; download these PDFs first."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Nicknames seen on Clerk index First fields vs common short names.
_FIRST_EQUIV = {
    "mike": "michael",
    "michael": "michael",
    "tim": "timothy",
    "timothy": "timothy",
    "steve": "steven",
    "steven": "steven",
    "rob": "robert",
    "robert": "robert",
}


@dataclass(frozen=True)
class WatchName:
    last: str
    first: str
    state_dst: str | None = None


# Nancy Pelosi (CA11) is required. Match Last + First loosely; Hon. is a Prefix.
WATCHLIST: tuple[WatchName, ...] = (
    WatchName("Pelosi", "Nancy", "CA11"),
    WatchName("Gottheimer", "Josh"),
    WatchName("McCaul", "Michael"),
    WatchName("Khanna", "Rohit"),
    WatchName("Peters", "Scott"),
    WatchName("Cohen", "Steve"),
    WatchName("Hern", "Kevin"),
    WatchName("DelBene", "Suzan"),
    WatchName("Kelly", "Mike"),
    WatchName("Cisneros", "Gilbert"),
    WatchName("Harshbarger", "Diana"),
    WatchName("Fields", "Cleo"),
    WatchName("Morrison", "Kelly"),
    WatchName("Taylor", "David"),
    WatchName("Moore", "Tim"),
)


def watchlist_labels() -> list[str]:
    out: list[str] = []
    for spec in WATCHLIST:
        label = f"{spec.first} {spec.last}"
        if spec.state_dst:
            label = f"{label} ({spec.state_dst})"
        out.append(label)
    return out


def _norm_token(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def _first_tokens(first: str) -> list[str]:
    cleaned = re.sub(r"(?i)^hon\.?\s+", "", first or "").strip()
    return [t for t in (_norm_token(p) for p in cleaned.replace(".", " ").split()) if t]


def _first_ok(filing_first: str, spec_first: str) -> bool:
    filing = _first_tokens(filing_first)
    spec = _first_tokens(spec_first)
    if not spec:
        return True
    if not filing:
        return False
    a = filing[0]
    b = spec[0]
    if a == b:
        return True
    if _FIRST_EQUIV.get(a, a) == _FIRST_EQUIV.get(b, b):
        return True
    # "Kelly" matches "Kelly Louise"; "Michael" matches "Michael T."
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def _match_spec(last: str, first: str, state_dst: str | None) -> WatchName | None:
    last_n = _norm_token(last or "")
    if not last_n:
        return None
    dst = re.sub(r"[^A-Z0-9]", "", (state_dst or "").upper())
    for spec in WATCHLIST:
        if _norm_token(spec.last) != last_n:
            continue
        if spec.state_dst and dst and dst != spec.state_dst.upper():
            continue
        if _first_ok(first or "", spec.first):
            return spec
    return None


def is_watchlist(last: str, first: str, state_dst: str | None = None) -> bool:
    return _match_spec(last, first, state_dst) is not None


def watch_rank(last: str, first: str, state_dst: str | None = None) -> int:
    """0 = required Pelosi, 1 = other watchlist, 2 = everyone else. Used for PDF order."""
    spec = _match_spec(last, first, state_dst)
    if spec is None:
        return 2
    if _norm_token(spec.last) == "pelosi" and spec.state_dst == "CA11":
        return 0
    return 1
