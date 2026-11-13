"""yt-dlp caption fallback for Yolo Demon transcripts (Tb 2:06 AM CT 10/3).

When ``youtube-transcript-api`` is IP-blocked (or finds nothing), read the video's
captions with yt-dlp instead: manual English subtitles first, then YouTube's
automatic captions (``en-orig`` = original-language ASR, then ``en``). Nothing is
downloaded except the caption track itself (json3, VTT fallback).

Run as a subprocess by the transcript worker so a hang or crash in yt-dlp can never
touch the trading process:

    python -m snowball.yolo_demon.ytdlp_fetch -- VIDEO_ID   # prints one JSON line

(``--`` keeps a dash-leading ID such as ``-k71tnoYnBU`` from ever reading as an option;
yt-dlp only ever gets the full watch URL.)

Result keys: status (ok / none / blocked / error), text, language, is_generated,
duration, live_status, availability, captions ("manual" / "auto" / "none"),
error, retry_after_sec (None = terminal).
"""

from __future__ import annotations

import html
import json
import re
import sys
from typing import Any

MANUAL_PREF = ("en", "en-US", "en-GB", "en-CA", "en-AU")
AUTO_PREF = ("en-orig", "en", "en-US", "en-GB")
FORMAT_PREF = ("json3", "vtt", "srv1", "srv3", "ttml")

_BLOCK_PAT = re.compile(
    r"(sign in to confirm|not a bot|http error 429|too many requests|rate.?limited|"
    r"try again later|ip.?blocked|requestblocked|unusual traffic|captcha|po.?token)", re.I)
_GEO_PAT = re.compile(r"(in your country|geo.?restrict|not available in your location)", re.I)
_GONE_PAT = re.compile(
    r"(private video|video unavailable|has been removed|account .* terminated|"
    r"no longer available|copyright|does not exist|incomplete youtube id|"
    r"members.only|join this channel|premium|requires payment|age.restricted|"
    r"inappropriate|confirm your age)", re.I)
_LATER_PAT = re.compile(r"(live event will begin|premieres? in|is upcoming|this live stream|"
                        r"offline|will begin in|scheduled)", re.I)


def classify_error(msg: str) -> dict[str, Any]:
    m = (msg or "").replace("\n", " ").strip()
    short = m[:240]
    if _GEO_PAT.search(m):
        return {"status": "none", "error": f"YtDlpUnavailable: {short}", "retry_after_sec": None}
    if _BLOCK_PAT.search(m):
        return {"status": "blocked", "error": f"YtDlpBlocked: {short}", "retry_after_sec": 3600.0}
    if _LATER_PAT.search(m):
        return {"status": "none", "error": f"YtDlpNotYet: {short}", "retry_after_sec": 6 * 3600.0}
    if _GONE_PAT.search(m):
        return {"status": "none", "error": f"YtDlpUnavailable: {short}", "retry_after_sec": None}
    return {"status": "error", "error": f"YtDlpError: {short}", "retry_after_sec": 1800.0}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def parse_json3(raw: str) -> str:
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    parts: list[str] = []
    for ev in d.get("events") or []:
        for seg in ev.get("segs") or []:
            t = seg.get("utf8")
            if t:
                parts.append(t)
    return _norm(" ".join(parts))


_VTT_TS = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s+-->")
_TAG = re.compile(r"<[^>]+>")


def parse_vtt(raw: str) -> str:
    """WebVTT/SRT to text; rolling auto-caption duplicates collapsed."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if (not s or s == "WEBVTT" or s.isdigit() or _VTT_TS.match(s)
                or s.startswith(("Kind:", "Language:", "NOTE", "STYLE"))):
            continue
        s = _norm(_TAG.sub("", s))
        if s and (not out or out[-1] != s):
            out.append(s)
    return _norm(" ".join(out))


def parse_xmlish(raw: str) -> str:
    return _norm(_TAG.sub(" ", raw or ""))


def pick_track(info: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """(kind, lang, format entry) best English track; manual beats auto."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}

    def best_fmt(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        by = {str(e.get("ext")): e for e in entries if e.get("url")}
        for f in FORMAT_PREF:
            if f in by:
                return by[f]
        return next(iter(by.values()), None)

    for kind, pool, pref in (("manual", manual, MANUAL_PREF), ("auto", auto, AUTO_PREF)):
        keys = [k for k in pref if k in pool]
        if kind == "manual":
            keys += sorted(k for k in pool if k.lower().startswith("en") and k not in keys)
        for k in keys:
            f = best_fmt(pool[k])
            if f:
                return kind, k, f
    return None


def parse_track(ext: str, raw: str) -> str:
    if ext == "json3":
        return parse_json3(raw)
    if ext in ("vtt", "srt"):
        return parse_vtt(raw)
    return parse_xmlish(raw)


def _ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extractor_retries": 1,
        "noprogress": True,
    }


def fetch(video_id: str, ydl_factory: Any = None) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        if ydl_factory is None:
            from yt_dlp import YoutubeDL  # noqa: PLC0415

            ydl_factory = YoutubeDL
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"YtDlpMissing: {exc}", "retry_after_sec": 3600.0}
    try:
        with ydl_factory(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            meta = {
                "duration": info.get("duration"),
                "live_status": info.get("live_status"),
                "availability": info.get("availability"),
                "title": info.get("title"),
            }
            if info.get("live_status") in ("is_live", "is_upcoming"):
                return {**meta, "status": "none", "captions": "none",
                        "error": f"YtDlpNotYet: live_status={info.get('live_status')}",
                        "retry_after_sec": 6 * 3600.0}
            tr = pick_track(info)
            if tr is None:
                return {**meta, "status": "none", "captions": "none",
                        "error": "NoCaptions: yt-dlp found no English subtitles or automatic captions",
                        "retry_after_sec": None}
            kind, lang, fmt = tr
            try:
                raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - e.g. timedtext HTTP 429 (IP block)
                return {**meta, **classify_error(f"{type(exc).__name__}: {exc}"), "captions": kind}
            text = parse_track(str(fmt.get("ext")), raw)
            if not text:
                return {**meta, "status": "none", "captions": kind,
                        "error": f"EmptyCaptions: {kind} {lang} {fmt.get('ext')}", "retry_after_sec": 6 * 3600.0}
            return {**meta, "status": "ok", "captions": kind, "text": text,
                    "language": lang.replace("-orig", ""), "is_generated": kind == "auto",
                    "track": f"{kind}:{lang}:{fmt.get('ext')}", "retry_after_sec": None}
    except Exception as exc:  # noqa: BLE001
        return classify_error(f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if len(argv) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", argv[0]):
        print(json.dumps({"status": "error", "error": "usage: ytdlp_fetch -- VIDEO_ID", "retry_after_sec": None}))
        return 2
    sys.stdout.write(json.dumps(fetch(argv[0]), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
