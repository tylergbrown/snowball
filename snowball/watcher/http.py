from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Mapping, Protocol

log = logging.getLogger("snowball.watcher")

WATCHER_USER_AGENT = "SnowballWatcher/1.0"
DEFAULT_TIMEOUT = 20.0
DEFAULT_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml, "
    "text/xml, application/json;q=0.9, */*;q=0.8"
)


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
    """stdlib HTTP. Inject a fake in tests; never used to place orders."""

    def __init__(self, user_agent: str = WATCHER_USER_AGENT) -> None:
        self.user_agent = user_agent

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        out = {
            "User-Agent": self.user_agent,
            "Accept": DEFAULT_ACCEPT,
        }
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


def fetch_text(
    client: HttpClient,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    raw = client.get_bytes(url, headers=headers, timeout=timeout)
    return raw.decode("utf-8", errors="replace")
