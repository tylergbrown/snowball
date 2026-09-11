"""Watchlist for Earnings Scout — reuse Snowball stock universe static list."""

from __future__ import annotations

from typing import Iterable

from snowball.stocks.universe import normalize_symbol, static_universe


def earnings_watchlist(extra: Iterable[str] | None = None) -> set[str]:
    """Static HOT universe (+ optional extras). Research filter only."""
    out = {normalize_symbol(s) for s in static_universe()}
    for raw in extra or []:
        sym = normalize_symbol(raw)
        if sym:
            out.add(sym)
    return out


def in_watchlist(symbol: str, watch: set[str] | None = None) -> bool:
    wl = watch if watch is not None else earnings_watchlist()
    return normalize_symbol(symbol) in wl
