"""主页发现纯预览：离线 transport，禁止持久化和内网请求。"""

import gzip
import re
import socket

import httpx
import pytest

from secondbrain import config, db, sources_file
from secondbrain.models import FetchSettings
from secondbrain.net import HttpClient
from secondbrain.source_discovery import (
    MAX_ENTRIES,
    MAX_REQUESTS,
    discover_source,
    follow_source,
)
from secondbrain.sources import html_index

HOME = "https://site.example/"
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>Example updates</title><link>https://site.example/</link>
<item><title>First post</title><link>https://site.example/blog/first</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Example</title><id>https://site.example/</id><updated>2026-11-01T00:00:00Z</updated>
<entry><id>first</id><title>Atom post</title><link href="https://site.example/blog/atom"/>
<updated>2026-11-01T00:00:00Z</updated></entry></feed>"""
LIST = """<html><head><title>Example blog</title></head><body>
<nav><a href="/about">About</a><a href="/blog/login">Login</a></nav>
<a href="/blog/first" data-date="2026-11-01">First</a>
<a href="/blog/second">Second</a><a href="/blog/first#comments">Again</a>
<a href="https://other.example/blog/offsite">Other</a>
<a href="/blog/asset.svg">Asset</a><a href="/blog/third?login=1">Query</a>
<a href="javascript:alert(1)">Script</a><a href="/blog/LOGIN">Login again</a>
</body></html>"""


@pytest.fixture(autouse=True)
def public_dns_and_no_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))

    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not persist anything")

    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr(config, "_atomic_write", forbidden)
    monkeypatch.setattr(sources_file, "write", forbidden)
    yield
    assert list(tmp_path.iterdir()) == []


def _discover(routes, url=HOME, settings=None, *, apply=False, source_id=None):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        response = routes.get(str(request.url), httpx.Response(404))
        return response(request) if callable(response) else response

    settings = settings or FetchSettings(per_host_interval_seconds=0, retries=0)
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = (
            follow_source(url, client, settings, source_id=source_id)
            if apply else discover_source(url, client, settings)
        )
    return result, requests


@pytest.mark.parametrize("data,kind", [
    (RSS, "application/rss+xml"), (ATOM, "application/atom+xml"),
])
def test_discovers_advertised_feed(data, kind):
    page = f'<title>Example</title><link rel="alternate" type="{kind}" href="/feed">'
    result, requests = _discover({
        HOME: httpx.Response(200, text=page),
        HOME + "feed": httpx.Response(200, content=data),
    })
    assert set(result) == {"homepage", "title", "method", "source", "entries", "warnings"}
    assert result["method"] == "rss" and result["title"] == "Example"
    assert result["source"].url == HOME + "feed"
    assert result["source"].homepage == HOME and not result["source"].needs_scraper
    assert len(result["entries"]) == 1
    assert requests == [HOME, HOME + "feed"]  # 不抓取文章正文


def test_direct_feed_and_unsafe_entry_links():
    data = RSS.replace(b"</channel>", b"""<item><title>bad</title>
<link>http://127.0.0.1/private</link></item></channel>""")
    result, requests = _discover({HOME: httpx.Response(200, content=data)})
    assert result["method"] == "rss"
    assert [e.title for e in result["entries"]] == ["First post"]
    assert requests == [HOME]


def test_html_list_reuses_index_parser_with_safe_future_pattern():
    result, requests = _discover({HOME: httpx.Response(200, text=LIST)})
    source = result["source"]
    assert result["method"] == "html_index"
    assert [e.url for e in result["entries"]] == [HOME + "blog/first", HOME + "blog/second"]
    assert result["entries"][0].published_at.isoformat().startswith("2026-11-01")
    assert source.max_links == MAX_ENTRIES and source.url == HOME
    assert html_index.parse_index(LIST, HOME, source.url_pattern) == result["entries"]
    pattern = re.compile(source.url_pattern)
    assert pattern.search(HOME + "blog/future-post")
    for url in [
        HOME + "blog/login", HOME + "blog/LOGIN", HOME + "blog/about.html",
        HOME + "blog/file.pdf", HOME + "blog/post?next=login", HOME + "blog/%6cogin",
        "https://other.example/blog/post", "https://user@site.example/blog/post",
    ]:
        assert not pattern.search(url)
    assert requests == [HOME]
    assert result["warnings"]  # 推断不是经过用户确认的范围


