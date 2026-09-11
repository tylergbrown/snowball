"""Polite urllib client for the official House Clerk disclosures site."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Mapping, Protocol

USER_AGENT = "SnowballClerk/1.0 (research; official House Clerk filings)"
CLERK_ORIGIN = "https://disclosures-clerk.house.gov"
DEFAULT_TIMEOUT = 60.0


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

    def header(self, name: str) -> str | None:
        want = name.lower()
        for key, value in self.headers.items():
            if key.lower() == want:
                return value
        return None


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...


class UrlLibHttp:
    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        if not url.startswith(CLERK_ORIGIN + "/"):
            raise ValueError(f"Clerk HTTP refuses non-Clerk URL: {url}")
        req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            req_headers.update(dict(headers))
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return HttpResponse(int(resp.status), dict(resp.headers.items()), body, url)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            return HttpResponse(int(exc.code), dict(exc.headers.items()), body, url)
