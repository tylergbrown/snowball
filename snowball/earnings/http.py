"""HTTP helpers for Nasdaq calendar + Yahoo chart (research only)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Protocol

USER_AGENT = "SnowballEarnings/1.0 (research; contact tylerbrown7@icloud.com)"
NASDAQ_ORIGIN = "https://api.nasdaq.com"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_TIMEOUT = 8.0


class HttpResponse:
    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        url: str,
    ) -> None:
        self.status = status
        self.headers = {str(k): str(v) for k, v in headers.items()}
        self.body = body
        self.url = url

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", errors="replace"))


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...


class UrlLibHttp:
    """GET via curl -4/--http1.1 with retries; urllib last resort."""

    def __init__(self, user_agent: str = USER_AGENT, retries: int = 1) -> None:
        self.user_agent = user_agent
        self.retries = max(1, int(retries))

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        req_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            req_headers.update(dict(headers))
        last: HttpResponse | None = None
        for attempt in range(self.retries):
            curl_resp = self._curl_get(url, req_headers, timeout)
            if curl_resp is not None and curl_resp.status == 200 and curl_resp.body:
                return curl_resp
            last = curl_resp
            time.sleep(0.25 * (attempt + 1))
        if last is not None and last.body:
            return last
        try:
            return self._urllib_get(url, req_headers, min(timeout, 8.0))
        except Exception:
            return last or HttpResponse(0, {}, b"", url)

    def _curl_get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse | None:
        with tempfile.NamedTemporaryFile(prefix="sb_earn_", delete=False) as tmp:
            out_path = Path(tmp.name)
        try:
            cmd = [
                "curl",
                "-4",
                "-sS",
                "-L",
                "--http1.1",
                "--max-time",
                str(max(1, int(timeout))),
                "-o",
                str(out_path),
                "-w",
                "%{http_code}",
                "-A",
                headers.get("User-Agent", self.user_agent),
            ]
            for key, value in headers.items():
                if key.lower() == "user-agent":
                    continue
                cmd.extend(["-H", f"{key}: {value}"])
            cmd.append(url)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            try:
                status = int((proc.stdout or b"0").decode().strip() or "0")
            except ValueError:
                status = 0
            body = out_path.read_bytes() if out_path.exists() else b""
            if status == 0 and not body:
                return None
            return HttpResponse(status, {}, body, url)
        finally:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _urllib_get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return HttpResponse(int(resp.status), dict(resp.headers.items()), body, url)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            return HttpResponse(int(exc.code), dict(exc.headers.items()), body, url)


def nasdaq_calendar_url(report_date: str) -> str:
    q = urllib.parse.urlencode({"date": report_date})
    return f"{NASDAQ_ORIGIN}/api/calendar/earnings?{q}"


def nasdaq_calendar_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }


def yahoo_chart_url(symbol: str, *, range_: str = "2mo", interval: str = "1d") -> str:
    q = urllib.parse.urlencode({"range": range_, "interval": interval})
    sym = urllib.parse.quote(symbol)
    return f"{YAHOO_CHART}/{sym}?{q}"
