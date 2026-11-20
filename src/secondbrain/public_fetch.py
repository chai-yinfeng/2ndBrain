"""公开材料 HTTP：逐跳校验、流式大小限制与每次获取的共享请求预算。"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from secondbrain.net import RETRYABLE_STATUS, FetchError, HttpClient, _retry_after

MAX_REQUESTS = 24
MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_BYTES = 2 * 1024 * 1024


def public_url(value: str | None) -> str | None:
    try:
        parts = urlsplit(value or "")
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme not in ("http", "https") or not host
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443) or "%" in host
                or any(c.isspace() or ord(c) < 32 for c in value or "")
                or "\\" in (value or "")):
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or host.replace(".", "").isdigit():
                return None
        return value
    except (TypeError, ValueError):
        return None


def diagnostic_url(url: str) -> str:
    """诊断不保存凭据、查询值或 fragment。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        netloc = f"[{host}]" if ":" in host else host
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "…" if parts.query else "", ""))
    except ValueError:
        return "(无效地址)"


def diagnostic_reason(reason: str) -> str:
    return re.sub(r"https?://[^\s<>\"']+", lambda match: diagnostic_url(match.group()), reason)


class PublicClient:
    def __init__(self, client):
        self.client = client
        self.responses: list[dict] = []
        self.attempts: list[dict] = []
        self.remaining = MAX_REQUESTS
        self.total_bytes = 0
        self.failed: dict[str, str] = {}

    def note(self, url, status, reason):
        self.attempts.append({
            "url": diagnostic_url(url), "status": str(status),
            "reason": diagnostic_reason(reason),
        })

    def _request(self, url, kwargs, limit):
        if isinstance(self.client, HttpClient):
            http = self.client._client
            if isinstance(http._transport, httpx.HTTPTransport):
                parts = urlsplit(url)
                try:
                    addresses = socket.getaddrinfo(parts.hostname, parts.port or (
                        443 if parts.scheme == "https" else 80
                    ))
                except OSError as exc:
                    raise FetchError("无法解析公开材料域名") from exc
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global
                                        for a in addresses):
                    raise FetchError("拒绝解析到非公开网络的地址")
            self.client._throttle(urlsplit(url).hostname or "")
            args = {k: v for k, v in kwargs.items() if k != "max_bytes"}
            with http.stream("GET", url, follow_redirects=False, **args) as response:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    raise FetchError("公开材料响应超过大小上限")
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > limit:
                        raise FetchError("公开材料响应超过大小上限")
                    data.extend(chunk)
                headers = dict(response.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(response.status_code, headers=headers,
                                      content=bytes(data), request=response.request)
        response = self.client.get(url, **{**kwargs, "max_bytes": limit})
        if len(response.content) > limit:
            raise FetchError("公开材料响应超过大小上限")
        return response

    def get(self, url, **kwargs):
        limit = kwargs.get("max_bytes") or DEFAULT_BYTES
        retries = self.client.settings.retries if isinstance(self.client, HttpClient) else 0
        retry = 0
        for _ in range(MAX_REQUESTS):
            try:
                if not public_url(url):
                    raise FetchError("拒绝不安全或非公开的材料地址")
                if url in self.failed:
                    raise FetchError(self.failed[url])
                if self.remaining <= 0 or self.total_bytes >= MAX_TOTAL_BYTES:
                    raise FetchError("公开材料请求预算已用尽")
                self.remaining -= 1
                response = self._request(
                    url, kwargs, min(limit, MAX_TOTAL_BYTES - self.total_bytes)
                )
                self.total_bytes += len(response.content)
                if not public_url(str(response.url)):
                    raise FetchError("拒绝不安全的响应地址")
                status = response.status_code
                self.responses.append({"url": diagnostic_url(str(response.url)), "status": status})
                self.note(str(response.url), status, f"HTTP {status}")
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("公开材料跳转缺少 Location")
                    target = urljoin(str(response.url), location)
                    if urlsplit(target)[:2] != urlsplit(str(response.url))[:2]:
                        kwargs.pop("headers", None)
                    url = target
                    kwargs.pop("params", None)
                    continue
                if status in RETRYABLE_STATUS and retry < retries:
                    wait = max(self.client.settings.retry_backoff_seconds * 2 ** retry,
                               _retry_after(response.headers.get("retry-after"),
                                            self.client.settings.retry_after_max_seconds))
                    self.client._sleep(wait)
                    retry += 1
                    continue
                if status >= 400:
                    raise FetchError(f"HTTP {status}：{diagnostic_url(url)}")
                return response
            except (FetchError, httpx.HTTPError) as exc:
                reason = diagnostic_reason(str(exc))
                self.failed[url] = reason
                self.note(url, "failed", reason)
                raise FetchError(reason) from exc
        raise FetchError("公开材料地址跳转次数超过上限")
