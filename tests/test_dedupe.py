import httpx
import pytest
from conftest import NOW, fixture_bytes

from secondbrain import db
from secondbrain.ingest import fetch_sources
from secondbrain.models import SourceConfig
from secondbrain.urls import canonicalize_url, content_hash

BLOG_FEED = "https://blog.example.com/feed.xml"
NOTES_FEED = "https://notes.example.org/atom.xml"
ARXIV_API = "https://export.arxiv.org/api/query"
HF_API = "https://huggingface.co/api/daily_papers"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "http://Blog.Example.com/posts/a/?utm_source=rss&utm_medium=feed#intro",
            "https://blog.example.com/posts/a",
        ),
        ("https://example.com/p?b=2&a=1&fbclid=x", "https://example.com/p?a=1&b=2"),
        ("https://example.com:443/p/", "https://example.com/p"),
        ("https://example.com:8080/p", "https://example.com:8080/p"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/p?id=7#comments", "https://example.com/p?id=7"),
    ],
)
def test_canonicalize_url(raw, expected):
    assert canonicalize_url(raw) == expected


def test_content_hash_normalizes_whitespace_and_case():
    assert content_hash("Title", "Some   body\ntext") == content_hash("title ", "some body text")
    assert content_hash("A", "") != content_hash("B", "")


def _rss(**kw) -> SourceConfig:
    return SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=7, **kw)


def _routes():
    return {
        BLOG_FEED: httpx.Response(200, content=fixture_bytes("rss.xml")),
        NOTES_FEED: httpx.Response(200, content=fixture_bytes("atom.xml")),
        "https://blog.example.com/posts/continuous-batching": httpx.Response(
            200, content=fixture_bytes("article.html")
        ),
        ARXIV_API: httpx.Response(200, content=fixture_bytes("arxiv_api.xml")),
        HF_API: httpx.Response(200, content=fixture_bytes("hf_daily_papers.json")),
    }


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    yield c
    c.close()


def test_second_fetch_adds_nothing(conn, make_client):
    client, _ = make_client(_routes(), min_feed_content_chars=50)
    sources = [_rss(), SourceConfig(id="notes", type="rss", url=NOTES_FEED, max_age_days=7)]

    first = fetch_sources(conn, sources, client, client.settings, now=NOW)
    # rss 的旧文章被时间窗过滤
    assert [(r.source_id, r.status, r.added) for r in first] == [
        ("blog", "ok", 2),
        ("notes", "ok", 2),
    ]
    second = fetch_sources(conn, sources, client, client.settings, now=NOW)
    assert [(r.added, r.duplicates) for r in second] == [(0, 2), (0, 2)]
    urls = {r["url"] for r in db.list_items(conn)}
    raw = "http://Blog.Example.com/posts/paged-kv-cache/?utm_source=rss&utm_medium=feed#intro"
    assert raw in urls
    stored = conn.execute("SELECT url_canonical, extract_method FROM items ORDER BY id").fetchall()
    assert ("https://blog.example.com/posts/paged-kv-cache", "feed") in [tuple(r) for r in stored]
    assert ("https://blog.example.com/posts/continuous-batching", "html") in [
        tuple(r) for r in stored
    ]


def test_limit_is_per_source(conn, make_client):
    client, _ = make_client(_routes(), min_feed_content_chars=50)
    sources = [_rss(), SourceConfig(id="notes", type="rss", url=NOTES_FEED, max_age_days=7)]
    results = fetch_sources(conn, sources, client, client.settings, limit=1, now=NOW)
    assert [r.added for r in results] == [1, 1]
    titles = {r["title"] for r in db.list_items(conn)}
    # 取的是每个信源最新的一条
    assert titles == {"Paged KV cache explained", "Why Raft needs leader leases"}
    # 同样的 limit 重复运行：新增 0，不会逐次补抓更旧的条目
    results = fetch_sources(conn, sources, client, client.settings, limit=1, now=NOW)
    assert [(r.added, r.duplicates) for r in results] == [(0, 1), (0, 1)]
    # 放开 limit 后补上其余条目
    results = fetch_sources(conn, sources, client, client.settings, now=NOW)
    assert [(r.added, r.duplicates) for r in results] == [(1, 1), (1, 1)]


