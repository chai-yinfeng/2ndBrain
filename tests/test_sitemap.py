"""sitemap 信源（ADR 0012）：URL 筛选、发布时间来源、最新 N、去重 / 幂等。"""

from datetime import UTC, datetime

import httpx
import pytest
from conftest import NOW, fast_settings, fixture_bytes

from secondbrain import db
from secondbrain.extract.html import extract_page_meta
from secondbrain.ingest import fetch_sources
from secondbrain.models import SourceConfig
from secondbrain.net import FetchError
from secondbrain.sources.sitemap import date_in_url, parse_sitemap

BLOG_SITEMAP = "https://blog.example.com/sitemap.xml"
BLOG_PATTERN = r"^https://blog\.example\.com/blog/\d{4}-\d{2}-\d{2}-"
LAB_SITEMAP = "https://lab.example.com/sitemap.xml"
LAB_PATTERN = r"^https://lab\.example\.com/news/(?!product$)[^/]+$"

ARTICLE = fixture_bytes("sitemap_article.html").decode()
ARTICLE_DATE_META = '<meta property="article:published_time" content="2026-11-01T00:00:00Z">'


def _page(title: str, published: str | None = None) -> httpx.Response:
    html = ARTICLE.replace("Hierarchical KV cache for distributed serving", title)
    if published is None:
        html = html.replace(ARTICLE_DATE_META, "")
    else:
        html = html.replace("2026-11-01T00:00:00Z", published)
    return httpx.Response(200, text=html)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    yield c
    c.close()


# ---------- 解析 ----------


def test_url_pattern_keeps_only_dated_posts():
    entries = parse_sitemap(fixture_bytes("sitemap_dated.xml"), BLOG_PATTERN)
    urls = [e.url for e in entries]
    assert "https://blog.example.com/blog" not in urls  # 列表页
    assert "https://blog.example.com/about" not in urls
    assert urls == [
        "https://blog.example.com/blog/2026-10-20-radix-cache",
        "https://blog.example.com/blog/2026-11-02-hicache",
        "https://blog.example.com/blog/2026-10-28-spec-decoding",
        "https://blog.example.com/blog/2025-01-15-old-post",
    ]
    assert all(e.page_meta for e in entries)
    assert entries[1].title == "2026-11-02-hicache"  # 抓正文页前用 slug 占位


def test_without_pattern_keeps_every_url():
    assert len(parse_sitemap(fixture_bytes("sitemap_dated.xml"))) == 7


def test_publish_date_comes_from_url_not_lastmod():
    entries = parse_sitemap(fixture_bytes("sitemap_dated.xml"), BLOG_PATTERN, use_lastmod=True)
    # 所有 lastmod 都是同一个构建时间；URL 中的日期优先
    assert entries[1].published_at == datetime(2026, 11, 2, tzinfo=UTC)
    assert entries[3].published_at == datetime(2025, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.org/blog/2026-11-02-hicache", datetime(2026, 11, 2, tzinfo=UTC)),
        ("https://x.org/2024/05/01/post/", datetime(2024, 5, 1, tzinfo=UTC)),
        ("https://x.org/blog/2026-13-01-bad-month", None),
        ("https://x.org/blog/2026/11-02-mixed", None),
        ("https://x.org/blog/hicache", None),
        ("https://x.org/blog/12026-11-02-too-many-digits", None),
        ("https://x.org/blog/2099-01-01-future", None),  # 不合理（晚于当前）
        ("https://2026-11-02.x.org/post", None),  # 只看路径
    ],
)
def test_date_in_url(url, expected):
    assert date_in_url(url) == expected


def test_lastmod_is_used_only_when_enabled():
    data = fixture_bytes("sitemap_lastmod.xml")
    without = parse_sitemap(data, LAB_PATTERN)
    assert [e.published_at for e in without] == [None] * 4
    with_lastmod = {e.url: e.published_at for e in parse_sitemap(data, LAB_PATTERN, True)}
    assert "https://lab.example.com/news/product" not in with_lastmod  # 分类页由正则排除
    assert "https://lab.example.com/research/interp" not in with_lastmod
    assert with_lastmod["https://lab.example.com/news/new-model"] == datetime(
        2026, 11, 3, 17, 9, 8, tzinfo=UTC
    )
    assert "https://lab.example.com/news" not in with_lastmod  # 列表页


