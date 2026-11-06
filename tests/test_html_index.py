"""M12（ADR 0020）：html_index 信源（解析博客列表页）与信源目录的 homepage。"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import fast_settings, fixture_bytes
from fastapi.testclient import TestClient

from secondbrain import config, db
from secondbrain.ingest import fetch_sources
from secondbrain.models import SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources import html_index
from secondbrain.sources.homepage import safe_url, source_homepage
from secondbrain.web.app import create_app

INDEX = "https://www.sglang.io/blog"
PATTERN = r"^https://www\.sglang\.io/blog/[^/?#]+$"
NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
ARTICLE = (
    "<html><head><title>HiCache deep dive | SGLang</title>"
    '<meta property="og:title" content="HiCache deep dive">'
    '<meta property="article:published_time" content="2026-10-20T00:00:00Z"></head>'
    "<body><article>"
    + "".join(f"<p>Paragraph {n} about hierarchical KV caching in SGLang.</p>" for n in range(30))
    + "</article></body></html>"
)


def test_parse_index_fixture():
    entries = html_index.parse_index(
        fixture_bytes("sglang_blog_index.html").decode(), INDEX, PATTERN
    )
    urls = [e.url for e in entries]
    assert len(urls) == 13 and len(set(urls)) == 13
    assert urls[0] == "https://www.sglang.io/blog/kimi-k3.1-pro-day0-support"  # 页面顺序
    assert INDEX not in urls and all(u.startswith(INDEX + "/") for u in urls)  # 列表页本身被排除
    hicache = next(e for e in entries if e.url.endswith("/hicache"))
    assert hicache.published_at == datetime(2026, 10, 19, tzinfo=UTC)  # 来自 <a data-post-date>
    assert hicache.title == "hicache" and hicache.page_meta
    capped = html_index.parse_index(
        fixture_bytes("sglang_blog_index.html").decode(), INDEX, PATTERN, max_links=3
    )
    assert len(capped) == 3


def test_parse_index_relative_links_dedupe_and_time_tag():
    html = (
        '<a href="/p/a#comments">A</a><a href="/p/a">A again</a>'
        '<a href="p/b"><time datetime="2026-11-01">Nov 1</time> B</a>'
        '<a href="https://other.example.com/p/c">C</a>'
    )
    entries = html_index.parse_index(html, "https://x.example.com/", r"^https://x\.example\.com/p/")
    assert [e.url for e in entries] == ["https://x.example.com/p/a", "https://x.example.com/p/b"]
    assert entries[0].published_at is None
    assert entries[1].published_at == datetime(2026, 11, 1, tzinfo=UTC)


def test_config_validation():
    with pytest.raises(ValueError, match="需要 url_pattern"):
        SourceConfig(id="a", type="html_index", url="https://x")
    with pytest.raises(ValueError, match="max_links"):
        SourceConfig(id="a", type="rss", url="https://x", max_links=3)
    src = SourceConfig(id="a", type="html_index", url=INDEX, url_pattern=PATTERN, max_links=5)
    assert source_homepage(src) == INDEX  # 主页兜底为列表页本身


def _client(routes):
    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    return HttpClient(fast_settings(), transport=httpx.MockTransport(handler))


def test_fetch_uses_page_date_and_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    routes = {INDEX: httpx.Response(200, content=fixture_bytes("sglang_blog_index.html"))}
    routes[INDEX + "/kimi-k3.1-pro-day0-support"] = httpx.Response(200, text=ARTICLE)
    routes[INDEX + "/minimax-m3.1-flash-day0-support"] = httpx.Response(
        200,
        text=ARTICLE.replace("HiCache deep dive", "MiniMax day 0").replace(
            "2026-10-20", "2026-10-29"
        ),
    )
    client = _client(routes)
    src = SourceConfig(
        id="sglang-blog", type="html_index", url=INDEX, url_pattern=PATTERN, max_age_days=3650
    )
    conn = db.connect()
    (result,) = fetch_sources(conn, [src], client, client.settings, limit=2, now=NOW)
    rows = conn.execute("SELECT title, published_at FROM items ORDER BY id").fetchall()
    conn.close()
    assert result.added == 2
    assert [(r["title"], r["published_at"]) for r in rows] == [
        ("HiCache deep dive", "2026-10-20T00:00:00Z"),  # 正文页 meta 的日期优先于列表页
        ("MiniMax day 0", "2026-10-29T00:00:00Z"),
    ]


def test_fetch_without_matching_links_fails(tmp_path):
    client = _client({INDEX: httpx.Response(200, text="<a href='/about'>about</a>")})
    src = SourceConfig(id="s", type="html_index", url=INDEX, url_pattern=PATTERN)
    with pytest.raises(FetchError, match="没有匹配"):
        html_index.fetch(src, client, client.settings)


def test_catalog_lists_sglang_with_homepage_and_peek(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    (config.config_dir() / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    routes = {INDEX: httpx.Response(200, content=fixture_bytes("sglang_blog_index.html"))}
    monkeypatch.setattr("secondbrain.web.app._http_client", lambda fs: _client(routes))
    client = TestClient(create_app())
    html = client.get("/sources").text
    item = html.split('data-catalog="sglang-blog"', 1)[1].split("</div>", 1)[0]
    assert f'href="{INDEX}"' in item
    peek = client.post("/sources/sglang-blog/peek").text
    assert "解析到 13 条" in peek and "kimi-k3.1-pro-day0-support" in peek and "2026-10-19" in peek


def test_every_example_source_has_explicit_safe_homepage():
    sources = config.load_sources(config.config_dir() / "sources.example.yaml")
    assert sources and all(s.homepage and safe_url(s.homepage) for s in sources)
    by_id = {s.id: s.homepage for s in sources}
    assert by_id["anthropic-research"] == "https://www.anthropic.com/research"
    assert by_id["anthropic-news"] == "https://www.anthropic.com/news"
    assert by_id["lmsys-sglang-blog"] == "https://lmsys.org/blog/"
    assert by_id["arxiv-cs-dc"] == "https://arxiv.org/list/cs.DC/recent"
    assert by_id["gpu-mode"].startswith("https://www.youtube.com/channel/")


@pytest.mark.live
def test_live_sglang_index():
    src = next(
        s
        for s in config.load_sources(config.config_dir() / "sources.example.yaml")
        if s.id == "sglang-blog"
    )
    with HttpClient(fast_settings()) as client:
        entries = html_index.fetch(src, client, client.settings)
    assert len(entries) >= 5 and all(e.url.startswith(INDEX + "/") for e in entries)
    assert NOW - timedelta(days=3650) < (entries[0].published_at or NOW)
