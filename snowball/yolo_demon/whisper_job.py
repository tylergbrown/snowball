"""Whisper fallback for Yolo Demon transcripts (Tb 2:06 AM CT 10/3).

Last resort when a video has no captions anywhere (youtube-transcript-api and yt-dlp
both found none): download the audio track only with yt-dlp, transcribe it locally
with faster-whisper (CPU int8 by default; ``--device cuda`` if a GPU is attached),
delete the audio, print one JSON line. Free: no paid API.

Run by the Whisper worker as a ``nice 19`` subprocess with a timeout, so it can only
use idle CPU and can never block or crash the trading process:

    python -m snowball.yolo_demon.whisper_job --model base.en --threads 2 \
        --model-dir /app/data/whisper-models [--device cpu] [--max-minutes 720] \
        [--defer-long-minutes 120] [--chunk-minutes 20] -- VIDEO_ID

The ``--`` matters (image r, 10/3): YouTube IDs may start with ``-`` (``-k71tnoYnBU``,
``--cb19Jkr4Y``) and argparse would otherwise read them as flags. yt-dlp itself is only
ever handed the full ``https://www.youtube.com/watch?v=<id>`` URL, never a bare ID.

Memory (image r): audio is decoded and transcribed in ``--chunk-minutes`` pieces
(default 20) instead of all at once. faster-whisper's whole-file path held the full
decoded audio + full-length STFT in RAM (~10 GB for an 8 h livestream, OOM-killed with
rc=-9 on Brown-02); per-chunk peak is a few hundred MB regardless of length.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from snowball.yolo_demon.ytdlp_fetch import classify_error

AUDIO_FORMAT = "bestaudio[abr<=96]/bestaudio/worstaudio/worst"
SAMPLE_RATE = 16000
DEFAULT_CHUNK_MINUTES = 20.0
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")


def video_url(video_id: str) -> str:
    """Always hand yt-dlp a full URL, so a dash-leading ID can never look like an option."""
    return f"https://www.youtube.com/watch?v={video_id}"


def iter_audio_chunks(path: str, chunk_sec: float, sampling_rate: int = SAMPLE_RATE) -> Any:
    """Stream-decode ``path`` to mono float32 at ``sampling_rate``, yielding numpy arrays of
    at most ``chunk_sec`` seconds. Memory stays bounded by one chunk (same s16 resample +
    /32768 scaling as faster_whisper.audio.decode_audio, which loads the whole file)."""
    import av  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    target = max(sampling_rate, int(float(chunk_sec) * sampling_rate))
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    buf: list[Any] = []
    have = 0

    def take(n: int) -> Any:
        nonlocal buf, have
        cat = np.concatenate(buf) if len(buf) > 1 else buf[0]
        out, rest = cat[:n], cat[n:]
        buf = [rest] if rest.size else []
        have = int(rest.size)
        return out.astype(np.float32) / 32768.0

    def frames(container: Any) -> Any:
        it = container.decode(audio=0)
        while True:
            try:
                fr = next(it)
            except StopIteration:
                break
            except av.error.InvalidDataError:
                continue
            yield fr
        yield None  # flush the resampler

    try:
        with av.open(path, mode="r", metadata_errors="ignore") as container:
            for fr in frames(container):
                out = resampler.resample(fr)
                for rf in (out if isinstance(out, list) else [out] if out is not None else []):
                    a = rf.to_ndarray().reshape(-1)
                    if a.size:
                        buf.append(a)
                        have += int(a.size)
                while have >= target:
                    yield take(target)
        if have:
            yield take(have)
    finally:
        del resampler
        gc.collect()


def download_audio(video_id: str, out_dir: Path, max_minutes: float, ydl_factory: Any = None,
                   defer_long_minutes: float = 0.0) -> dict[str, Any]:
    if ydl_factory is None:
        from yt_dlp import YoutubeDL  # noqa: PLC0415

        ydl_factory = YoutubeDL
    url = video_url(video_id)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
            "socket_timeout": 30, "format": AUDIO_FORMAT,
            "outtmpl": str(out_dir / "%(id)s.%(ext)s")}
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        dur = info.get("duration")
        if info.get("live_status") in ("is_live", "is_upcoming"):
            return {"status": "none", "error": f"YtDlpNotYet: live_status={info.get('live_status')}",
                    "retry_after_sec": 6 * 3600.0, "duration": dur}
        if max_minutes and dur and float(dur) > float(max_minutes) * 60.0:
            return {"status": "skipped_long", "duration": dur,
                    "error": f"WhisperSkipped: {float(dur) / 60:.0f} min > YOLO_WHISPER_MAX_MINUTES={max_minutes:g}"}
        if defer_long_minutes and dur and float(dur) > float(defer_long_minutes) * 60.0:
            return {"status": "deferred_long", "duration": dur}
        info = ydl.process_ie_result(info, download=True) or info
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            cands = sorted(out_dir.glob(f"{video_id}.*"))
            path = cands[0] if cands else path
        if not path.exists():
            return {"status": "error", "error": "WhisperAudioMissing", "retry_after_sec": 3600.0, "duration": dur}
        return {"status": "downloaded", "path": str(path), "duration": dur,
                "bytes": path.stat().st_size}


def transcribe(path: str, model: str, threads: int, model_dir: str, device: str = "cpu",
               model_factory: Any = None, chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
               audio_loader: Any = None) -> dict[str, Any]:
    """Transcribe ``path`` chunk by chunk (bounded memory); texts are joined in order."""
    if model_factory is None:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        model_factory = WhisperModel
    loader = audio_loader or iter_audio_chunks
    compute = "int8" if device == "cpu" else "float16"
    m = model_factory(model, device=device, compute_type=compute, cpu_threads=max(1, int(threads)),
                      download_root=model_dir)
    lang = "en" if model.endswith(".en") else None
    detected = None
    audio_sec = 0.0
    have_sec = False
    parts: list[str] = []
    chunks = 0
    for audio in loader(path, max(1.0, float(chunk_minutes)) * 60.0):
        chunks += 1
        segments, info = m.transcribe(audio, language=lang, beam_size=1, vad_filter=True,
                                      condition_on_previous_text=False)
        parts.extend(str(getattr(s, "text", "") or "").strip() for s in segments)
        detected = detected or getattr(info, "language", None)
        d = getattr(info, "duration", None)
        if isinstance(d, (int, float)):
            audio_sec += float(d)
            have_sec = True
        del audio, segments
        gc.collect()
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return {"text": text, "language": detected or lang,
            "audio_sec": audio_sec if have_sec else None, "chunks": chunks}


def run(video_id: str, model: str, threads: int, model_dir: str, device: str, max_minutes: float,
        ydl_factory: Any = None, model_factory: Any = None, defer_long_minutes: float = 0.0,
        chunk_minutes: float = DEFAULT_CHUNK_MINUTES, audio_loader: Any = None) -> dict[str, Any]:
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"yolo-whisper-{video_id}-"))
    try:
        try:
            dl = download_audio(video_id, tmp, max_minutes, ydl_factory=ydl_factory,
                                defer_long_minutes=defer_long_minutes)
        except Exception as exc:  # noqa: BLE001
            return classify_error(f"{type(exc).__name__}: {exc}")
        if dl.get("status") != "downloaded":
            return dl
        t1 = time.time()
        try:
            tr = transcribe(dl["path"], model, threads, model_dir, device, model_factory=model_factory,
                            chunk_minutes=chunk_minutes, audio_loader=audio_loader)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"WhisperError: {type(exc).__name__}: {str(exc)[:200]}",
                    "retry_after_sec": 6 * 3600.0, "duration": dl.get("duration")}
        t2 = time.time()
        audio = float(tr.get("audio_sec") or dl.get("duration") or 0.0)
        out = {"status": "ok" if tr["text"] else "none", "text": tr["text"], "language": tr.get("language"),
               "is_generated": True, "duration": dl.get("duration"), "audio_bytes": dl.get("bytes"),
               "download_sec": round(t1 - t0, 1), "transcribe_sec": round(t2 - t1, 1),
               "realtime_x": round(audio / (t2 - t1), 2) if audio and t2 > t1 else None,
               "model": model, "device": device, "chunks": tr.get("chunks")}
        if not tr["text"]:
            out.update(error="WhisperEmpty: no speech detected", retry_after_sec=None)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="whisper_job")
    ap.add_argument("video_id")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--model-dir", default="./data/whisper-models")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--max-minutes", type=float, default=720.0)
    ap.add_argument("--defer-long-minutes", type=float, default=0.0)
    ap.add_argument("--chunk-minutes", type=float, default=DEFAULT_CHUNK_MINUTES)
    a = ap.parse_args(argv)
    if not VIDEO_ID_RE.fullmatch(a.video_id):
        print(json.dumps({"status": "error", "error": "bad video id", "retry_after_sec": None}))
        return 2
    res = run(a.video_id, a.model, a.threads, a.model_dir, a.device, a.max_minutes,
              defer_long_minutes=a.defer_long_minutes, chunk_minutes=a.chunk_minutes)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
