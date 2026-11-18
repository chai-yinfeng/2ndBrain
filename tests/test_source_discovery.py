"""主页发现纯预览：离线 transport，禁止持久化和内网请求。"""

import gzip
import re
import socket

import httpx
import pytest

from secondbrain import config, db, sources_file
from secondbrain.models import FetchSettings, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.source_discovery import (
    MAX_ENTRIES,
    MAX_REQUESTS,
    discover_source,
    follow_source,
    preview_source,
    source_matches,
)
from secondbrain.sources import FETCHERS, html_index, sitemap

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


def _discover(routes, url=HOME, settings=None, *, apply=False, source_id=None, name=None):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        response = routes.get(str(request.url), httpx.Response(404))
        return response(request) if callable(response) else response

    settings = settings or FetchSettings(per_host_interval_seconds=0, retries=0)
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = (
            follow_source(url, client, settings, source_id=source_id, name=name)
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
    assert result["status"] == "ready" and result["matched_from"] == "generic"
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


def test_sitemap_index_reuses_bounded_adapter():
    result, requests = _discover({
        HOME: httpx.Response(200, text="<title>Site</title>"),
        HOME + "sitemap.xml": httpx.Response(200, text="""<sitemapindex><sitemap>
        <loc>https://site.example/sub.xml</loc></sitemap></sitemapindex>"""),
        HOME + "sub.xml": httpx.Response(200, text="""<urlset>
        <url><loc>https://site.example/blog/one</loc></url>
        <url><loc>https://site.example/blog/two</loc></url></urlset>"""),
    })
    assert result["method"] == "sitemap" and result["status"] == "ready"
    assert result["source"].url == HOME + "sitemap.xml"
    assert requests == [HOME, HOME + "sitemap.xml", HOME + "sub.xml"]


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
        settings=FetchSettings(
            per_host_interval_seconds=0, retries=0, discovery_response_max_bytes=4000
        ),
    )
    assert result["method"] == "bookmark"
    assert result["status"] == "incomplete"
    assert result["diagnostics"][0]["kind"] == "response_limit"
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
    result, _ = _discover(
        {HOME: httpx.Response(200, text="<title>Manual site</title>")}, apply=True
    )
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


def _with_followed(monkeypatch, sources):
    original = config.load_sources
    monkeypatch.setattr(
        config, "load_sources", lambda path=None: sources if path is None else original(path)
    )


def _preview(source, routes, settings=None):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    settings = settings or FetchSettings(per_host_interval_seconds=0, retries=0)
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = preview_source(source, client, settings)
    return result, requests


HF_HOME = "https://huggingface.co/papers"
HF_API = "https://huggingface.co/api/daily_papers"
HF_PAPERS = [{
    "paper": {"id": "2611.00001", "title": "A real paper", "summary": "Evidence"},
    "publishedAt": "2026-11-01T00:00:00Z",
}]
DEEPMIND_HOME = "https://deepmind.google/blog/"
DEEPMIND_RSS = "https://deepmind.google/blog/rss.xml"


@pytest.mark.parametrize("homepage,endpoint,method,response", [
    (HF_HOME, HF_API, "hf_papers", httpx.Response(200, json=HF_PAPERS)),
    (DEEPMIND_HOME, DEEPMIND_RSS, "rss", httpx.Response(200, content=RSS)),
])
def test_known_adapter_precedes_homepage_guessing(
    monkeypatch, homepage, endpoint, method, response,
):
    result, requests = _discover({endpoint: response}, homepage)
    assert result["method"] == method and result["status"] == "ready"
    assert result["matched_from"] == "catalog"
    assert result["matched_source_id"] == result["source"].id
    assert requests == [endpoint]
    assert result["entries"]


def test_hf_preview_matches_url_discovery_and_real_fetcher():
    source = SourceConfig(id="papers", type="hf_papers", url=HF_API, homepage=HF_HOME)
    preview, requests = _preview(source, {HF_API: httpx.Response(200, json=HF_PAPERS)})
    discovered, discovered_requests = _discover(
        {HF_API: httpx.Response(200, json=HF_PAPERS)}, HF_HOME
    )
    assert preview["entries"] == discovered["entries"]
    assert preview["entries"][0].url == "https://arxiv.org/abs/2611.00001"
    assert requests == discovered_requests == [HF_API]


