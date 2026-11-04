"""测试公共工具：离线 HTTP（httpx.MockTransport）、fixture 读取、最小 PDF 生成。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from secondbrain.models import FetchSettings
from secondbrain.net import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fast_settings(**overrides) -> FetchSettings:
    """无限速、无退避等待的设置，供离线测试使用。"""
    base = {"per_host_interval_seconds": 0, "retry_backoff_seconds": 0, "concurrency": 2}
    return FetchSettings(**{**base, **overrides})


class Router:
    """按 URL（不含 query）匹配响应；未登记的 URL 返回 404。记录所有请求。"""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url.copy_with(query=None))
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, text="not found")
        return route(request) if callable(route) else route

    def urls(self) -> list[str]:
        return [str(r.url.copy_with(query=None)) for r in self.requests]


@pytest.fixture
def make_client():
    clients: list[HttpClient] = []

    def factory(routes: dict[str, Route], **settings) -> tuple[HttpClient, Router]:
        router = Router(routes)
        client = HttpClient(
            fast_settings(**settings), transport=httpx.MockTransport(router), sleep=lambda _: None
        )
        clients.append(client)
        return client, router

    yield factory
    for c in clients:
        c.close()


def make_pdf(pages: list[str]) -> bytes:
    """生成每页一行文本的最小合法 PDF。"""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs[4 + 2 * i] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objs[5 + 2 * i] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    out = b"%PDF-1.4\n"
    offsets = {}
    for k in sorted(objs):
        offsets[k] = len(out)
        out += f"{k} 0 obj\n{objs[k]}\nendobj\n".encode()
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for k in range(1, size):
        out += f"{offsets[k]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
