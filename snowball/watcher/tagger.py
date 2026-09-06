"""Conservative keyword tagger for The Watcher. No NLP sentiment, no trading."""

from __future__ import annotations

import re

# tag -> substrings / regexes matched against title + summary (case-insensitive)
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fomc", ("fomc", "federal open market", "fomc statement", "fomc minutes")),
    (
        "rate_decision",
        (
            "interest rate decision",
            "rate decision",
            "bank rate",
            "policy rate",
            "fed funds",
            "federal funds rate",
            "funds rate",
            "mpc decision",
            "refi rate",
            "deposit facility rate",
        ),
    ),
    ("cpi", (" cpi", "cpi ", "consumer price", "inflation rate", "pce price", "core pce")),
    ("nfp", ("nonfarm", "non-farm", "nfp", "payrolls", "employment situation")),
    ("treasury", ("treasury", "t-bill", "t-note", "t-bond", "auction announcement", "auction results", "public debt")),
    ("worldbank", ("world bank", "ibrd", "ida ")),
    ("qe", ("quantitative easing", " asset purchase", "qe ", "qe,")),
    ("qt", ("quantitative tightening", "balance sheet runoff", "qt ", "qt,")),
)

_WORD_BOUND_PREFIX = re.compile(r"^(cpi|nfp|qe|qt)$")


def tag_text(*parts: str | None) -> list[str]:
    blob = " ".join(p for p in parts if p).lower()
    if not blob:
        return []
    padded = f" {blob} "
    tags: list[str] = []
    for tag, needles in _RULES:
        for needle in needles:
            if needle in padded or needle in blob:
                tags.append(tag)
                break
    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def te_event_tags(event_name: str, category: str | None = None) -> list[str]:
    tags = tag_text(event_name, category)
    name = (event_name or "").lower()
    cat = (category or "").lower()
    joined = f"{name} {cat}"
    if "fomc" in joined or "fed interest rate" in joined or "federal reserve" in joined and "rate" in joined:
        if "fomc" not in tags:
            tags.append("fomc")
    if "interest rate" in joined or "rate decision" in joined:
        if "rate_decision" not in tags:
            tags.append("rate_decision")
    return tags