def test_current_usable_config_precedes_catalog(monkeypatch):
    source = SourceConfig(
        id="my-papers", type="rss", url=HOME + "my-feed", homepage=HF_HOME,
        name="My choice", tags=["personal"], enabled=False, trust=5,
    )
    _with_followed(monkeypatch, [source])
    result, requests = _discover({source.url: httpx.Response(200, content=RSS)}, HF_HOME)
    assert result["source"] == source and result["matched_from"] == "followed"
    assert requests == [source.url] and result["title"] == "My choice"


def test_existing_bookmark_repaired_without_replacing_identity_or_manual_fields(monkeypatch):
    original = """# personal order
sources:
  - id: my-papers
    name: 我的论文
    type: rss
    url: https://huggingface.co/papers
    homepage: https://huggingface.co/papers
    needs_scraper: true
    tags: [manual-domain]
    trust: 5  # chosen
    enabled: false
    role: expansion
  - id: another
    type: rss
    url: https://another.example/feed
"""
    sources = config.parse_sources_text(original)
    _with_followed(monkeypatch, sources)
    saved = []

    def save(callback):
        text = callback(original)
        saved.append(text)
        return config.parse_sources_text(text)

    monkeypatch.setattr("secondbrain.ops.edit_sources", save)
    result, requests = _discover(
        {HF_API: httpx.Response(200, json=HF_PAPERS)}, HF_HOME, apply=True, name="Not an overwrite",
    )
    updated = result["source"]
    assert updated.id == result["saved_id"] == "my-papers"
    assert updated.type == "hf_papers" and updated.url == HF_API and not updated.needs_scraper
    for field in ("name", "tags", "trust", "enabled", "role", "max_age_days"):
        assert getattr(updated, field) == getattr(sources[0], field)
    assert [source.id for source in config.parse_sources_text(saved[0])] == ["my-papers", "another"]
    assert "# personal order" in saved[0] and "# chosen" in saved[0]
    assert requests == [HF_API]


def test_follow_existing_source_is_idempotent_and_id_change_requires_edit(monkeypatch):
    source = SourceConfig(id="mine", type="rss", url=HOME, name="Chosen", tags=["mine"])
    _with_followed(monkeypatch, [source])
    original = "sources:\n" + sources_file.dump_entry(source) + "\n"
    saved = []
    monkeypatch.setattr(
        "secondbrain.ops.edit_sources", lambda callback: saved.append(callback(original))
    )
    result, _ = _discover({HOME: httpx.Response(200, content=RSS)}, apply=True)
    assert result["saved_id"] == "mine" and result["source"].name == "Chosen"
    assert len(config.parse_sources_text(saved[0])) == 1
    with pytest.raises(ValueError, match="正式编辑接口"):
        _discover({HOME: httpx.Response(200, content=RSS)}, apply=True, source_id="new-id")
    assert len(saved) == 1


@pytest.mark.parametrize("homepage,endpoint", [
    (HF_HOME, HF_API), (DEEPMIND_HOME, DEEPMIND_RSS),
])
def test_known_adapter_failure_preserves_existing_bookmark(monkeypatch, homepage, endpoint):
    bookmark = SourceConfig(
        id="personal-id", type="rss", url=homepage, homepage=homepage, needs_scraper=True,
        name="Manual name", tags=["curated"], trust=4, enabled=False,
    )
    _with_followed(monkeypatch, [bookmark])
    result, requests = _discover({endpoint: httpx.Response(503)}, homepage)
    assert result["status"] == "failed" and result["method"] == "bookmark"
    assert result["source"] == bookmark
    assert "503" in " ".join(result["warnings"])
    assert requests == [endpoint]