def test_sitemap_fallback_reuses_existing_parser():
    xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://site.example/about</loc></url>
    <url><loc>https://site.example/blog/first</loc></url>
    <url><loc>https://site.example/blog/second</loc></url>
    <url><loc>https://other.example/blog/third</loc></url></urlset>"""
    result, requests = _discover({
        HOME: httpx.Response(200, text="<title>Example</title><p>Welcome</p>"),
        HOME + "sitemap.xml": httpx.Response(200, text=xml),
    })
    assert result["method"] == "sitemap"
    assert result["source"].url == HOME + "sitemap.xml"
    assert [e.url for e in result["entries"]] == [HOME + "blog/first", HOME + "blog/second"]
    assert not result["source"].use_lastmod
    assert requests == [HOME, HOME + "sitemap.xml"]


@pytest.mark.parametrize("page", [
    '<title>Sign in</title><input type="password"><a href="/blog/a">a</a><a href="/blog/b">b</a>',
    '<title>App</title><script>fetch("/api/private")</script><div id="app"></div>',
    '<title>Article</title><article>This is one article</article>'
    '<a href="/blog/related">Related</a>',
    '<title>Site</title><a href="/one">One</a><a href="/two">Two</a>',
])
def test_unsupported_pages_are_persistable_manual_browse_sources(page):
    result, requests = _discover({HOME: httpx.Response(200, text=page)})
    assert result["method"] == "bookmark" and result["entries"] == []
    assert result["source"].needs_scraper
    assert result["source"].homepage == HOME and result["source"].url == HOME
    assert result["source"].type == "rss"  # 既有跳过标记，不引入假 fetcher
    assert "手动浏览" in " ".join(result["warnings"])
    assert all("/api/" not in url for url in requests)
    if "password" in page:
        assert requests == [HOME]


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_inaccessible_public_homepage_remains_bookmark(status):
    result, requests = _discover({HOME: httpx.Response(status)})
    assert result["method"] == "bookmark"
    assert str(status) in " ".join(result["warnings"])
    assert requests == [HOME]


def test_ambiguous_sections_are_not_silently_selected():
    page = LIST + '<a href="/research/one">R1</a><a href="/research/two">R2</a>'
    result, _ = _discover({HOME: httpx.Response(200, text=page)})
    assert result["method"] == "bookmark"
    assert "多个文章路径" in " ".join(result["warnings"])


def test_sitemap_index_is_explicitly_unsupported():
    result, requests = _discover({
        HOME: httpx.Response(200, text="<title>Site</title>"),
        HOME + "sitemap.xml": httpx.Response(200, text="""<sitemapindex><sitemap>
        <loc>https://site.example/sub.xml</loc></sitemap></sitemapindex>"""),
    })
    assert result["method"] == "bookmark"
    assert "暂不支持 sitemap index" in " ".join(result["warnings"])
    assert HOME + "sub.xml" not in requests


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "ftp://site.example/", "https://u:p@site.example/",
    "http://127.0.0.1/", "http://[::1]/", "http://10.0.0.1/", "http://169.254.169.254/",
    "http://localhost/", "http://a.local/", "http://server/", "http://site.example:bad/",
])
def test_rejects_unsafe_initial_urls_without_request(url):
    result, requests = _discover({}, url)
    assert result["source"] is None and result["method"] == "rejected"
    assert result["warnings"] and requests == []


@pytest.mark.parametrize("target", ["http://127.0.0.1/secret", "https://u:p@site.example/", "file:///etc/passwd"])
def test_rejects_unsafe_redirect_before_request(target):
    result, requests = _discover({HOME: httpx.Response(302, headers={"location": target})})
    assert result["method"] == "rejected" and result["source"] is None
    assert requests == [HOME]


def test_rejects_private_dns_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
    ])
    result, requests = _discover({})
    assert result["source"] is None and requests == []
    assert "私有" in " ".join(result["warnings"])


def test_redirect_to_private_dns_resolution_is_not_requested(monkeypatch):
    def dns(host, port):
        address = "10.1.1.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    result, requests = _discover({
        HOME: httpx.Response(302, headers={"location": "https://private.example/"}),
    })
    assert result["source"] is None and requests == [HOME]


