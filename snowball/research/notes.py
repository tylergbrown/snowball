"""Extractive notes for a transcribed video (Tb 3:45 AM CT 10/3).

No LLM is available in the container, so notes are VERBATIM sentences picked from the
transcript (nothing is paraphrased or invented): sentences are scored by economic /
market vocabulary, numbers and the tickers the stance extractor found; sponsor / outro
filler is skipped. The top sentences are returned in transcript order.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

ECON_TERMS = {
    # macro
    "inflation": 2.0, "cpi": 2.0, "pce": 2.0, "fed": 2.0, "federal reserve": 2.0, "fomc": 2.0, "powell": 1.5,
    "rate cut": 2.5, "rate cuts": 2.5, "rate hike": 2.5, "rate hikes": 2.5, "interest rate": 2.0,
    "interest rates": 2.0, "rates": 1.0, "yield": 1.5, "yields": 1.5, "treasury": 1.5, "bond": 1.2,
    "bonds": 1.2, "recession": 2.0, "gdp": 2.0, "jobs": 1.2, "unemployment": 1.8, "payrolls": 1.8,
    "labor market": 1.8, "tariff": 1.8, "tariffs": 1.8, "deficit": 1.5, "debt": 1.0, "liquidity": 1.8,
    "dollar": 1.2, "dxy": 1.5, "oil": 1.5, "crude": 1.5, "diesel": 1.2, "gold": 1.2, "housing": 1.2,
    "earnings": 1.5, "guidance": 1.5, "revenue": 1.2, "margins": 1.0, "valuation": 1.0, "stimulus": 1.5,
    "qe": 1.5, "quantitative": 1.5, "balance sheet": 1.5, "economy": 1.2, "consumer": 1.0, "credit": 1.0,
    "regulation": 1.0, "etf": 1.0, "sec": 1.0, "clarity act": 1.5,
    # market / trade
    "s&p": 1.5, "nasdaq": 1.5, "dow": 1.0, "bitcoin": 1.2, "ethereum": 1.2, "crypto": 1.0, "altcoins": 1.0,
    "bullish": 1.5, "bearish": 1.5, "support": 1.2, "resistance": 1.2, "breakout": 1.2, "target": 1.0,
    "price target": 1.5, "all-time high": 1.2, "correction": 1.2, "sell-off": 1.2, "rally": 1.2,
    "buy": 0.6, "sell": 0.6, "short": 0.6, "long": 0.4, "risk": 0.8, "volatility": 1.0, "momentum": 0.8,
}
FILLER = re.compile(
    r"\b(subscribe|like button|link (is )?(in|down) (the )?(below|description)|patreon|discord|sponsor|"
    r"promo code|use code|merch|telegram|newsletter|notification bell|smash|giveaway|affiliate|"
    r"not financial advice|thanks for watching|see you (in the next|next time))\b", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'$])")
_NUM = re.compile(r"(\$\s?\d|\d+(\.\d+)?\s?(%|percent|basis points|bps|k\b|trillion|billion|million))", re.I)


def sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return []
    parts = _SPLIT.split(t)
    if len(parts) <= 2 and len(t) > 600:  # unpunctuated captions: fixed word windows
        words = t.split(" ")
        parts = [" ".join(words[i:i + 30]) for i in range(0, len(words), 30)]
    return [p.strip() for p in parts if p.strip()]


def _score(sent: str, tickers: Iterable[str]) -> float:
    low = sent.lower()
    sc = 0.0
    for term, w in ECON_TERMS.items():
        if (" " in term or "&" in term) and term in low:
            sc += w
        elif re.search(rf"\b{re.escape(term)}\b", low):
            sc += w
    if _NUM.search(sent):
        sc += 1.0
    for t in tickers:
        if t and re.search(rf"\b{re.escape(t)}\b", sent):
            sc += 1.0
    n = len(sent.split())
    if n < 8:
        sc *= 0.3
    elif n > 60:
        sc *= 0.7
    return sc


def extract_notes(text: str, ideas: list[dict[str, Any]] | None = None, max_sentences: int = 3,
                  max_chars: int = 240) -> list[str]:
    """Top verbatim sentences (transcript order). Empty list when nothing scores."""
    ss = sentences(text)
    if not ss:
        return []
    tickers = [str(i.get("ticker") or "") for i in (ideas or []) if isinstance(i, dict)]
    scored = [(i, _score(s, tickers)) for i, s in enumerate(ss) if not FILLER.search(s)]
    top = sorted((x for x in scored if x[1] >= 2.0), key=lambda x: (-x[1], x[0]))[:max_sentences]
    if not top:  # short clip: best single sentence with any market vocabulary
        top = sorted((x for x in scored if x[1] >= 1.0), key=lambda x: (-x[1], x[0]))[:1]
    out = []
    for i, _sc in sorted(top):
        s = ss[i]
        out.append(s if len(s) <= max_chars else s[:max_chars - 1].rstrip() + "…")
    return out


def stance_line(ideas: list[dict[str, Any]] | None) -> str:
    parts = []
    for i in ideas or []:
        if not isinstance(i, dict) or not i.get("ticker"):
            continue
        b, r = int(i.get("bull") or 0), int(i.get("bear") or 0)
        if b or r:
            parts.append(f"{i['ticker']} {i.get('stance') or ''} {b}/{r}".replace("  ", " "))
    return ", ".join(parts)