def test_empty_feed_invalid_feed_and_manual_are_distinct():
    source = SourceConfig(id="feed", type="rss", url=HOME)
    empty, _ = _preview(source, {HOME: httpx.Response(
        200, content=b'<rss version="2.0"><channel><title>No posts</title></channel></rss>'
    )})
    failed, _ = _preview(source, {HOME: httpx.Response(200, text="<title>Not feed</title>")})
    manual, _ = _discover({HOME: httpx.Response(200, text="<title>App</title>")})
    assert empty["status"] == "empty" and empty["source"].name == "No posts"
    assert failed["status"] == "failed" and failed["source"].type == "rss"
    assert not failed["source"].needs_scraper
    assert manual["status"] == "manual" and manual["source"].needs_scraper


def test_follow_fills_default_display_name_from_feed_and_respects_explicit_name(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "secondbrain.ops.edit_sources",
        lambda callback: saved.append(config.parse_sources_text(callback("sources: []\n"))),
    )
    default, _ = _discover({HOME: httpx.Response(200, content=RSS)}, apply=True)
    explicit, _ = _discover(
        {HOME: httpx.Response(200, content=RSS)}, apply=True, name="Personal display",
    )
    assert default["source"].name == "Example updates"
    assert explicit["source"].name == "Personal display"


@pytest.mark.parametrize("left,right,expected", [
    ("https://SITE.example:443/blog/", HOME + "blog", True),
    (HOME + "bl%6Fg#top", HOME + "blog/", True),
    (HOME + "feed?section=ai&lang=en", HOME + "feed?lang=en&section=ai", True),
    (HOME + "blog", HOME + "research", False),
    (HOME + "feed?section=ai", HOME + "feed?section=physics", False),
    ("https://www.site.example/blog", HOME + "blog", False),
    ("http://site.example/blog", HOME + "blog", False),
])
def test_source_identity_retains_paths_queries_and_unproven_aliases(left, right, expected):
    assert source_matches(left, right) is expected


def test_rss_display_root_does_not_merge_different_columns():
    blog = SourceConfig(id="blog", type="rss", url=HOME + "blog/feed")
    research = SourceConfig(id="research", type="rss", url=HOME + "research/feed")
    assert not source_matches(blog, research)
    assert not source_matches(blog, HOME)
    assert not source_matches(
        SourceConfig(id="arxiv-ai", type="arxiv", query="cat:cs.AI"),
        SourceConfig(id="arxiv-ml", type="arxiv", query="cat:cs.AI AND cat:cs.LG"),
    )


def test_existing_config_does_not_bypass_private_url_or_redirect_checks():
    source = SourceConfig(id="unsafe", type="rss", url="http://127.0.0.1/feed", homepage=HOME)
    direct, direct_requests = _preview(source, {})
    assert direct["status"] == "rejected" and direct["source"] == source
    assert direct_requests == []
    source = source.model_copy(update={"url": HOME + "feed"})
    redirect, requests = _preview(source, {
        source.url: httpx.Response(302, headers={"location": "http://169.254.169.254/meta"}),
    })
    assert redirect["status"] == "rejected" and redirect["source"] == source
    assert requests == [source.url]


def _index(*urls):
    return "<sitemapindex>" + "".join(
        f"<sitemap><loc>{url}</loc></sitemap>" for url in urls
    ) + "</sitemapindex>"


def _urlset(*urls):
    return "<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"


def test_nested_sitemap_cycles_are_bounded_and_saved_index_can_fetch():
    root, nested, leaf = [HOME + name for name in ("sitemap.xml", "nested.xml", "leaf.xml")]
    routes = {
        HOME: httpx.Response(200, text="<title>Site</title>"),
        root: httpx.Response(200, text=_index(root, nested)),
        nested: httpx.Response(200, text=_index(leaf)),
        leaf: httpx.Response(200, text=_urlset(HOME + "blog/one", HOME + "blog/two")),
    }
    discovered, requests = _discover(routes)
    assert discovered["method"] == "sitemap" and discovered["source"].url == root
    assert requests == [HOME, root, nested, leaf]
    assert "循环" in " ".join(discovered["warnings"])
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return routes[str(request.url)]

    settings = FetchSettings(per_host_interval_seconds=0, retries=0)
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        entries = FETCHERS["sitemap"](discovered["source"], client, settings)
    assert entries == discovered["entries"]
    assert fetched == [root, nested, leaf]


