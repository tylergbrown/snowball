"""HTTP helper for Yolo Demon. Independent of The Watcher ingest."""

from __future__ import annotations

import urllib.request
from typing import Mapping, Protocol

DEFAULT_TIMEOUT = 20.0
DEFAULT_ACCEPT = "application/json, */*;q=0.8"


class HttpClient(Protocol):
    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes: ...

    def post_bytes(
        self,
        url: str,
        data: bytes,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes: ...


class UrlLibHttp:
    def __init__(self, user_agent: str = "SnowballYoloDemon/1.0") -> None:
        self.user_agent = user_agent

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        out = {"User-Agent": self.user_agent, "Accept": DEFAULT_ACCEPT}
        if extra:
            out.update(extra)
        return out

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes:
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def post_bytes(
        self,
        url: str,
        data: bytes,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes:
        req = urllib.request.Request(
            url, data=data, headers=self._headers(headers), method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