def test_sitemap_without_lastmod_value_stays_undated():
    data = fixture_bytes("sitemap_lastmod.xml")
    (entry,) = parse_sitemap(data, r"/news/product$", use_lastmod=True)
    assert entry.published_at is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"<html><body>not xml", "不是有效的 sitemap XML"),
        (
            b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/'
            b'sitemap/0.9"><sitemap><loc>https://x.org/s1.xml</loc></sitemap></sitemapindex>',
            "sitemap index",
        ),
        (b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>', "根元素为 <rss>"),
    ],
)
def test_parse_sitemap_errors(data, message):
    with pytest.raises(FetchError, match=message):
        parse_sitemap(data)


def test_page_meta_extraction():
    meta = extract_page_meta(ARTICLE, "https://lab.example.com/news/x")
    assert meta.title == "Hierarchical KV cache for distributed serving"
    assert meta.author == "Alice Chen"
    assert meta.date == "2026-11-01"


# ---------- 配置 ----------


def test_sitemap_fields_are_validated():
    with pytest.raises(ValueError, match="只用于 sitemap"):
        SourceConfig(id="a", type="rss", url="https://x.org/feed", url_pattern="x")
    with pytest.raises(ValueError, match="只用于 sitemap"):
        SourceConfig(id="a", type="rss", url="https://x.org/feed", use_lastmod=True)
    with pytest.raises(ValueError, match="不是有效的正则"):
        SourceConfig(id="a", type="sitemap", url=BLOG_SITEMAP, url_pattern="(")
    with pytest.raises(ValueError, match="需要 url"):
        SourceConfig(id="a", type="sitemap")
    src = SourceConfig(id="a", type="sitemap", url=BLOG_SITEMAP)
    assert src.url_pattern is None and src.use_lastmod is False


# ---------- 采集 ----------


def _blog_routes() -> dict:
    return {
        BLOG_SITEMAP: httpx.Response(200, content=fixture_bytes("sitemap_dated.xml")),
        "https://blog.example.com/blog/2026-11-02-hicache": _page("HiCache deep dive"),
        "https://blog.example.com/blog/2026-10-28-spec-decoding": _page("Spec decoding notes"),
        "https://blog.example.com/blog/2026-10-20-radix-cache": _page("Radix cache revisited"),
    }


def _blog_source(**kw) -> SourceConfig:
    return SourceConfig(
        id="blog", type="sitemap", url=BLOG_SITEMAP, url_pattern=BLOG_PATTERN, max_age_days=30, **kw
    )


def _rows(conn):
    return conn.execute(
        "SELECT url, title, author, published_at, extract_method FROM items ORDER BY id"
    ).fetchall()


def test_fetch_takes_newest_n_and_fills_page_meta(conn, make_client):
    client, router = make_client(_blog_routes())
    (result,) = fetch_sources(conn, [_blog_source()], client, client.settings, limit=2, now=NOW)
    assert result.status == "ok" and result.added == 2
    rows = _rows(conn)
    assert [r["title"] for r in rows] == ["HiCache deep dive", "Spec decoding notes"]
    assert rows[0]["author"] == "Alice Chen"
    # URL 中的日期优先于页面日期（页面写的是 2026-11-01）
    assert rows[0]["published_at"] == "2026-11-02T00:00:00Z"
    assert rows[0]["extract_method"] == "html"
    # 只抓了最新 2 篇的正文页；时间窗外的旧文与第三篇没有请求
    assert router.urls() == [
        BLOG_SITEMAP,
        "https://blog.example.com/blog/2026-11-02-hicache",
        "https://blog.example.com/blog/2026-10-28-spec-decoding",
    ]


def test_fetch_is_idempotent_and_skips_known_pages(conn, make_client):
    client, router = make_client(_blog_routes())
    fetch_sources(conn, [_blog_source()], client, client.settings, limit=5, now=NOW)
    assert len(_rows(conn)) == 3  # 2025 年的旧文在时间窗外
    router.requests.clear()
    (again,) = fetch_sources(conn, [_blog_source()], client, client.settings, limit=5, now=NOW)
    assert again.added == 0 and again.duplicates == 3
    assert router.urls() == [BLOG_SITEMAP]  # 已入库的 URL 不再抓正文页


