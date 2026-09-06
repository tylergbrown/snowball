"""Conservative ticker extraction for Yolo Demon. No sentiment trading."""

from __future__ import annotations

import re

# 1-2 letter tokens are ignored unless in this set
KNOWN_SHORT = frozenset({"SPY", "QQQ"})
CRYPTO = frozenset({"BTC", "ETH", "DOGE", "SOL"})
WSB_COMMON = frozenset(
    {
        "GME",
        "AMC",
        "NVDA",
        "TSLA",
        "AAPL",
        "MSFT",
        "AMD",
        "META",
        "GOOG",
        "GOOGL",
        "AMZN",
        "PLTR",
        "NFLX",
        "BABA",
        "SOFI",
        "HOOD",
        "COIN",
        "MSTR",
        "SMCI",
        "IWM",
        "DIA",
        "TQQQ",
        "SQQQ",
        "UVXY",
        "SPX",
        "NDX",
        "VIX",
        "RIVN",
        "NIO",
        "LCID",
        "INTC",
        "MU",
        "ARM",
        "AVGO",
        "TSM",
        "SNAP",
        "UBER",
        "ABNB",
        "SHOP",
        "SQ",
        "PYPL",
        "DIS",
        "BA",
        "JPM",
        "GS",
        "XOM",
        "CVX",
        "NKE",
        "WMT",
        "COST",
        "UNH",
        "LLY",
        "JNJ",
        "PEP",
        "KO",
        "NVAX",
        "BB",
        "NOK",
        "DKNG",
        "ROKU",
        "RBLX",
        "SOXX",
        "ARKK",
        "SPY",
        "QQQ",
        "BTC",
        "ETH",
        "DOGE",
        "SOL",
    }
)

# Cashtag $TSLA / $SPY (1-5 letters)
_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
# Standalone ALLCAPS 2-5 (we filter)
_STANDALONE = re.compile(r"\b([A-Z]{2,5})\b")

_NOISE = frozenset(
    {
        "THE",
        "AND",
        "FOR",
        "ARE",
        "BUT",
        "NOT",
        "YOU",
        "ALL",
        "CAN",
        "THIS",
        "THAT",
        "WITH",
        "FROM",
        "HAVE",
        "WILL",
        "JUST",
        "WHAT",
        "WHEN",
        "YOUR",
        "THEY",
        "BEEN",
        "WERE",
        "MORE",
        "SOME",
        "INTO",
        "THAN",
        "THEM",
        "THEN",
        "ALSO",
        "ONLY",
        "OVER",
        "SUCH",
        "WSB",
        "YOLO",
        "HOLD",
        "HODL",
        "MOON",
        "PUMP",
        "DUMP",
        "GAIN",
        "LOSS",
        "LOSS",
        "CEO",
        "CFO",
        "IPO",
        "ATH",
        "ATL",
        "IMO",
        "TBH",
        "LOL",
        "WTF",
        "USA",
        "USD",
        "EPS",
        "CEO",
        "SEC",
        "FED",
        "DD",
        "TA",
        "EU",
        "UK",
        "AI",
        "IT",
        "AM",
        "PM",
        "ET",
        "PT",
        "EST",
        "PST",
        "UTC",
        "CEO",
        "OTM",
        "ITM",
        "ATM",
        "LEAP",
        "LEAPS",
        "CALL",
        "PUTS",
        "PUT",
        "BULL",
        "BEAR",
        "RH",
        "IRA",
        "ROTH",
        "FOMO",
        "FUD",
        "NFT",
        "APR",
        "JAN",
        "FEB",
        "MAR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
        "MON",
        "TUE",
        "WED",
        "THU",
        "FRI",
        "NEWS",
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "VOLUME",
        "SHORT",
        "LONG",
        "EDIT",
        "TLDR",
        "ELI5",
        "STONK",
        "STONKS",
        "TENDIES",
        "APE",
        "APES",
        "DIAMOND",
        "HANDS",
        "PAPER",
    }
)


def is_crypto(ticker: str) -> bool:
    return ticker.upper() in CRYPTO


def extract_tickers(*parts: str | None) -> list[str]:
    blob = " ".join(p for p in parts if p)
    if not blob:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(sym: str, *, from_cashtag: bool) -> None:
        sym = sym.upper()
        if sym in seen or sym in _NOISE:
            return
        allowed_short = KNOWN_SHORT | CRYPTO | WSB_COMMON
        if len(sym) <= 2 and sym not in allowed_short:
            return
        if not from_cashtag and len(sym) >= 3 and sym not in allowed_short:
            return
        seen.add(sym)
        found.append(sym)

    for m in _CASHTAG.finditer(blob):
        add(m.group(1), from_cashtag=True)

    allowed = WSB_COMMON | KNOWN_SHORT | CRYPTO
    for m in _STANDALONE.finditer(blob):
        sym = m.group(1).upper()
        if sym in allowed:
            add(sym, from_cashtag=False)

    return found
