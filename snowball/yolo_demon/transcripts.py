"""Yolo Demon transcript reader (2026-09-30, Tb approved).

Fetches YouTube captions for every priority-channel video (poll + backfill) with
``youtube-transcript-api`` (pure Python, baked into the image), stores a trimmed
transcript with a status (ok / none / blocked / error), and extracts tickers +
stance context from the spoken text.

Safety / politeness
* Runs in its own daemon thread (``snowball-yolo-transcripts``): never blocks the
  trading loop, never touches Coinbase.
* One fetch at a time, ``YOLO_TRANSCRIPT_DELAY_SECONDS`` between fetches, and a
  global hourly cap.
* 429 / RequestBlocked / IpBlocked => circuit breaker: log ONCE per episode, mark
  the video ``blocked`` (retried later), pause ALL fetching with exponential
  backoff (1h, 2h, 4h ... max 24h). No hammering.

Research signal only
* Transcript tickers merge into the SAME idea schema (source ``youtube``, one
  mention per (video, ticker) — the UNIQUE(source, ticker, post_id) key means a
  ticker already found in the title is not double counted). No sizing or
  trading-aggressiveness change: Yolo ideas still only feed the paper stock
  watchlist and the dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from snowball.yolo_demon.tickers import CRYPTO, KNOWN_SHORT, WSB_COMMON, _NOISE

log = logging.getLogger("snowball.yolo_demon.transcripts")

STATUS_OK = "ok"
STATUS_NONE = "none"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"

MAX_STORED_CHARS = 40_000
MAX_TICKERS_PER_VIDEO = 10
WINDOW_WORDS = 12

# Tickers that are ordinary English words (or read like them in speech). Never
# matched as bare ALLCAPS tokens in transcripts; only via $CASHTAG or company name.
ENGLISH_WORD_TICKERS = frozenset(
    {
        "A", "I", "IT", "ALL", "NOW", "ARE", "BIG", "ONE", "OPEN", "AI", "ARM", "COIN",
        "HOOD", "SHOP", "SNAP", "COST", "ON", "SO", "BE", "DO", "GO", "AN", "AM", "OR",
        "KEY", "CAT", "LOW", "NET", "REAL", "FAST", "TRUE", "PLAY", "RUN", "SAFE", "BUY",
        "CASH", "FUN", "LOVE", "PEAK", "WELL", "CAR", "TELL", "MAIN", "NICE", "BEST",
        "GOOD", "HAS", "HE", "ME", "WE", "US", "UP", "OUT", "SEE", "NEW", "OLD", "BIT",
        "EAT", "FLY", "JOB", "LIFE", "MOVE", "NEXT", "PAY", "RIDE", "SAVE", "TECH", "WAY",
        "WIN", "WOW", "YOU", "DIS", "KO", "BA", "MU", "GS", "SQ", "BB", "DD", "TA", "PT",
        "ET", "EU", "UK", "IRS", "CEO", "USA", "GDP", "CPI", "FED", "SEC", "ETF", "IPO",
        "EPS", "ATH", "EV", "EVS", "DIA", "SOL",
    }
)

# Spoken company / coin names -> ticker (only kept if the ticker is whitelisted).
# Case-sensitive (captions are properly cased); ambiguous words need a capital.
NAME_ALIASES: dict[str, str] = {
    "Nvidia": "NVDA", "NVIDIA": "NVDA", "Tesla": "TSLA", "Apple": "AAPL",
    "Microsoft": "MSFT", "Amazon": "AMZN", "Alphabet": "GOOGL", "Google": "GOOGL",
    "Meta Platforms": "META", "Palantir": "PLTR", "Netflix": "NFLX", "Broadcom": "AVGO",
    "Taiwan Semiconductor": "TSM", "TSMC": "TSM", "Intel": "INTC", "Micron": "MU",
    "Coinbase": "COIN", "Robinhood": "HOOD", "MicroStrategy": "MSTR", "Super Micro": "SMCI",
    "Supermicro": "SMCI", "SoFi": "SOFI", "Rivian": "RIVN", "Lucid Motors": "LCID",
    "Uber": "UBER", "Airbnb": "ABNB", "Shopify": "SHOP", "PayPal": "PYPL", "Disney": "DIS",
    "Boeing": "BA", "JPMorgan": "JPM", "JP Morgan": "JPM", "Goldman Sachs": "GS",
    "Exxon": "XOM", "ExxonMobil": "XOM", "Chevron": "CVX", "Nike": "NKE", "Walmart": "WMT",
    "Costco": "COST", "UnitedHealth": "UNH", "Eli Lilly": "LLY", "Johnson & Johnson": "JNJ",
    "PepsiCo": "PEP", "Coca-Cola": "KO", "Roblox": "RBLX", "Roku": "ROKU",
    "DraftKings": "DKNG", "Snapchat": "SNAP", "Alibaba": "BABA", "Nio": "NIO",
    "GameStop": "GME", "AMC Entertainment": "AMC", "Novavax": "NVAX", "BlackBerry": "BB",
    "Nokia": "NOK", "Cisco": "CSCO", "Lam Research": "LRCX", "Applied Materials": "AMAT",
    "Palo Alto Networks": "PANW", "Texas Instruments": "TXN", "KLA": "KLAC",
    "Amgen": "AMGN", "CrowdStrike": "CRWD", "SoundHound": "SOUN", "C3.ai": "AI",
    "Arm Holdings": "ARM", "Oracle": "ORCL", "Salesforce": "CRM", "Adobe": "ADBE",
    "Bitcoin": "BTC", "bitcoin": "BTC", "Ethereum": "ETH", "ethereum": "ETH",
    "Solana": "SOL", "Dogecoin": "DOGE", "dogecoin": "DOGE",
}

BULL_TERMS = (
    "buy", "buying", "bought", "long", "bullish", "calls", "undervalued", "accumulate",
    "accumulating", "breakout", "upside", "rally", "adding", "loading", "load up",
    "going higher", "outperform", "cheap", "strong buy", "price target",
)
BEAR_TERMS = (
    "sell", "selling", "sold", "short", "shorting", "bearish", "puts", "overvalued",
    "avoid", "downside", "trim", "trimming", "dump", "crash", "stay away", "bubble",
    "underperform", "breakdown",
)

_CASHTAG = re.compile(r"\$([A-Za-z]{2,5})\b")
_UPPER = re.compile(r"(?<![A-Za-z$])([A-Z]{2,5})(?![A-Za-z])")
_WORD = re.compile(r"[A-Za-z$][A-Za-z0-9$.'&-]*")


def ticker_whitelist(settings: Any | None = None) -> frozenset[str]:
    """WSB list + crypto the bot trades + the stock lane's static US universe."""
    wl = set(WSB_COMMON) | set(KNOWN_SHORT) | set(CRYPTO)
    try:
        from snowball.stocks.universe import static_universe

        wl |= {s for s in static_universe() if s.isalpha()}
    except Exception:  # noqa: BLE001
        pass
    if settings is not None:
        try:
            for p in getattr(settings, "product_list", []) or []:
                base = str(p).split("-")[0].upper()
                if base.isalpha():
                    wl.add(base)
        except Exception:  # noqa: BLE001
            pass
    return frozenset(wl)


