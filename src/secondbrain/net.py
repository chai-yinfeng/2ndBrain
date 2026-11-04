"""HTTP 客户端：礼貌 UA、超时、重试、按 host 限速。线程安全。"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import httpx

from secondbrain.models import FetchSettings

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """抓取失败（已重试），消息可直接展示给用户。"""


class HttpClient:
    def __init__(
        self,
        settings: FetchSettings,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        interval = self.settings.host_intervals.get(host, self.settings.per_host_interval_seconds)
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = start + interval
        if start > now:
            self._sleep(start - now)

    def get(
        self, url: str, *, params: dict | None = None, max_bytes: int | None = None
    ) -> httpx.Response:
        """GET 并返回 2xx 响应；网络错误与 429/5xx 按配置重试，其余失败抛 FetchError。"""
        host = urlsplit(url).hostname or ""
        retries = self.settings.retries
        last_error = ""
        for attempt in range(retries + 1):
            if attempt:
                self._sleep(self.settings.retry_backoff_seconds * 2 ** (attempt - 1))
            self._throttle(host)
            try:
                with self._client.stream("GET", url, params=params) as resp:
                    if resp.status_code in RETRYABLE_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}：{resp.url}")
                    length = resp.headers.get("content-length")
                    if max_bytes and length and length.isdigit() and int(length) > max_bytes:
                        raise FetchError(f"响应过大（{length} 字节 > {max_bytes}）：{resp.url}")
                    resp.read()
                    return resp
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise FetchError(f"{last_error}（已重试 {retries} 次）：{url}")