def test_lastmod_selects_candidates_and_page_date_wins(conn, make_client):
    routes = {
        LAB_SITEMAP: httpx.Response(200, content=fixture_bytes("sitemap_lastmod.xml")),
        "https://lab.example.com/news/new-model": _page("A new model", "2026-11-01T00:00:00Z"),
        "https://lab.example.com/news/refreshed-old-post": _page(
            "An old post", "2025-03-01T00:00:00Z"
        ),
        "https://lab.example.com/news/partnership": _page("A partnership"),  # 页面无日期
    }
    client, router = make_client(routes)
    src = SourceConfig(
        id="lab",
        type="sitemap",
        url=LAB_SITEMAP,
        url_pattern=r"^https://lab\.example\.com/news/(?!product$)[^/]+$",
        use_lastmod=True,
        max_age_days=14,
    )
    fetch_sources(conn, [src], client, client.settings, limit=10, now=NOW)
    dates = {r["title"]: r["published_at"] for r in _rows(conn)}
    assert dates == {
        "A new model": "2026-11-01T00:00:00Z",  # 页面日期覆盖 lastmod
        "An old post": "2025-03-01T00:00:00Z",  # lastmod 被刷新过，页面日期才是真实发布时间
        "A partnership": "2026-10-30T21:40:46Z",  # 页面无日期，退回 lastmod
    }
    assert "https://lab.example.com/news/ancient" not in router.urls()  # 时间窗外


def test_undated_sitemap_pages_follow_existing_non_article_rule(conn, make_client):
    sitemap = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://essays.example.com/faq</loc></url>
<url><loc>https://essays.example.com/classic-essay</loc></url>
</urlset>"""
    essay = (
        b"<html><body><article>"
        + b"<p>A long classic essay paragraph about systems.</p>" * 120
        + b"</article></body></html>"
    )
    routes = {
        "https://essays.example.com/sitemap.xml": httpx.Response(200, content=sitemap),
        "https://essays.example.com/faq": httpx.Response(
            200, content=b"<html><body><p>Contact me.</p></body></html>"
        ),
        "https://essays.example.com/classic-essay": httpx.Response(200, content=essay),
    }
    client, _ = make_client(routes)
    src = SourceConfig(
        id="essays", type="sitemap", url="https://essays.example.com/sitemap.xml", backfill=True
    )
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    rows = conn.execute("SELECT url, is_backfill FROM items").fetchall()
    assert [(r["url"], r["is_backfill"]) for r in rows] == [
        ("https://essays.example.com/classic-essay", 1)
    ]
    assert any("疑似非文章页" in n and "faq" in n for n in result.notes)


@pytest.mark.live
def test_live_lmsys_sitemap():
    from secondbrain import config
    from secondbrain.net import HttpClient
    from secondbrain.sources import sitemap

    sources = config.load_sources(config.CONFIG_DIR / "sources.example.yaml")
    (src,) = [s for s in sources if s.id == "lmsys-sglang-blog"]
    settings = fast_settings()
    with HttpClient(settings) as client:
        entries = sitemap.fetch(src, client, settings)
    assert len(entries) > 10
    assert all(e.published_at is not None for e in entries)
    assert all("/blog/" in e.url and not e.url.endswith("/blog") for e in entries)


def test_example_anthropic_research_pattern():
    from secondbrain import config

    sources = config.load_sources(config.CONFIG_DIR / "sources.example.yaml")
    (src,) = [s for s in sources if s.id == "anthropic-research"]
    assert src.type == "sitemap" and src.use_lastmod and not src.needs_scraper
    data = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.anthropic.com/research</loc><lastmod>2026-10-13T18:34:55Z</lastmod></url>
<url><loc>https://www.anthropic.com/research/team/alignment</loc>
<lastmod>2026-10-18T19:42:27Z</lastmod></url>
<url><loc>https://www.anthropic.com/research/managed-agents</loc>
<lastmod>2026-10-30T15:51:07Z</lastmod></url>
<url><loc>https://www.anthropic.com/news/managed-agents</loc>
<lastmod>2026-10-30T15:51:07Z</lastmod></url>
</urlset>"""
    entries = parse_sitemap(data, src.url_pattern, src.use_lastmod)
    assert [e.url for e in entries] == ["https://www.anthropic.com/research/managed-agents"]