def test_chunked_body_size_is_bounded_without_content_length():
    consumed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for n in range(100):
                consumed.append(n)
                yield b"x" * 1024

    result, requests = _discover(
        {HOME: lambda request: httpx.Response(200, stream=Body())},
        settings=FetchSettings(per_host_interval_seconds=0, retries=0, content_max_chars=1000),
    )
    assert result["method"] == "bookmark"
    assert "下载上限" in " ".join(result["warnings"])
    assert len(consumed) < 100 and requests == [HOME]


def test_compressed_response_does_not_double_decode():
    result, _ = _discover({HOME: lambda request: httpx.Response(
        200, stream=httpx.ByteStream(gzip.compress(LIST.encode())),
        headers={"content-encoding": "gzip", "content-type": "text/html"},
    )})
    assert result["method"] == "html_index"


def test_link_and_entry_caps_and_deterministic_source_identity():
    page = "".join(f'<a href="/blog/post-{n}">Post {n}</a>' for n in range(600))
    result, requests = _discover({HOME: httpx.Response(200, text=page)})
    again, _ = _discover({HOME: httpx.Response(200, text=page)})
    assert len(result["entries"]) == MAX_ENTRIES
    assert "500" in " ".join(result["warnings"])
    assert result["source"].id == again["source"].id
    assert len(requests) <= MAX_REQUESTS


def test_feed_redirect_budget_is_shared_and_bounded():
    page = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="/feed-{n}">'
        for n in range(4)
    )
    routes = {HOME: httpx.Response(200, text=page)}
    for n in range(4):
        routes[HOME + f"feed-{n}"] = httpx.Response(302, headers={"location": f"/feed-{n}"})
    result, requests = _discover(routes)
    assert result["method"] == "bookmark" and len(requests) <= MAX_REQUESTS
    assert "上限" in " ".join(result["warnings"])


def test_follow_is_the_only_explicit_config_save_and_keeps_entries_out_of_db(monkeypatch):
    saved = []

    def edit(callback):
        text = callback("# Keep this comment\nsources: []\n")
        saved.append(text)
        return config.parse_sources_text(text)

    monkeypatch.setattr("secondbrain.ops.edit_sources", edit)
    routes = {HOME: httpx.Response(200, content=RSS)}
    preview, _ = _discover(routes)
    assert not saved
    applied, requests = _discover(routes, apply=True, source_id="my-site")
    assert applied["saved_id"] == "my-site" and len(saved) == 1
    assert saved[0].startswith("# Keep this comment\n")
    assert config.parse_sources_text(saved[0]) == [applied["source"]]
    assert preview["entries"] == applied["entries"]
    assert requests == [HOME]  # 显式保存重新发现，但不抓正文


def test_follow_saves_manual_browse_capability(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "secondbrain.ops.edit_sources",
        lambda callback: saved.extend(config.parse_sources_text(callback("sources: []\n"))),
    )
    result, _ = _discover({HOME: httpx.Response(403)}, apply=True)
    assert result["saved_id"] == result["source"].id
    assert len(saved) == 1 and saved[0].needs_scraper and saved[0].homepage == HOME


def test_follow_rejected_url_never_calls_config_save(monkeypatch):
    def forbidden(callback):
        raise AssertionError("unsafe URL must not create configuration")

    monkeypatch.setattr("secondbrain.ops.edit_sources", forbidden)
    result, requests = _discover({}, "http://localhost/", apply=True)
    assert result["saved_id"] is None and result["source"] is None and not requests


@pytest.mark.parametrize("source_id", ["UPPERCASE", "", "../escape"])
def test_follow_validates_explicit_source_id_before_saving(monkeypatch, source_id):
    def forbidden(callback):
        raise AssertionError("invalid source id must not create configuration")

    monkeypatch.setattr("secondbrain.ops.edit_sources", forbidden)
    with pytest.raises(ValueError):
        _discover({HOME: httpx.Response(200, content=RSS)}, apply=True, source_id=source_id)


def test_follow_does_not_overwrite_existing_source_id(monkeypatch):
    original = """# personal setting
sources:
  - id: my-site
    type: rss
    url: https://different.example/feed
    trust: 5
"""

    def edit(callback):
        callback(original)
        raise AssertionError("duplicate id should fail before returning replacement text")

    monkeypatch.setattr("secondbrain.ops.edit_sources", edit)
    with pytest.raises(ValueError, match="已存在"):
        _discover({HOME: httpx.Response(200, content=RSS)}, apply=True, source_id="my-site")
