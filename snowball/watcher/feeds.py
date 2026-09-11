"""Official RSS/API catalog for The Watcher.

URLs were verified with GET at build time (2026-09-02). Dead feeds are listed
in SKIPPED_FEEDS and logged at startup — never scraped as HTML.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RssFeed:
    source: str  # fed|ecb|boe|boj|treasury|worldbank
    url: str
    country: str
    label: str


# Verified 200 + RSS/XML at build:
# - federalreserve.gov/feeds/press_monetary.xml
# - federalreserve.gov/feeds/press_all.xml
# - ecb.europa.eu/rss/press.xml
# - bankofengland.co.uk/rss/news  (index /rss is HTML; /rss/news is XML)
# - boj.or.jp/en/rss/whatsnew.xml (from boj.or.jp/en/tips.htm)
# - treasurydirect.gov official RSS (needs Accept: application/rss+xml)
WATCHER_RSS_FEEDS: tuple[RssFeed, ...] = (
    RssFeed(
        "fed",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "united states",
        "Fed monetary press",
    ),
    RssFeed(
        "fed",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "united states",
        "Fed all press",
    ),
    RssFeed(
        "ecb",
        "https://www.ecb.europa.eu/rss/press.xml",
        "euro area",
        "ECB press",
    ),
    RssFeed(
        "boe",
        "https://www.bankofengland.co.uk/rss/news",
        "united kingdom",
        "BoE news",
    ),
    RssFeed(
        "boj",
        "https://www.boj.or.jp/en/rss/whatsnew.xml",
        "japan",
        "BoJ what's new",
    ),
    RssFeed(
        "treasury",
        "https://www.treasurydirect.gov/TA_WS/securities/announced/rss",
        "united states",
        "Treasury offering announcements",
    ),
    RssFeed(
        "treasury",
        "https://www.treasurydirect.gov/TA_WS/securities/auctioned/rss",
        "united states",
        "Treasury auction results",
    ),
    RssFeed(
        "treasury",
        "https://www.treasurydirect.gov/rss/mspd.xml",
        "united states",
        "Monthly Statement of the Public Debt",
    ),
)


@dataclass(frozen=True)
class SkippedFeed:
    source: str
    url: str
    reason: str


# Official news RSS 404/HTML at build. Do not scrape the World Bank HTML news page.
SKIPPED_FEEDS: tuple[SkippedFeed, ...] = (
    SkippedFeed(
        "worldbank",
        "https://www.worldbank.org/en/news/all/rss",
        "404 at build; no working official World Bank news RSS (HTML news pages are not scraped)",
    ),
    SkippedFeed(
        "treasury",
        "https://home.treasury.gov/news/press-releases/rss",
        "404 at build; using TreasuryDirect official RSS instead of treasury.gov HTML",
    ),
)

TE_COUNTRIES: tuple[str, ...] = (
    "united states",
    "euro area",
    "united kingdom",
    "japan",
    "china",
)

TE_CALENDAR_URL = "https://api.tradingeconomics.com/calendar/country/{countries}?c={key}&f=json"

FRED_SERIES: tuple[str, ...] = ("FEDFUNDS", "DGS2", "DGS10", "DFF")
FRED_OBS_URL = (
    "https://api.stlouisfed.org/fred/series/observations"
    "?series_id={series_id}&api_key={key}&file_type=json&sort_order=desc&limit=5"
)


# --- Official research sources (non-RSS; package / API backed) -----------------

CME_FEDWATCH_TOOL_URL = (
    "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
)


@dataclass(frozen=True)
class OfficialResearchSource:
    """First-class official source for The Watcher / Fed research belt.

    Probabilities are fetched via the sanctioned ``cme-fedwatch`` package (same
    path Fed Desk uses). The CME page URL is recorded for citation; brittle HTML
    scraping is not used for order signals (Watcher never places orders).
    """

    source: str
    url: str
    label: str
    fetch_path: str  # human description of sanctioned fetch path


WATCHER_OFFICIAL_SOURCES: tuple[OfficialResearchSource, ...] = (
    OfficialResearchSource(
        "cme_fedwatch",
        CME_FEDWATCH_TOOL_URL,
        "CME FedWatch Tool",
        "cme-fedwatch package get_probabilities() / Fed Desk research sidecar",
    ),
)