@dataclass
class TickerIdea:
    ticker: str
    hits: int = 0
    bull: int = 0
    bear: int = 0
    cashtag: bool = False
    snippet: str = ""

    @property
    def stance(self) -> str:
        if self.bull and self.bear:
            return "bullish" if self.bull >= 2 * self.bear else ("bearish" if self.bear >= 2 * self.bull else "mixed")
        if self.bull:
            return "bullish"
        if self.bear:
            return "bearish"
        return "neutral"

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "hits": self.hits, "bull": self.bull, "bear": self.bear,
                "stance": self.stance, "cashtag": self.cashtag, "snippet": self.snippet}


def _alias_regex(aliases: dict[str, str], wl: frozenset[str]) -> re.Pattern[str] | None:
    names = sorted((n for n, t in aliases.items() if t in wl), key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z])")


def extract_transcript_ideas(
    text: str,
    whitelist: Iterable[str] | None = None,
    *,
    max_tickers: int = MAX_TICKERS_PER_VIDEO,
) -> list[TickerIdea]:
    """Tickers from spoken text with stance context. Conservative by design.

    Kept when: a $CASHTAG (not noise/English), or >=2 hits, or 1 hit with
    buy/sell/long/short/bullish/bearish-type context within ±12 words.
    """
    if not text:
        return []
    wl = frozenset(whitelist) if whitelist is not None else ticker_whitelist()
    words = [(m.start(), m.group(0)) for m in _WORD.finditer(text)]
    starts = [w[0] for w in words]
    lower_words = [w[1].lower().strip(".,'") for w in words]
    ideas: dict[str, TickerIdea] = {}

    def word_index(pos: int) -> int:
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= pos:
                lo = mid + 1
            else:
                hi = mid
        return max(0, lo - 1)

    def context(pos: int) -> tuple[int, int, str]:
        i = word_index(pos)
        a, b = max(0, i - WINDOW_WORDS), min(len(words), i + WINDOW_WORDS + 1)
        win = " " + " ".join(lower_words[a:b]) + " "
        bull = sum(win.count(f" {t} ") for t in BULL_TERMS)
        bear = sum(win.count(f" {t} ") for t in BEAR_TERMS)
        snippet = " ".join(w[1] for w in words[a:b])
        return bull, bear, snippet[:240]

    def hit(sym: str, pos: int, *, cashtag: bool) -> None:
        idea = ideas.setdefault(sym, TickerIdea(sym))
        idea.hits += 1
        idea.cashtag = idea.cashtag or cashtag
        b, s, snip = context(pos)
        idea.bull += b
        idea.bear += s
        if not idea.snippet or (b + s and not (idea.bull - b + idea.bear - s)):
            idea.snippet = snip

    for m in _CASHTAG.finditer(text):
        sym = m.group(1).upper()
        if sym in _NOISE or sym in ENGLISH_WORD_TICKERS and sym not in wl:
            continue
        hit(sym, m.start(), cashtag=True)
    for m in _UPPER.finditer(text):
        sym = m.group(1)
        if sym in wl and sym not in ENGLISH_WORD_TICKERS and sym not in _NOISE:
            hit(sym, m.start(), cashtag=False)
    rx = _alias_regex(NAME_ALIASES, wl)
    if rx is not None:
        for m in rx.finditer(text):
            hit(NAME_ALIASES[m.group(1)], m.start(), cashtag=False)

    kept = [
        i for i in ideas.values()
        if i.cashtag or i.hits >= 2 or (i.hits >= 1 and (i.bull or i.bear))
    ]
    kept.sort(key=lambda i: (i.hits, i.bull + i.bear), reverse=True)
    return kept[: max(0, int(max_tickers))]