def test_direct_sitemap_index_does_not_refetch_root():
    root, child = HOME + "sitemap.xml", HOME + "child.xml"
    result, requests = _discover({
        root: httpx.Response(200, text=_index(child), headers={"content-type": "application/xml"}),
        child: httpx.Response(200, text=_urlset(HOME + "blog/one", HOME + "blog/two")),
    }, root)
    assert result["method"] == "sitemap" and requests == [root, child]


def test_sitemap_depth_and_request_limits_are_explicit():
    urls = [HOME + f"level-{number}.xml" for number in range(5)]
    source = SourceConfig(id="index", type="sitemap", url=urls[0])
    result, requests = _preview(source, {
        url: httpx.Response(200, text=_index(urls[index + 1]))
        for index, url in enumerate(urls[:-1])
    })
    assert result["status"] == "empty"
    assert len(requests) == sitemap.MAX_SITEMAP_DEPTH + 1
    assert "层" in " ".join(result["warnings"])
    children = [HOME + f"child-{number}.xml" for number in range(20)]
    result, requests = _preview(source, {
        urls[0]: httpx.Response(200, text=_index(*children)),
        **{url: httpx.Response(200, text=_urlset(HOME + "blog/" + str(index)))
           for index, url in enumerate(children)},
    })
    assert result["status"] == "ready"
    assert len(requests) == sitemap.MAX_SITEMAP_REQUESTS <= MAX_REQUESTS
    assert "最多读取" in " ".join(result["warnings"])


@pytest.mark.parametrize("target", [
    "http://127.0.0.1/index.xml", "https://other.example/index.xml",
])
def test_nested_sitemap_never_fetches_private_or_cross_origin_documents(target):
    root = HOME + "sitemap.xml"
    source = SourceConfig(id="index", type="sitemap", url=root)
    result, requests = _preview(source, {root: httpx.Response(200, text=_index(target))})
    assert result["status"] in {"failed", "rejected"} and result["source"] == source
    assert requests == [root]


def test_nested_sitemap_redirect_checks_private_dns(monkeypatch):
    root, child = HOME + "sitemap.xml", HOME + "child.xml"

    def dns(host, port):
        address = "10.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    source = SourceConfig(id="index", type="sitemap", url=root)
    result, requests = _preview(source, {
        root: httpx.Response(200, text=_index(child)),
        child: httpx.Response(302, headers={"location": "https://private.example/index.xml"}),
    })
    assert result["status"] == "rejected" and requests == [root, child]


def test_homepage_scope_wins_over_other_sitemap_article_paths():
    root = HOME + "sitemap.xml"
    result, _ = _discover({
        HOME + "blog/": httpx.Response(200, text="<title>Blog</title>"),
        root: httpx.Response(200, text=_urlset(
            HOME + "blog/one", HOME + "blog/two",
            HOME + "research/one", HOME + "research/two",
        )),
    }, HOME + "blog/")
    assert result["method"] == "sitemap"
    assert [entry.url for entry in result["entries"]] == [HOME + "blog/one", HOME + "blog/two"]
    assert not re.search(result["source"].url_pattern, HOME + "research/future")


def test_shared_index_with_distinct_article_filters_is_not_one_source():
    left = SourceConfig(
        id="blog", type="sitemap", url=HOME + "sitemap.xml",
        homepage=HOME + "blog/", url_pattern=r"/blog/",
    )
    right = SourceConfig(
        id="research", type="sitemap", url=HOME + "sitemap.xml",
        homepage=HOME + "research/", url_pattern=r"/research/",
    )
    assert not source_matches(left, right)


