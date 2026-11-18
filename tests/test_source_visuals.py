"""来源封面的实际缓存、渲染及网络安全边界（全离线）。"""

import hashlib
import json
import socket
import struct
import zlib
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from secondbrain import config, db, source_visuals, sources_file
from secondbrain.models import FetchSettings, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.web.app import create_app

HOME = "https://blog.example/home"
IMAGE = "https://cdn.example/cover.png"


def png(width=1, height=1):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + chunk(b"IEND", b""))


@pytest.fixture
def network(monkeypatch):
    calls = []
    routes = {}

    def resolve(host, port, *args, **kwargs):
        address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def handler(request):
        calls.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    settings = FetchSettings(retries=0, per_host_interval_seconds=0)
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        yield routes, calls, client, settings


def page(images):
    return httpx.Response(200, headers={"Content-Type": "text/html"}, text=images)


def test_og_image_is_cached_and_bound_to_homepage(tmp_path, network):
    routes, calls, client, settings = network
    routes[HOME] = page(f'<meta property="og:image" content="{IMAGE}">')
    routes[IMAGE] = httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())
    result = source_visuals.refresh_visual(HOME, client, settings, tmp_path)
    assert result["available"]
    assert calls == [HOME, IMAGE]
    cached = source_visuals.read_visual(HOME, tmp_path)
    assert cached.media_type == "image/png"
    assert cached.path.read_bytes() == png()
    assert cached.version == hashlib.sha256(png()).hexdigest()
    assert source_visuals.read_visual("https://other.example/", tmp_path) is None
    calls.clear()
    assert source_visuals.read_visual(HOME, tmp_path) == cached
    assert calls == []



@pytest.mark.parametrize("homepage_response", [
    httpx.Response(503),
    page("x" * (source_visuals.MAX_RESPONSE_BYTES + 1)),
])
def test_failed_or_large_homepage_still_tries_bounded_favicon(
    tmp_path, network, homepage_response,
):
    routes, calls, client, settings = network
    routes[HOME] = homepage_response
    routes["https://blog.example/favicon.ico"] = httpx.Response(
        200, headers={"Content-Type": "image/png"}, content=png(),
    )
    assert source_visuals.refresh_visual(HOME, client, settings, tmp_path)["available"]
    assert calls == [HOME, "https://blog.example/favicon.ico"]

@pytest.mark.parametrize("unsafe", [
    "http://127.0.0.1/private", "http://[::1]/", "http://169.254.169.254/latest",
    "http://private.example/", "file:///etc/passwd", "https://user:pass@cdn.example/a",
])
def test_private_candidate_is_never_requested_and_icon_fallback_works(tmp_path, network, unsafe):
    routes, calls, client, settings = network
    routes[HOME] = page(f'<meta property="og:image" content="{unsafe}">'
                        '<link rel="icon" href="/icon.png">')
    routes["https://blog.example/icon.png"] = httpx.Response(
        200, headers={"Content-Type": "image/png"}, content=png(),
    )
    result = source_visuals.refresh_visual(HOME, client, settings, tmp_path)
    assert result["available"]
    assert calls == [HOME, "https://blog.example/icon.png"]


def test_private_homepage_and_redirect_are_blocked(tmp_path, network):
    routes, calls, client, settings = network
    assert not source_visuals.refresh_visual(
        "http://private.example/", client, settings, tmp_path,
    )["available"]
    assert calls == []
    routes[HOME] = page(f'<meta property="og:image" content="{IMAGE}">')
    routes[IMAGE] = httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
    assert not source_visuals.refresh_visual(HOME, client, settings, tmp_path)["available"]
    assert all("127.0.0.1" not in url for url in calls)


