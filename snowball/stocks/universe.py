"""HOT stock paper universe: index tops + core/chip overlay + Yolo Demon dynamic.

Sources (do not invent weights):
- Top 25 SPY holdings by weight — stockanalysis.com/etf/spy/holdings (as of 2026-08-19)
- Top 25 QQQ holdings by weight — stockanalysis.com/etf/qqq/holdings (as of 2026-08-27)
  QQQ tracks Nasdaq-100; used as the Nasdaq-100 weight proxy.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from snowball.yolo_demon.tickers import CRYPTO, is_crypto

log = logging.getLogger("snowball.stocks.universe")

# --- Index tops (public ETF holdings; BRK.B normalized to BRK-B for Yahoo) ---

TOP25_SPY: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "AVGO",
    "GOOG",
    "META",
    "MU",
    "LLY",
    "TSLA",
    "JPM",
    "BRK-B",
    "AMD",
    "XOM",
    "JNJ",
    "V",
    "WMT",
    "ABBV",
    "MA",
    "INTC",
    "CSCO",
    "COST",
    "BAC",
    "PLTR",
)

TOP25_QQQ: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "MSFT",
    "MU",
    "AMZN",
    "AMD",
    "GOOGL",
    "GOOG",
    "TSLA",
    "AVGO",
    "META",
    "WMT",
    "INTC",
    "CSCO",
    "PLTR",
    "COST",
    "LRCX",
    "AMAT",
    "NFLX",
    "PANW",
    "SPCX",
    "TXN",
    "KLAC",
    "AMGN",
    "CRWD",
)

# Prior core liquid + ETFs requested for the paper trial
STATIC_CORE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "SPCX",
)

# Chip / AI overlay (keep even if not in top-25 that week)
CHIP_AI: tuple[str, ...] = (
    "AMD",
    "AVGO",
    "SMCI",
    "TSM",
    "ARM",
    "PLTR",
    "SOUN",
    "AI",
)

# Skip synthetic / non-equity symbols from Yolo Demon
_SKIP_DYNAMIC = frozenset({"WATCH", "SPX", "NDX", "VIX"}) | CRYPTO

# Yahoo / paper symbol aliases
_YAHOO_ALIAS = {
    "BRK.B": "BRK-B",
    "BRK/B": "BRK-B",
}


def normalize_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace(" ", "")
    return _YAHOO_ALIAS.get(s, s)


def static_universe() -> list[str]:
    """Deduped static list: core → chip/AI → SPY top25 → QQQ top25."""
    out: list[str] = []
    seen: set[str] = set()
    for bucket in (STATIC_CORE, CHIP_AI, TOP25_SPY, TOP25_QQQ):
        for raw in bucket:
            sym = normalize_symbol(raw)
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
    return out


def _eligible_dynamic(sym: str) -> bool:
    sym = normalize_symbol(sym)
    if not sym or sym in _SKIP_DYNAMIC:
        return False
    if is_crypto(sym):
        return False
    if sym == "WATCH":
        return False
    # Require 1–5 letter tickers (BRK-B allowed via hyphen)
    body = sym.replace("-", "")
    if not body.isalpha() or not (1 <= len(body) <= 5):
        return False
    return True


def dynamic_from_yolo(
    yolo_store: Any | None,
    *,
    max_dynamic: int = 30,
    video_limit: int = 80,
) -> list[str]:
    """Top mention-frequency equity tickers from Yolo Demon priority videos / ideas."""
    if yolo_store is None or max_dynamic <= 0:
        return []
    counts: Counter[str] = Counter()
    try:
        for vid in yolo_store.recent_videos(video_limit):
            for t in vid.get("tickers") or []:
                sym = normalize_symbol(str(t))
                if _eligible_dynamic(sym):
                    counts[sym] += 1
    except Exception:  # noqa: BLE001 — universe must not crash the lane
        log.exception("yolo recent_videos failed for stock universe")
    try:
        for idea in yolo_store.top_ideas(40):
            sym = normalize_symbol(getattr(idea, "ticker", "") or "")
            if not _eligible_dynamic(sym):
                continue
            if getattr(idea, "is_crypto", False):
                continue
            # Weight ideas by mentions_24h when present
            w = int(getattr(idea, "mentions_24h", 0) or 0) or 1
            counts[sym] += w
    except Exception:  # noqa: BLE001
        log.exception("yolo top_ideas failed for stock universe")
    ranked = [s for s, _ in counts.most_common(max_dynamic)]
    return ranked


def build_stock_universe(
    yolo_store: Any | None = None,
    *,
    max_dynamic: int = 30,
    max_active: int | None = 60,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    """Build HOT watchlist.

    Returns dict with:
      symbols — full mark universe (static + dynamic + extras), deduped
      active — trading subset (first max_active of symbols) when capped
      static / dynamic / sources metadata
    """
    static = static_universe()
    dynamic = dynamic_from_yolo(yolo_store, max_dynamic=max_dynamic)
    extras = [normalize_symbol(x) for x in (extra or []) if normalize_symbol(x)]

    symbols: list[str] = []
    seen: set[str] = set()
    for sym in static + dynamic + extras:
        if sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)

    active = symbols if max_active is None else symbols[: max(1, int(max_active))]
    return {
        "symbols": symbols,
        "active": active,
        "static": static,
        "dynamic": [s for s in dynamic if s not in set(static)],
        "sources": {
            "spy_top25": "stockanalysis.com/etf/spy/holdings as of 2026-08-19",
            "qqq_top25": "stockanalysis.com/etf/qqq/holdings as of 2026-08-27 (Nasdaq-100 proxy)",
            "core": list(STATIC_CORE),
            "chip_ai": list(CHIP_AI),
            "yolo_dynamic_cap": max_dynamic,
            "max_active": max_active,
        },
    }


def universe_json(meta: dict[str, Any]) -> str:
    return json.dumps(meta, sort_keys=True)