def test_explicit_preview_uses_supplied_rules_not_followed_homepage_match(monkeypatch):
    current = SourceConfig(
        id="personal-feed", type="rss", url=HOME + "feed", homepage=HF_HOME,
        name="Personal feed",
    )
    _with_followed(monkeypatch, [current])
    explicit = SourceConfig(
        id="advanced-form", type="html_index", url=HOME + "list", homepage=HF_HOME,
        name="Explicit display", url_pattern=r"/blog/second$", max_links=1,
    )
    result, requests = _preview(explicit, {explicit.url: httpx.Response(200, text=LIST)})
    assert result["source"] == explicit and result["method"] == "html_index"
    assert result["title"] == "Explicit display"
    assert [entry.url for entry in result["entries"]] == [HOME + "blog/second"]
    assert requests == [explicit.url]
    assert result["matched_source_id"] == "advanced-form"


def test_proven_public_redirect_can_reach_known_adapter_without_guessing_www_alias():
    alias = "https://www.huggingface.co/papers"
    result, requests = _discover({
        alias: httpx.Response(302, headers={"location": HF_HOME}),
        HF_HOME: httpx.Response(200, text="<title>Papers</title>"),
        HF_API: httpx.Response(200, json=HF_PAPERS),
    }, alias)
    assert result["method"] == "hf_papers" and result["matched_from"] == "catalog"
    assert requests == [alias, HF_HOME, HF_API]


def test_known_adapter_has_same_shared_request_and_body_limits():
    source = SourceConfig(id="papers", type="hf_papers", url=HF_API, homepage=HF_HOME)
    oversized, requests = _preview(
        source, {HF_API: httpx.Response(200, content=b"x" * 5000)},
        FetchSettings(
            per_host_interval_seconds=0, retries=0, discovery_response_max_bytes=4000
        ),
    )
    assert oversized["status"] == "incomplete" and oversized["source"] == source
    assert oversized["diagnostics"][0]["kind"] == "response_limit"
    assert requests == [HF_API]
    looping, requests = _preview(source, {
        HF_API: httpx.Response(302, headers={"location": HF_API}),
    })
    assert looping["status"] == "failed" and len(requests) <= MAX_REQUESTS
    assert looping["source"] == source


def test_existing_bookmark_can_be_repaired_by_generic_feed_discovery(monkeypatch):
    bookmark = SourceConfig(
        id="kept-id", type="rss", url=HOME, homepage=HOME, needs_scraper=True,
        tags=["human"], enabled=False, trust=5,
    )
    _with_followed(monkeypatch, [bookmark])
    result, requests = _discover({HOME: httpx.Response(200, content=RSS)})
    assert result["source"].id == "kept-id" and not result["source"].needs_scraper
    assert result["source"].name == "Example updates"
    assert result["source"].tags == ["human"] and not result["source"].enabled
    assert result["matched_from"] == "followed" and requests == [HOME]


@pytest.mark.parametrize("name", [" ", "x" * 201])
def test_follow_validates_display_name_without_saving(monkeypatch, name):
    def forbidden(callback):
        raise AssertionError("invalid name must not save")

    monkeypatch.setattr("secondbrain.ops.edit_sources", forbidden)
    with pytest.raises(ValueError):
        _discover({HOME: httpx.Response(200, content=RSS)}, apply=True, name=name)


def test_homepage_subsection_does_not_fall_back_to_another_article_scope():
    homepage = HOME + "blog/"
    result, _ = _discover({
        homepage: httpx.Response(200, text=(
            '<title>Blog</title><a href="/research/one">One</a>'
            '<a href="/research/two">Two</a>'
        )),
        HOME + "sitemap.xml": httpx.Response(200, text=_urlset(
            HOME + "research/one", HOME + "research/two",
        )),
    }, homepage)
    assert result["method"] == "bookmark" and result["status"] == "manual"
    assert "未自动扩大" in " ".join(result["warnings"])