def test_cross_source_duplicate_paper(conn, make_client):
    client, _ = make_client(_routes())
    sources = [
        SourceConfig(id="arxiv", type="arxiv", query="cat:cs.DC", max_age_days=7),
        SourceConfig(id="hf", type="hf_papers", url=HF_API, max_age_days=7),
    ]
    results = fetch_sources(conn, sources, client, client.settings, now=NOW)
    assert [(r.added, r.duplicates) for r in results] == [(2, 0), (1, 1)]
    # arXiv HTML 与 PDF 都 404 → 用摘要代替并计数
    assert results[0].extract_failed == 2
    assert len(db.list_items(conn)) == 3


def test_same_content_different_url_is_duplicate(conn, make_client):
    mirror = "https://mirror.example.com/feed.xml"
    routes = _routes() | {mirror: httpx.Response(200, content=fixture_bytes("atom.xml"))}
    client, _ = make_client(routes)
    sources = [
        SourceConfig(id="notes", type="rss", url=NOTES_FEED, max_age_days=7),
        SourceConfig(id="mirror", type="rss", url=mirror, max_age_days=7),
    ]
    results = fetch_sources(conn, sources, client, client.settings, now=NOW)
    # mirror 的条目 URL 相同 → 按 URL 去重
    assert [(r.added, r.duplicates) for r in results] == [(2, 0), (0, 2)]

    def rewrite(request):
        body = fixture_bytes("atom.xml").replace(b"notes.example.org", b"copy.example.net")
        return httpx.Response(200, content=body)

    client2, _ = make_client({"https://copy.example.net/feed": rewrite})
    copy = SourceConfig(id="copy", type="rss", url="https://copy.example.net/feed", max_age_days=7)
    (result,) = fetch_sources(conn, [copy], client2, client2.settings, now=NOW)
    # URL 不同、标题与正文相同 → 按 content_hash 去重
    assert (result.added, result.duplicates) == (0, 2)


def test_failures_are_isolated_and_skips_make_no_requests(conn, make_client):
    client, router = make_client(_routes(), min_feed_content_chars=50)
    sources = [
        SourceConfig(id="broken", type="rss", url="https://down.example.com/feed"),
        SourceConfig(id="scrape", type="rss", url="https://s.example.com", needs_scraper=True),
        SourceConfig(id="off", type="rss", url="https://off.example.com/feed", enabled=False),
        SourceConfig(id="yt", type="youtube", url="https://youtube.example.com/feed"),
        _rss(),
    ]
    results = fetch_sources(conn, sources, client, client.settings, now=NOW)
    by_id = {r.source_id: r for r in results}
    assert by_id["broken"].status == "failed"
    assert "HTTP 404" in by_id["broken"].message
    assert by_id["scrape"].status == "skipped"
    assert "待支持" in by_id["scrape"].message
    assert by_id["off"].status == "skipped"
    # M5 起 youtube 信源会正常抓取（这里的地址不存在 → 失败，且不影响其他信源）
    assert by_id["yt"].status == "failed"
    assert (by_id["blog"].status, by_id["blog"].added) == ("ok", 2)
    hosts = {r.url.host for r in router.requests}
    assert not hosts & {"s.example.com", "off.example.com"}


def test_retries_transient_errors(conn, make_client):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=fixture_bytes("atom.xml"))

    client, _ = make_client({NOTES_FEED: flaky}, retries=2)
    src = SourceConfig(id="notes", type="rss", url=NOTES_FEED, max_age_days=7)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert (result.status, result.added) == ("ok", 2)


def test_gives_up_after_retries(conn, make_client):
    client, router = make_client({NOTES_FEED: httpx.Response(503)}, retries=2)
    src = SourceConfig(id="notes", type="rss", url=NOTES_FEED)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert result.status == "failed"
    assert "HTTP 503" in result.message
    assert router.urls().count(NOTES_FEED) == 3


def test_sources_are_stored_with_new_fields(conn, make_client):
    client, _ = make_client(_routes())
    src = SourceConfig(
        id="blog", type="rss", url=BLOG_FEED, role="expansion", backfill=True, tags=["x"]
    )
    fetch_sources(conn, [src], client, client.settings, now=NOW)
    row = conn.execute("SELECT role, backfill, needs_scraper, tags FROM sources").fetchone()
    assert tuple(row) == ("expansion", 1, 0, '["x"]')


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "brain.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()