@pytest.mark.parametrize(("mime", "content"), [
    ("image/png", b"<html>not a picture</html>"),
    ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("image/png", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("text/html", png()),
    ("image/png", png(8192, 8192)),
    ("image/png", png()[:-1]),
    ("image/png", png() + b"<script>alert(1)</script>"),
])
def test_rejects_non_raster_mime_spoofing_and_oversized_dimensions(
    tmp_path, network, mime, content,
):
    routes, _, client, settings = network
    routes[HOME] = page(f'<meta property="og:image" content="{IMAGE}">')
    routes[IMAGE] = httpx.Response(200, headers={"Content-Type": mime}, content=content)
    assert not source_visuals.refresh_visual(HOME, client, settings, tmp_path)["available"]
    assert source_visuals.read_visual(HOME, tmp_path) is None


def test_download_budget_and_failed_refresh_preserve_cache(tmp_path, network):
    routes, calls, client, settings = network
    routes[HOME] = page(f'<meta property="og:image" content="{IMAGE}">')
    routes[IMAGE] = httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())
    assert source_visuals.refresh_visual(HOME, client, settings, tmp_path)["available"]
    original = source_visuals.read_visual(HOME, tmp_path)
    routes[IMAGE] = httpx.Response(
        200, headers={"Content-Type": "image/png"},
        content=b"x" * (source_visuals.MAX_RESPONSE_BYTES + 1),
    )
    calls.clear()
    result = source_visuals.refresh_visual(HOME, client, settings, tmp_path)
    assert result["available"] and result["version"] == original.version
    assert source_visuals.read_visual(HOME, tmp_path).path.read_bytes() == png()
    assert len(calls) <= source_visuals.MAX_REQUESTS


def test_cache_metadata_cannot_escape_cache_dir(tmp_path):
    key = hashlib.sha256(HOME.encode()).hexdigest()
    (tmp_path / f"{key}.json").write_text(json.dumps({
        "version": "../../outside", "media_type": "image/png",
    }))
    assert source_visuals.read_visual(HOME, tmp_path) is None


class Elements(HTMLParser):
    """记录真实渲染元素及祖先，不对模板/脚本源文本做断言。"""

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs, tuple(self.stack)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_sources_render_local_covers_and_contained_repair_dialog(tmp_path, monkeypatch, network):
    routes, calls, http_client, settings = network
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    sources_file.write("sources:\n" + sources_file.dump_entry(SourceConfig(
        id="mine", name="My Blog", type="rss", url="https://blog.example/feed", homepage=HOME,
    )) + "\n")
    conn = db.connect()
    try:
        db.upsert_sources(conn, config.load_sources())
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("secondbrain.web.app._http_client", lambda fs: http_client)
    routes[HOME] = page(f'<meta property="og:image" content="{IMAGE}">')
    routes[IMAGE] = httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())
    cache_dir = config.data_dir() / "source-visuals"
    source_visuals.refresh_visual(HOME, http_client, settings, cache_dir)
    calls.clear()
    with TestClient(create_app()) as web:
        response = web.get("/sources")
        assert response.status_code == 200
        assert calls == []
        dom = Elements(response.text).elements
        ids = {attrs.get("id") for _, attrs, _ in dom}
        assert {"following-list", "source-layout", "source-sort", "sources-repair-dialog"} <= ids
        assert "catalog" not in ids and "catalog-query" not in ids
        rows = [attrs for tag, attrs, _ in dom if tag == "article" and "data-source" in attrs]
        assert [row["data-source"] for row in rows] == ["mine"]
        repairs = [ancestors for _, attrs, ancestors in dom
                   if attrs.get("id") == "task-sources-repair"]
        assert repairs and ("dialog", "sources-repair-dialog") in repairs[0]
        images = [attrs for tag, attrs, _ in dom if tag == "img" and "data-source-visual" in attrs]
        assert len(images) == 1 and images[0]["src"].startswith("/sources/mine/visual?v=")
        image = web.get(images[0]["src"])
        assert image.status_code == 200 and image.content == png()
        assert image.headers["content-type"] == "image/png"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert calls == []
        refreshed = web.post("/sources/mine/visual")
        assert refreshed.status_code == 200
        assert refreshed.json()["available"]
        assert refreshed.json()["version"] == hashlib.sha256(png()).hexdigest()
        assert calls == [HOME, IMAGE]