@pytest.mark.parametrize("papers,status", [(HF_PAPERS, "ready"), ([], "empty")])
def test_stale_followed_adapter_repaired_only_after_validated_catalog_preview(
    monkeypatch, papers, status,
):
    current = SourceConfig(
        id="my-papers", type="rss", url=HOME + "stale-feed", homepage=HF_HOME,
        name="My papers", tags=["human"], trust=5, enabled=False, role="expansion",
    )
    _with_followed(monkeypatch, [current])
    original = "# keep\nsources:\n" + sources_file.dump_entry(current) + "\n"
    saved = []
    monkeypatch.setattr(
        "secondbrain.ops.edit_sources", lambda callback: saved.append(callback(original))
    )
    result, requests = _discover({
        current.url: httpx.Response(503),
        HF_API: httpx.Response(200, json=papers),
    }, HF_HOME, apply=True)
    assert result["status"] == status and result["saved_id"] == "my-papers"
    assert result["source"].type == "hf_papers" and result["source"].url == HF_API
    for field in ("id", "name", "tags", "trust", "enabled", "role", "max_age_days"):
        assert getattr(result["source"], field) == getattr(current, field)
    assert "503" in " ".join(result["warnings"])
    assert requests == [current.url, HF_API]
    assert config.parse_sources_text(saved[0]) == [result["source"]]


def test_failed_current_and_catalog_adapters_keep_original_config_and_diagnostics(monkeypatch):
    current = SourceConfig(
        id="my-papers", type="rss", url=HOME + "stale-feed", homepage=HF_HOME,
        tags=["human"], enabled=False, trust=4,
    )
    _with_followed(monkeypatch, [current])

    def forbidden(callback):
        raise AssertionError("failed previews must not write configuration")

    monkeypatch.setattr("secondbrain.ops.edit_sources", forbidden)
    result, requests = _discover({
        current.url: httpx.Response(503),
        HF_API: httpx.Response(502),
    }, HF_HOME, apply=True, name="Do not fill on failure")
    assert result["status"] == "failed" and result["saved_id"] is None
    assert result["source"] == current and result["method"] == "rss"
    warnings = " ".join(result["warnings"])
    assert "503" in warnings and "502" in warnings and "hf_papers" in warnings
    assert requests == [current.url, HF_API]


@pytest.mark.parametrize("case", ["generic", "catalog", "bookmark"])
def test_follow_never_saves_any_failed_preview(monkeypatch, case):
    homepage = HOME if case == "generic" else HF_HOME
    endpoint = HOME if case == "generic" else HF_API
    if case == "bookmark":
        current = SourceConfig(
            id="mine", type="rss", url=HF_HOME, homepage=HF_HOME, needs_scraper=True,
        )
        _with_followed(monkeypatch, [current])

    def forbidden(callback):
        raise AssertionError("failed preview must not save or return a saved ID")

    monkeypatch.setattr("secondbrain.ops.edit_sources", forbidden)
    result, requests = _discover({endpoint: httpx.Response(503)}, homepage, apply=True)
    assert result["status"] == "failed" and result["saved_id"] is None
    assert requests == [endpoint]


def test_failed_adapter_fallback_shares_one_request_budget(monkeypatch):
    current = SourceConfig(
        id="mine", type="rss", url=HOME + "old-feed", homepage=HF_HOME, name="Keep",
    )
    adapters = [
        SourceConfig(id="candidate-one", type="hf_papers", url=HF_API, homepage=HF_HOME),
        SourceConfig(
            id="candidate-two", type="hf_papers", url=HOME + "another-api", homepage=HF_HOME,
        ),
    ]
    monkeypatch.setattr(
        config, "load_sources", lambda path=None: [current] if path is None else adapters
    )
    result, requests = _discover({
        current.url: httpx.Response(302, headers={"location": current.url}),
        HF_API: httpx.Response(302, headers={"location": HF_API}),
        adapters[1].url: httpx.Response(200, json=HF_PAPERS),
    }, HF_HOME)
    assert result["status"] == "incomplete" and result["source"] == current
    assert any(d["kind"] == "request_limit" for d in result["diagnostics"])
    assert len(requests) == MAX_REQUESTS
    assert adapters[1].url not in requests
    assert "上限" in " ".join(result["warnings"])


def test_failed_preview_does_not_retry_identical_catalog_fetch_rules(monkeypatch):
    current = SourceConfig(
        id="my-papers", type="hf_papers", url=HF_API, homepage=HF_HOME,
    )
    _with_followed(monkeypatch, [current])
    result, requests = _discover({HF_API: httpx.Response(503)}, HF_HOME)
    assert result["status"] == "failed" and result["source"] == current
    assert requests == [HF_API]
