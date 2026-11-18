"""发现预算、范围确认与正文提取分离的消费者可见回归。"""

import gzip
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from secondbrain import config, ops, sources_file
from secondbrain.models import FetchSettings, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.source_discovery import discover_source, follow_source, source_matches
from secondbrain.sources import qwen, rss
from secondbrain.web.app import create_app

HOME = "https://scope.example/research/"
FEED = "https://scope.example/news/rss.xml"
PAGE = '<title>Research</title><a href="/index/release">Release</a><a href="/index/study">Study</a>'
RSS = b'''<rss version="2.0"><channel><title>Official News</title>
<link>https://scope.example/news/</link>
<item><title>Release</title><link>https://scope.example/index/release</link><category>Product</category></item>
<item><title>Study</title><link>https://scope.example/index/study</link><category>Research</category></item>
<item><title>Company</title><link>https://scope.example/index/company</link><category>Company</category></item>
</channel></rss>'''


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])


def client_for(routes, settings=None):
    settings = settings or FetchSettings(retries=0, per_host_interval_seconds=0)
    def request(req):
        return routes.get(str(req.url), httpx.Response(404))
    return HttpClient(settings, transport=httpx.MockTransport(request))


def setup_feed_catalog():
    reference = SourceConfig(id="official-news", type="rss", url=FEED,
                             homepage="https://scope.example/news/")
    (config.config_dir() / "sources.example.yaml").write_text(
        "sources:\n" + sources_file.dump_entry(reference) + "\n", encoding="utf-8")
    original = SourceConfig(id="my-research", name="我的研究关注", type="rss", url=HOME,
                            homepage=HOME, needs_scraper=True, tags=["人工"], trust=5,
                            enabled=False)
    sources_file.write("sources:\n" + sources_file.dump_entry(original) + "\n")
    return original


def test_download_budget_does_not_use_content_window():
    page = '<a href="/blog/one">One</a><a href="/blog/two">Two</a>' + " " * 4000
    settings = FetchSettings(content_max_chars=1, discovery_response_max_bytes=5000,
                             discovery_total_max_bytes=6000, per_host_interval_seconds=0)
    with client_for({"https://scope.example/": httpx.Response(200, text=page)}, settings) as client:
        result = discover_source("https://scope.example/", client, settings)
    assert result["method"] == "html_index" and result["status"] == "ready"
    assert result["budget"]["downloaded_bytes"] == len(page)
    assert settings.content_max_chars == 1


def test_decoded_total_budget_stops_second_response_and_reports_request():
    page = '<link rel="alternate" type="application/rss+xml" href="/feed">' + " " * 80
    feed = b'<rss version="2.0"><channel><title>Feed</title></channel></rss>' + b' ' * 200
    settings = FetchSettings(discovery_response_max_bytes=500,
                             discovery_total_max_bytes=len(page) + len(feed) - 1,
                             per_host_interval_seconds=0)
    routes = {
        "https://scope.example/": httpx.Response(200, text=page),
        "https://scope.example/feed": httpx.Response(200, content=gzip.compress(feed),
                                                   headers={"content-encoding": "gzip"}),
    }
    with client_for(routes, settings) as client:
        result = discover_source("https://scope.example/", client, settings)
    assert result["status"] == "incomplete"
    diagnostic = next(d for d in result["diagnostics"] if d["kind"] == "total_limit")
    assert diagnostic["url"] == "https://scope.example/feed"
    assert diagnostic["phase"] == "页面声明的 RSS"


def test_budget_error_never_saves_source_and_redacts_query_values():
    url = "https://scope.example/?token=not-for-logs"
    settings = FetchSettings(discovery_response_max_bytes=20, per_host_interval_seconds=0)
    with client_for({url: httpx.Response(200, content=b'x' * 21)}, settings) as client:
        result = follow_source(url, client, settings)
    assert result["status"] == "incomplete" and result["saved_id"] is None
    assert config.load_sources() == []
    assert "not-for-logs" not in json.dumps(result["diagnostics"])
    assert "not-for-logs" not in " ".join(result["warnings"])


def test_html_sitemap_is_diagnosed_before_xml_parse():
    routes = {"https://scope.example/": httpx.Response(200, text="<title>Site</title>"),
              "https://scope.example/sitemap.xml": httpx.Response(
                  200, text="<html><body>SPA shell</body></html>",
                  headers={"content-type": "text/html"})}
    with client_for(routes) as client:
        result = discover_source("https://scope.example/", client, client.settings)
    assert any(d["kind"] == "format" and d["url"].endswith("/sitemap.xml")
               for d in result["diagnostics"])


def test_scope_candidate_requires_choice_and_filters_real_fetch():
    original = setup_feed_catalog()
    before = (config.config_dir() / "sources.yaml").read_bytes()
    routes = {HOME: httpx.Response(200, text=PAGE), FEED: httpx.Response(200, content=RSS)}
    with client_for(routes) as client:
        result = discover_source(HOME, client, client.settings)
        assert result["method"] == "bookmark" and len(result["candidates"]) == 2
        assert (config.config_dir() / "sources.yaml").read_bytes() == before
        narrow = next(c for c in result["candidates"] if c["scope_categories"])
        assert not narrow["scope_confirmed"]
        saved = follow_source(HOME, client, client.settings, source_id=original.id,
                              candidate_key=narrow["key"])
        assert saved["saved_id"] == original.id
        source = config.load_sources()[0]
        assert (source.id, source.name, source.tags, source.enabled) == (
            original.id, original.name, original.tags, original.enabled)
        assert source.homepage == HOME and source.url == FEED
        assert source.rss_categories == ["Research"] and source.scope_note
        assert [entry.title for entry in rss.fetch(source, client, client.settings)] == ["Study"]
        assert not source_matches(source, SourceConfig(id="all-news", type="rss", url=FEED))


def test_web_scope_preview_is_readonly_and_stale_key_cannot_write(monkeypatch):
    setup_feed_catalog()
    before = (config.config_dir() / "sources.yaml").read_bytes()
    routes = {HOME: httpx.Response(200, text=PAGE), FEED: httpx.Response(200, content=RSS)}
    monkeypatch.setattr("secondbrain.web.app._http_client", lambda fs: client_for(routes, fs))
    with TestClient(create_app()) as client:
        preview = client.post("/sources/home/preview", data={"url": HOME})
        assert preview.status_code == 200 and "candidate_key" in preview.text
        rejected = client.post("/sources/home/follow", data={"url": HOME,
                                                           "candidate_key": "stale"})
        assert rejected.status_code == 400
    assert (config.config_dir() / "sources.yaml").read_bytes() == before


def test_qwen_manual_material_uses_public_article_not_spa(monkeypatch):
    record = {"path": "sample", "type": "qwen_ai", "language": "en-US",
              "title": "Real research", "extra": {"date": "2026-11-01"},
              "content": "<article><p>Actual research body from public data.</p></article>"}
    routes = {qwen.ARTICLES_URL: httpx.Response(
        200, json={"success": True, "data": {"articles": [record]}}),
        qwen.LEGACY_URL: httpx.Response(200, json=[])}
    monkeypatch.setattr(ops, "HttpClient", lambda fs: client_for(routes, fs))
    result = ops.add_manual(["https://qwen.ai/blog?id=sample"], lambda _: None, state="later")
    item_id = result["results"][0]["item_id"]
    from secondbrain import db
    conn = db.connect()
    row = db.item_detail(conn, item_id)
    assert row["title"] == "Real research"
    assert "Actual research body" in row["original_text"]
    assert row["reading_state"] == "unread" and row["content_completeness"] == "full"
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    conn.close()