# --- fetching -----------------------------------------------------------------


@dataclass
class FetchResult:
    status: str
    text: str = ""
    language: str | None = None
    is_generated: bool | None = None
    error: str | None = None
    retry_after_sec: float | None = None  # None => terminal
    method: str | None = None  # api / ytdlp / whisper (2026-10-03 fallback chain)
    duration_sec: float | None = None
    no_captions: bool = False  # yt-dlp saw a playable video with no captions => Whisper candidate


def _classify_exception(exc: BaseException) -> FetchResult:
    name = type(exc).__name__
    msg = str(exc).replace("\n", " ")[:300]
    if name in ("RequestBlocked", "IpBlocked", "PoTokenRequired", "TooManyRequests"):
        return FetchResult(STATUS_BLOCKED, error=f"{name}: {msg}", retry_after_sec=3600.0)
    if name == "YouTubeRequestFailed" and ("429" in msg or "Too Many Requests" in msg):
        return FetchResult(STATUS_BLOCKED, error=f"{name}: {msg}", retry_after_sec=3600.0)
    if name in ("TranscriptsDisabled", "InvalidVideoId", "VideoUnavailable", "AgeRestricted",
                "NotTranslatable", "TranslationLanguageNotAvailable"):
        return FetchResult(STATUS_NONE, error=name, retry_after_sec=None)
    if name == "NoTranscriptFound":
        # auto-captions often appear hours after upload: retry a few times
        return FetchResult(STATUS_NONE, error=name, retry_after_sec=6 * 3600.0)
    if name == "VideoUnplayable":
        # upcoming / live premieres: captions come after the stream
        return FetchResult(STATUS_NONE, error=f"{name}: {msg[:120]}", retry_after_sec=6 * 3600.0)
    return FetchResult(STATUS_ERROR, error=f"{name}: {msg}", retry_after_sec=1800.0)


class TranscriptFetcher:
    """Thin wrapper over youtube-transcript-api (English, manual preferred over auto)."""

    def __init__(self, api: Any | None = None, languages: tuple[str, ...] = ("en", "en-US", "en-GB")) -> None:
        self._api = api
        self.languages = list(languages)

    def available(self) -> bool:
        if self._api is not None:
            return True
        try:
            import youtube_transcript_api  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _client(self) -> Any:
        if self._api is None:
            from youtube_transcript_api import YouTubeTranscriptApi

            self._api = YouTubeTranscriptApi()
        return self._api

    def fetch(self, video_id: str) -> FetchResult:
        try:
            tr = self._client().fetch(video_id, languages=self.languages)
        except Exception as exc:  # noqa: BLE001
            return _classify_exception(exc)
        try:
            parts = [str(getattr(s, "text", "") or "") for s in tr]
        except TypeError:
            parts = []
        text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
        if not text:
            return FetchResult(STATUS_NONE, error="empty", retry_after_sec=None)
        return FetchResult(STATUS_OK, text=text, language=getattr(tr, "language_code", None),
                           is_generated=getattr(tr, "is_generated", None))


def _background_child() -> None:
    """preexec for heavy helpers: lowest CPU priority, and first in line for the kernel OOM
    killer (oom_score_adj 1000) so a runaway Whisper job can never take the trader with it."""
    os.nice(19)
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass


def _run_json_subprocess(cmd: list[str], timeout: float, nice: bool = False) -> dict[str, Any]:
    """Run a helper module, return its last stdout JSON line. Never raises."""
    try:
        cp = subprocess.run(  # noqa: S603 - fixed argv, video id validated by the helper
            cmd, capture_output=True, text=True, timeout=timeout,
            preexec_fn=_background_child if nice else None,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Timeout: helper exceeded {timeout:.0f}s", "retry_after_sec": 3600.0}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"SubprocessError: {exc}", "retry_after_sec": 1800.0}
    for line in reversed((cp.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    tail = (cp.stderr or "").strip().splitlines()[-1:] or [f"rc={cp.returncode}"]
    if cp.returncode == -9 and tail[0] == "rc=-9":
        tail = ["rc=-9 (SIGKILL, likely out of memory)"]
    return {"status": "error", "error": f"HelperFailed: {tail[0][:200]}", "retry_after_sec": 1800.0}


def _result_from_json(d: dict[str, Any], method: str) -> FetchResult:
    st = str(d.get("status") or STATUS_ERROR)
    if st not in (STATUS_OK, STATUS_NONE, STATUS_BLOCKED, STATUS_ERROR):
        st = STATUS_ERROR
    ra = d.get("retry_after_sec")
    dur = d.get("duration")
    return FetchResult(
        st, text=str(d.get("text") or ""), language=d.get("language"),
        is_generated=d.get("is_generated"), error=d.get("error"),
        retry_after_sec=None if ra is None else float(ra), method=method,
        duration_sec=float(dur) if isinstance(dur, (int, float)) else None,
        no_captions=str(d.get("error") or "").startswith("NoCaptions"),
    )


class YtDlpCaptionFetcher:
    """Fallback 1: captions via yt-dlp (manual English, then YouTube auto-captions)."""

    method = "ytdlp"

    def __init__(self, timeout: float = 180.0, runner: Callable[..., dict[str, Any]] | None = None) -> None:
        self.timeout = float(timeout)
        self._runner = runner or _run_json_subprocess

    def available(self) -> bool:
        if self._runner is not _run_json_subprocess:
            return True
        return importlib.util.find_spec("yt_dlp") is not None

    def fetch(self, video_id: str) -> FetchResult:
        # "--" so a dash-leading ID (e.g. -k71tnoYnBU) is never parsed as an option
        d = self._runner([sys.executable, "-m", "snowball.yolo_demon.ytdlp_fetch", "--", video_id],
                         self.timeout)
        return _result_from_json(d, self.method)


class WhisperTranscriber:
    """Fallback 2: audio via yt-dlp + local faster-whisper (CPU int8 / CUDA), nice 19."""

    method = "whisper"

    def __init__(self, settings: Any, runner: Callable[..., dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self._runner = runner or _run_json_subprocess

    def _cfg(self, name: str, default: Any) -> Any:
        v = getattr(self.settings, name, default)
        return default if v in (None, "") else v

    @property
    def max_minutes(self) -> float:
        return float(self._cfg("yolo_whisper_max_minutes", 720.0))

    def available(self) -> bool:
        if self._runner is not _run_json_subprocess:
            return True
        return (importlib.util.find_spec("yt_dlp") is not None
                and importlib.util.find_spec("faster_whisper") is not None)

    def transcribe(self, video_id: str, duration_sec: float | None,
                   defer_long_minutes: float = 0.0) -> dict[str, Any]:
        dur = float(duration_sec or 0.0)
        # base.en int8 on 2 CPU threads runs well above 1x realtime; generous ceiling anyway
        timeout = 900.0 + 1.5 * dur if dur > 0 else 4 * 3600.0
        cmd = [sys.executable, "-m", "snowball.yolo_demon.whisper_job",
               "--model", str(self._cfg("yolo_whisper_model", "base.en")),
               "--threads", str(int(self._cfg("yolo_whisper_threads", 2))),
               "--model-dir", str(self._cfg("yolo_whisper_model_dir", "./data/whisper-models")),
               "--device", str(self._cfg("yolo_whisper_device", "cpu")),
               "--max-minutes", str(self.max_minutes)]
        if defer_long_minutes:
            cmd += ["--defer-long-minutes", str(float(defer_long_minutes))]
        # options first, then "--", then the ID: argparse treats everything after "--" as
        # positional, so IDs like -k71tnoYnBU / --cb19Jkr4Y no longer fail as unknown flags
        cmd += ["--", video_id]
        return self._runner(cmd, timeout, nice=True)


# --- worker -------------------------------------------------------------------


@dataclass
class _Breaker:
    blocked_until: float = 0.0
    backoff_sec: float = 3600.0
    episodes: int = 0
    logged: bool = False


_NOT_WHISPERABLE = ("YtDlpUnavailable", "YtDlpNotYet", "InvalidVideoId", "VideoUnavailable", "AgeRestricted")


def whisperable(res: FetchResult) -> bool:
    """Caption methods failed in a way local Whisper can still fix (blocked / error / no
    captions). Not for removed/private/members-only videos or not-yet-aired live streams."""
    if res.status == STATUS_OK:
        return False
    err = str(res.error or "")
    return not any(k in err for k in _NOT_WHISPERABLE)


def _methods(settings: Any) -> list[str]:
    raw = getattr(settings, "yolo_transcript_methods", None)
    if raw is None:  # bare test settings: legacy api-only behaviour
        return ["api"]
    out = [m.strip().lower() for m in str(raw).split(",") if m.strip()]
    return [m for m in dict.fromkeys(out) if m in ("api", "ytdlp", "whisper")] or ["api"]


class TranscriptWorker:
    """Background transcript fetch + ticker merge. Never trades.

    2026-10-03 fallback chain (YOLO_TRANSCRIPT_METHODS=api,ytdlp,whisper): try
    youtube-transcript-api; if blocked / nothing, yt-dlp captions; if the video is
    playable but has no captions anywhere, queue it for the Whisper worker (audio +
    local faster-whisper). Each network method has its own circuit breaker, so an
    IP block on one never stops the others; the worker pauses only when every
    caption method is blocked.
    """

    def __init__(
        self,
        settings: Any,
        store: Any,
        *,
        fetcher: TranscriptFetcher | None = None,
        fallbacks: dict[str, Any] | None = None,
        whisper: Any = None,
        running: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.fetcher = fetcher or TranscriptFetcher()
        self._running = running
        self._sleep = sleep
        self._clock = clock
        self.methods = _methods(settings)
        self.fetchers: dict[str, Any] = {"api": self.fetcher}
        if fallbacks is not None:
            self.fetchers.update(fallbacks)
        elif "ytdlp" in self.methods:
            self.fetchers["ytdlp"] = YtDlpCaptionFetcher(
                timeout=float(getattr(settings, "yolo_ytdlp_timeout_seconds", 180.0) or 180.0))
        self.chain = [m for m in self.methods if m in self.fetchers and m != "whisper"]
        self.breakers: dict[str, _Breaker] = {m: _Breaker() for m in self.chain}
        self.breakers.setdefault("api", _Breaker())
        self.breaker = self.breakers["api"]  # back-compat: the primary API breaker
        self._hour_start = clock()
        self._hour_count = 0
        self.whitelist = ticker_whitelist(settings)
        self.stats: dict[str, int] = {"fetched": 0, "ok": 0, "none": 0, "blocked": 0, "error": 0}
        self.method_stats: dict[str, dict[str, int]] = {}
        self.last_error: str | None = None
        self.last_ok: dict[str, Any] | None = None
        self.whisper = None
        if "whisper" in self.methods and bool(getattr(settings, "yolo_whisper_enabled", True)):
            self.whisper = WhisperWorker(settings, store, self, transcriber=whisper, running=running,
                                         sleep=sleep, clock=clock)

    @property
    def delay_sec(self) -> float:
        return max(1.0, float(getattr(self.settings, "yolo_transcript_delay_seconds", 6.0) or 6.0))

    @property
    def max_per_hour(self) -> int:
        return max(1, int(getattr(self.settings, "yolo_transcript_max_per_hour", 300) or 300))

    @property
    def max_stored_chars(self) -> int:
        v = int(getattr(self.settings, "yolo_transcript_max_stored_chars", 2_000_000) or 0)
        return v if v > 0 else 50_000_000

    def _alive(self) -> bool:
        return self._running is None or bool(getattr(self._running, "running", True))

    def _nap(self, sec: float) -> None:
        """Interruptible sleep so shutdown is never delayed by the worker."""
        end = self._clock() + max(0.0, float(sec))
        while self._alive():
            left = end - self._clock()
            if left <= 0:
                return
            self._sleep(min(0.5, left))

    def _method_blocked(self, m: str) -> bool:
        return self._clock() < self.breakers[m].blocked_until

    def _usable(self) -> list[str]:
        return [m for m in self.chain if not self._method_blocked(m) and self.fetchers[m].available()]

    def blocked(self) -> bool:
        """True when every caption method is paused (or unavailable)."""
        return not self._usable()

    def status_payload(self) -> dict[str, Any]:
        counts = self.store.transcript_status_counts()
        now = self._clock()
        breakers = {
            m: {"blocked": now < b.blocked_until,
                "blocked_until": (datetime.fromtimestamp(b.blocked_until, tz=timezone.utc).isoformat()
                                  if now < b.blocked_until else None),
                "episodes": b.episodes}
            for m, b in self.breakers.items()
        }
        try:
            methods = self.store.transcript_method_counts()
        except Exception:  # noqa: BLE001
            methods = {}
        return {
            "enabled": bool(getattr(self.settings, "yolo_transcripts_enabled", True)),
            "available": any(self.fetchers[m].available() for m in self.chain),
            "methods": self.methods,
            "counts": counts,
            "by_method": methods,
            "blocked": self.blocked(),
            "api_blocked": self._method_blocked("api"),
            "blocked_until": (
                datetime.fromtimestamp(self.breaker.blocked_until, tz=timezone.utc).isoformat()
                if self._method_blocked("api") else None
            ),
            "block_episodes": self.breaker.episodes,
            "breakers": breakers,
            "method_stats": self.method_stats,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "whisper": self.whisper.status_payload() if self.whisper is not None else {"enabled": False},
        }

    def _on_blocked(self, res: FetchResult, method: str = "api") -> None:
        br = self.breakers[method]
        now = self._clock()
        if now >= br.blocked_until:
            br.episodes += 1
            br.blocked_until = now + br.backoff_sec
            # log ONCE per episode
            other = [m for m in self.chain if m != method and not self._method_blocked(m)]
            log.warning(
                "Yolo Demon transcripts BLOCKED by YouTube (%s); pausing that method%s",
                method, f", continuing with {','.join(other)}" if other else "; pausing all transcript fetches",
                extra={"data": {"method": method, "error": res.error, "pause_sec": br.backoff_sec,
                                "episode": br.episodes}},
            )
            br.backoff_sec = min(br.backoff_sec * 2.0, 24 * 3600.0)

    def _tally(self, method: str, status: str) -> None:
        d = self.method_stats.setdefault(method, {})
        d[status] = d.get(status, 0) + 1

    def _fetch_chain(self, vid: str) -> FetchResult:
        res: FetchResult | None = None
        notes: list[str] = []
        for m in self.chain:
            f = self.fetchers[m]
            if self._method_blocked(m) or not f.available():
                continue
            r = f.fetch(vid)
            if r.method is None:
                r.method = m
            self._tally(m, r.status)
            if r.status == STATUS_OK:
                if notes:
                    log.info("Yolo Demon transcript via fallback", extra={"data": {
                        "video_id": vid, "method": m, "after": notes}})
                return r
            if r.status == STATUS_BLOCKED:
                self._on_blocked(r, m)
            notes.append(f"{m}: {(r.error or r.status)[:90]}")
            res = r
            if r.status == STATUS_NONE and r.retry_after_sec is None and m != "api":
                break  # video gone / no captions (definitive from yt-dlp)
        if res is None:
            return FetchResult(STATUS_BLOCKED, error="all transcript methods paused", retry_after_sec=3600.0)
        if len(notes) > 1:
            res.error = " | ".join(notes)[:300]
        return res

    def process_one(self, row: dict[str, Any], now: datetime) -> FetchResult:
        vid = str(row["video_id"])
        prev_ok = row.get("status") == STATUS_OK  # full-text refill of a trimmed ok transcript
        res = self._fetch_chain(vid)
        self.stats["fetched"] += 1
        self.stats[res.status] = self.stats.get(res.status, 0) + 1
        if prev_ok and res.status != STATUS_OK:
            self.store.defer_refill(vid, (now + timedelta(hours=6)).isoformat(),
                                    f"full-text refill failed: {res.error or res.status}")
            if res.status == STATUS_ERROR:
                self.last_error = res.error
            return res
        attempts = int(row.get("attempts") or 0) + 1
        whisper_state = None
        retry_after = res.retry_after_sec
        if self.whisper is not None and whisperable(res):
            # captions blocked / missing / failing: local Whisper on the audio (Tb 2:06 AM CT 10/3)
            whisper_state = "queued"
            retry_after = None
            res.error = f"{(res.error or res.status)[:270]} -> whisper queued"
        retry_at = None
        if retry_after is not None and attempts < 8:
            mult = 2 ** min(attempts - 1, 4) if res.status != STATUS_BLOCKED else 1
            retry_at = (now + timedelta(seconds=retry_after * mult)).isoformat()
        ideas: list[TickerIdea] = []
        if res.status == STATUS_OK:
            ideas = extract_transcript_ideas(res.text, self.whitelist)
        self.store.save_transcript(
            video_id=vid,
            channel_handle=row.get("channel_handle"),
            status=res.status,
            text=res.text[: self.max_stored_chars] if res.text else None,
            full_chars=len(res.text or ""),
            language=res.language,
            is_generated=res.is_generated,
            error=res.error if res.status != STATUS_OK else None,
            attempts=attempts,
            next_retry_at=retry_at,
            ideas=[i.to_dict() for i in ideas],
            fetched_at=now,
            method=res.method,
            whisper_state=whisper_state,
            duration_sec=res.duration_sec,
        )
        if res.status == STATUS_OK:
            self.store.merge_transcript_tickers(vid, [i.ticker for i in ideas],
                                                {i.ticker: i for i in ideas}, now)
            self.last_ok = {"video_id": vid, "channel": row.get("channel_handle"), "method": res.method,
                            "chars": len(res.text), "at": now.isoformat()}
            if res.method == "api":
                self.breaker.backoff_sec = 3600.0
        if res.status == STATUS_ERROR:
            self.last_error = res.error
        return res

    def run_batch(self, limit: int, now: datetime | None = None) -> int:
        done = 0
        refill = "ytdlp" in self.chain
        for row in self.store.transcripts_due(limit=limit, now=now or datetime.now(timezone.utc),
                                              refill_trimmed=refill):
            if not self._alive():
                break
            if self.blocked():
                if self.whisper is None or not self.whisper.available():
                    break
                if row.get("status") == STATUS_OK:
                    continue  # trimmed-text refill waits for captions; never re-Whisper an ok row
                self.store.queue_whisper(
                    video_id=str(row["video_id"]), channel_handle=row.get("channel_handle"),
                    error="caption methods paused (IP block) -> whisper queued",
                    fetched_at=now or datetime.now(timezone.utc))
                self.stats["queued_whisper"] = self.stats.get("queued_whisper", 0) + 1
                done += 1
                continue
            if self._clock() - self._hour_start >= 3600:
                self._hour_start, self._hour_count = self._clock(), 0
            if self._hour_count >= self.max_per_hour:
                break
            self.process_one(row, now or datetime.now(timezone.utc))
            self._hour_count += 1
            done += 1
            self._nap(self.delay_sec)
        return done

    def run_forever(self) -> None:
        if not any(self.fetchers[m].available() for m in self.chain):
            log.warning("Yolo Demon transcripts disabled: no transcript method installed")
            return
        log.info("Yolo Demon transcript worker start",
                 extra={"data": {"delay_sec": self.delay_sec, "max_per_hour": self.max_per_hour,
                                 "methods": self.methods,
                                 "available": [m for m in self.chain if self.fetchers[m].available()]}})
        idle_logged_at = 0.0
        while self._alive():
            if self.blocked() and (self.whisper is None or not self.whisper.available()):
                self._nap(30.0)
                continue
            try:
                n = self.run_batch(limit=20)
            except Exception:  # noqa: BLE001
                log.exception("Yolo Demon transcript batch failed (trading loop unaffected)")
                n = 0
            if n == 0:
                if self._clock() - idle_logged_at > 3600:
                    idle_logged_at = self._clock()
                    log.info("Yolo Demon transcripts idle", extra={"data": {
                        "counts": self.store.transcript_status_counts(), **self.stats}})
                self._nap(60.0)
        log.info("Yolo Demon transcript worker stopped")


def _parse_iso(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class WhisperWorker:
    """Last-resort transcripts for caption-less videos: one at a time, nice 19, own thread."""

    def __init__(self, settings: Any, store: Any, parent: TranscriptWorker, *,
                 transcriber: Any = None, running: Any = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.store = store
        self.parent = parent
        self.transcriber = transcriber or WhisperTranscriber(settings)
        self._running = running
        self._sleep = sleep
        self._clock = clock
        self.breaker = _Breaker()
        self.stats: dict[str, int] = {"ok": 0, "failed": 0, "skipped_long": 0, "blocked": 0}
        self.current: str | None = None
        self.last: dict[str, Any] | None = None

    def available(self) -> bool:
        return self.transcriber.available()

    def _alive(self) -> bool:
        return self._running is None or bool(getattr(self._running, "running", True))

    def _nap(self, sec: float) -> None:
        end = self._clock() + max(0.0, float(sec))
        while self._alive():
            left = end - self._clock()
            if left <= 0:
                return
            self._sleep(min(0.5, left))

    def status_payload(self) -> dict[str, Any]:
        return {"enabled": True, "available": self.available(),
                "model": getattr(self.settings, "yolo_whisper_model", "base.en"),
                "device": getattr(self.settings, "yolo_whisper_device", "cpu"),
                "threads": getattr(self.settings, "yolo_whisper_threads", 2),
                "running": self.current, "last": self.last, "stats": self.stats,
                "blocked": self._clock() < self.breaker.blocked_until}

    def _cfg(self, name: str, default: float) -> float:
        v = getattr(self.settings, name, default)
        return float(default if v in (None, "") else v)

    def next_rows(self, limit: int = 1, now: datetime | None = None) -> list[dict[str, Any]]:
        return self.store.whisper_next(limit, now=now or datetime.now(timezone.utc),
                                       long_sec=60.0 * self._cfg("yolo_whisper_long_minutes", 120.0),
                                       fresh_hours=self._cfg("yolo_whisper_fresh_hours", 48.0))

    def process_one(self, row: dict[str, Any], now: datetime | None = None) -> str:
        vid = str(row["video_id"])
        now = now or datetime.now(timezone.utc)
        defer = 0.0
        pub = _parse_iso(row.get("published_at"))
        fresh = pub is not None and (now - pub) < timedelta(hours=self._cfg("yolo_whisper_fresh_hours", 48.0))
        if row.get("duration_sec") is None and not fresh:
            defer = self._cfg("yolo_whisper_long_minutes", 120.0)  # unknown length: learn it first
        self.current = vid
        try:
            d = self.transcriber.transcribe(vid, row.get("duration_sec"), defer)
        finally:
            self.current = None
        st = str(d.get("status") or STATUS_ERROR)
        self.last = {k: d.get(k) for k in ("status", "error", "duration", "transcribe_sec", "realtime_x",
                                            "model")} | {"video_id": vid, "at": now.isoformat()}
        if st == STATUS_OK and d.get("text"):
            res = _result_from_json(d, "whisper")
            ideas = extract_transcript_ideas(res.text, self.parent.whitelist)
            self.store.save_transcript(
                video_id=vid, channel_handle=row.get("channel_handle"), status=STATUS_OK,
                text=res.text[: self.parent.max_stored_chars], full_chars=len(res.text),
                language=res.language, is_generated=True, error=None,
                attempts=int(row.get("attempts") or 0) + 1, next_retry_at=None,
                ideas=[i.to_dict() for i in ideas], fetched_at=now, method="whisper",
                whisper_state="done", duration_sec=res.duration_sec,
            )
            self.store.merge_transcript_tickers(vid, [i.ticker for i in ideas],
                                                {i.ticker: i for i in ideas}, now)
            self.parent.last_ok = {"video_id": vid, "channel": row.get("channel_handle"), "method": "whisper",
                                   "chars": len(res.text), "at": now.isoformat()}
            self.stats["ok"] += 1
            log.info("Yolo Demon Whisper transcript ok", extra={"data": {
                "video_id": vid, "chars": len(res.text), "duration": d.get("duration"),
                "transcribe_sec": d.get("transcribe_sec"), "realtime_x": d.get("realtime_x")}})
            return STATUS_OK
        dur = d.get("duration") if isinstance(d.get("duration"), (int, float)) else None
        if st == "deferred_long":
            # long livestream: keep queued, now with its duration, so it sorts into the long tier
            self.store.set_whisper_state(vid, "queued", duration_sec=float(dur or 0) or None)
            self.stats["deferred_long"] = self.stats.get("deferred_long", 0) + 1
            return st
        if st == "skipped_long":
            self.store.set_whisper_state(vid, "skipped_long", d.get("error"), duration_sec=dur)
            self.stats["skipped_long"] += 1
            return st
        if st == STATUS_BLOCKED:
            self.stats["blocked"] += 1
            br = self.breaker
            br.episodes += 1
            br.blocked_until = self._clock() + br.backoff_sec
            log.warning("Yolo Demon Whisper audio download BLOCKED; pausing Whisper",
                        extra={"data": {"error": d.get("error"), "pause_sec": br.backoff_sec}})
            br.backoff_sec = min(br.backoff_sec * 2.0, 24 * 3600.0)
            return st  # stays queued
        # error / none: retry a few times (spaced out), then give up (status stays not-ok)
        self.stats["failed"] += 1
        tries = int(row.get("attempts") or 0) + 1
        ra = d.get("retry_after_sec")
        terminal = ra is None or tries >= 4
        after = None if terminal else (now + timedelta(seconds=max(1800.0, float(ra)))).isoformat()
        self.store.set_whisper_state(vid, "failed" if terminal else "queued",
                                     f"whisper: {d.get('error') or st}", after=after, duration_sec=dur)
        self.store.set_attempts(vid, tries)  # bounded retries
        log.warning("Yolo Demon Whisper transcript failed", extra={"data": {
            "video_id": vid, "error": d.get("error"), "terminal": terminal}})
        return st

    def run_forever(self) -> None:
        if not self.available():
            log.warning("Yolo Demon Whisper fallback unavailable (yt-dlp/faster-whisper not installed)")
            return
        log.info("Yolo Demon Whisper worker start", extra={"data": {
            "model": getattr(self.settings, "yolo_whisper_model", "base.en"),
            "threads": getattr(self.settings, "yolo_whisper_threads", 2)}})
        while self._alive():
            if self._clock() < self.breaker.blocked_until:
                self._nap(30.0)
                continue
            try:
                rows = self.next_rows(1)
                if rows:
                    st = self.process_one(rows[0])
                    if st == STATUS_OK:
                        self.breaker.backoff_sec = 3600.0
                    self._nap(5.0 if st == "deferred_long" else 20.0)
                    continue
            except Exception:  # noqa: BLE001
                log.exception("Yolo Demon Whisper job failed (trading loop unaffected)")
            self._nap(120.0)
        log.info("Yolo Demon Whisper worker stopped")
