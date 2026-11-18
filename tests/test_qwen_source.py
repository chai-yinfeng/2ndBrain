"""Qwen 公开响应结构的离线回归；不访问网络、不执行网页 JavaScript。"""

import copy
import json
import socket
from datetime import UTC, datetime

import httpx
import pytest
from conftest import fast_settings

from secondbrain.models import SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources import FETCHERS, qwen

# 公开 retrieval 响应的实际字段与元数据（2026-11-17 读取）；省略大段文章 HTML。
ARTICLE = {
    "id": "c6401188-9f83-4696-a2ed-036a837a7bda",
    "type": "qwen_ai",
    "title": (
        "SAPO: A Stable and Performant Reinforcement Learning Method "
        "for Training Large Language Models"
    ),
    "path": "sapo",
    "language": "en-US",
    "extra": {
        "description": "",
        "introduction": (
            "Reinforcement learning (RL) has become a core ingredient in advancing "
            "the reasoning capabilities of large language models (LLMs)."
        ),
        "tags": ["Research"],
        "date": "2025-12-05T04:00:00+08:00",
        "author": "QwenTeam",
    },
}
# 公开 research.research-list 中的真实旧记录；其余展示字段与图片地址省略。
LEGACY = {
    "id": "chinese-clip",
    "title": "Chinese CLIP: Contrastive Vision-Language Pretraining in Chinese",
    "date": "2022-12-24T06:54:19.000Z",
    "author": "QwenTeam",
    "tags": ["Research"],
    "description": (
        "However, we find that there is a necessity for a language-specific CLIP for "
        "applications, especially cross-modal retrieval, and there is no opensourced "
        "Chinese CLIP with good performance. We therefore launched this project to "
        "promote the Chinese multimodal representation learning."
    ),
    "tokenLinks": "https://docs.qwenlm.ai/research/chinese-clip/index.json",
}


def encoded(payload):
    return json.dumps(payload).encode()


def articles(*records):
    return encoded({"success": True, "data": {"articles": list(records)}})


def test_public_response_structure_and_dates():
    (entry,) = qwen.parse_articles(articles(ARTICLE))
    assert entry.url == "https://qwen.ai/blog?id=sapo"
    assert entry.title == ARTICLE["title"]
    assert entry.author == "QwenTeam"
    assert entry.summary == ARTICLE["extra"]["introduction"]
    assert entry.published_at == datetime(2025, 12, 4, 20, tzinfo=UTC)
    assert entry.keep_date and not entry.page_meta
    assert entry.feed_content is None
    (old,) = qwen.parse_legacy(encoded([LEGACY]))
    assert old.url == "https://qwen.ai/blog?id=chinese-clip"
    assert old.title == LEGACY["title"]
    assert old.summary == LEGACY["description"]
    assert old.published_at == datetime(2022, 12, 24, 6, 54, 19, tzinfo=UTC)


def test_content_is_data_and_description_precedes_introduction():
    record = copy.deepcopy(ARTICLE)
    record["content"] = "<article><p>Research text.</p><script>doNotExecute()</script></article>"
    record["extra"]["description"] = "A <b>published</b> description."
    (entry,) = qwen.parse_articles(articles(record))
    assert entry.feed_content == record["content"]
    assert entry.summary == "A published description."


def test_research_page_group_is_not_research_tag():
    release = copy.deepcopy(ARTICLE)
    release["extra"]["tags"] = ["Release"]
    unrelated = copy.deepcopy(ARTICLE)
    unrelated.update(type="news", path="other-news")
    translated = copy.deepcopy(ARTICLE)
    translated.update(language="zh-CN", path="other-language")
    assert len(qwen.parse_articles(articles(release, unrelated, translated))) == 1
    with pytest.raises(FetchError, match="分组"):
        qwen.parse_articles(articles(unrelated, translated))


def test_legacy_frontend_whitelist_excludes_migrated_configuration_records():
    migrated = copy.deepcopy(LEGACY)
    migrated["id"] = "qwen-deepresearch"  # 实际配置表有此记录，当前前端从新 API 合并它
    unrelated = copy.deepcopy(LEGACY)
    unrelated["id"] = "news-item"
    assert len(qwen.LEGACY_RESEARCH_IDS) == 53
    assert [e.url for e in qwen.parse_legacy(encoded([LEGACY, migrated, unrelated]))] == [
        "https://qwen.ai/blog?id=chinese-clip"
    ]
    release = copy.deepcopy(LEGACY)
    release.update(id="qwen", tags=["Release"])
    assert len(qwen.parse_legacy(encoded([release]))) == 1


def test_legacy_links_are_not_fetch_targets():
    record = copy.deepcopy(LEGACY)
    record.update(tokenLinks="http://127.0.0.1/private", htmlLink="javascript:alert(1)")
    assert qwen.parse_legacy(encoded([record]))[0].url == "https://qwen.ai/blog?id=chinese-clip"


@pytest.mark.parametrize(
    "path",
    ["https://evil.example/x", "//evil.example/x", "../x", "x/y", "x?evil=1", "x#frag",
     "x%2Fy", "x\\y", "javascript:alert(1)", ".", "..", " x", "x\n"],
)
def test_unsafe_article_paths_fail(path):
    record = copy.deepcopy(ARTICLE)
    record["path"] = path
    with pytest.raises(FetchError, match="安全"):
        qwen.parse_articles(articles(record))


@pytest.mark.parametrize("payload", [b"", b"<html>SPA shell</html>", b"{", b"\xff"])
def test_non_json_responses_have_specific_diagnostic(payload):
    with pytest.raises(FetchError, match="JSON"):
        qwen.parse_articles(payload)


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"success": False}, {"success": True}, {"success": True, "data": []},
     {"success": True, "data": {"articles": {}}},
     {"success": True, "data": {"articles": [None]}}],
)
def test_malformed_envelopes_fail(payload):
    with pytest.raises(FetchError, match="Qwen Research"):
        qwen.parse_articles(encoded(payload))


@pytest.mark.parametrize("field", ["path", "title", "type", "language", "extra"])
def test_missing_required_fields_fail(field):
    record = copy.deepcopy(ARTICLE)
    record.pop(field)
    with pytest.raises(FetchError, match="Qwen Research"):
        qwen.parse_articles(articles(record))


@pytest.mark.parametrize("field", ["date", "author", "description", "introduction"])
def test_malformed_metadata_fields_fail(field):
    record = copy.deepcopy(ARTICLE)
    record["extra"][field] = ["not text"]
    with pytest.raises(FetchError, match=field):
        qwen.parse_articles(articles(record))


@pytest.mark.parametrize("date", ["not a date", "0001-01-01", "2999-01-01", None])
def test_invalid_or_missing_dates_remain_unknown(date):
    record = copy.deepcopy(ARTICLE)
    record["extra"]["date"] = date
    assert qwen.parse_articles(articles(record))[0].published_at is None


def test_empty_deduplicated_and_bounded_lists():
    assert qwen.parse_articles(articles()) == []
    assert qwen.parse_legacy(encoded([])) == []
    assert len(qwen.parse_articles(articles(ARTICLE, ARTICLE))) == 1
    assert len(qwen.parse_legacy(encoded([LEGACY, LEGACY]))) == 1
    with pytest.raises(FetchError, match="上限"):
        qwen.parse_articles(articles(*([ARTICLE] * (qwen.MAX_RESEARCH_ENTRIES + 1))))
    for payload in ({}, [None], [{}]):
        with pytest.raises(FetchError):
            qwen.parse_legacy(encoded(payload))


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )


def source():
    return SourceConfig(id="qwen-research", type="qwen_research", url=qwen.ARTICLES_URL,
                        homepage=qwen.HOMEPAGE)


def test_fetch_registered_and_shares_guarded_two_request_reader(public_dns):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        body = articles(ARTICLE) if str(request.url) == qwen.ARTICLES_URL else encoded([LEGACY])
        return httpx.Response(200, content=body)

    settings = fast_settings()
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert FETCHERS["qwen_research"] is qwen.fetch
        assert len(qwen.fetch(source(), client, settings)) == 2
    assert calls == [qwen.ARTICLES_URL, qwen.LEGACY_URL]


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://evil.example/api"])
def test_fetch_rejects_cross_origin_or_private_redirects(public_dns, target):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    settings = fast_settings()
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            qwen.fetch(source(), client, settings)
    assert calls == [qwen.ARTICLES_URL]


def test_fetch_rejects_unverified_same_origin_endpoint_redirect(public_dns):
    def handler(request):
        if str(request.url) == qwen.ARTICLES_URL:
            return httpx.Response(302, headers={"location": "/api/other"})
        return httpx.Response(200, content=articles(ARTICLE))

    settings = fast_settings()
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError, match="跳转"):
            qwen.fetch(source(), client, settings)


def test_fetch_enforces_response_budget(public_dns):
    settings = fast_settings(discovery_response_max_bytes=100)
    with HttpClient(
        settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=articles(ARTICLE))
        )
    ) as client:
        with pytest.raises(FetchError, match="上限"):
            qwen.fetch(source(), client, settings)


def test_collect_does_not_request_unverified_urls():
    class NoRequests:
        def get(self, *args, **kwargs):
            raise AssertionError("unverified endpoint requested")

    for url in ("https://evil.example/api", qwen.LEGACY_URL, qwen.ARTICLES_URL + "&type=news"):
        with pytest.raises(FetchError, match="只允许"):
            qwen.collect_research(NoRequests(), url)


def test_collect_orders_reliable_dates_before_unknown_stably():
    newer = copy.deepcopy(ARTICLE)
    newer.update(path="qwen4-max", title="Qwen4-Max: Your New Teammate Has Arrived")
    newer["extra"]["date"] = "2026-11-16T10:00:00+08:00"
    unknown = copy.deepcopy(ARTICLE)
    unknown.update(path="unknown-date", title="Unknown date")
    unknown["extra"].pop("date")
    old_unknown = copy.deepcopy(LEGACY)
    old_unknown["id"] = "ofa"
    old_unknown.pop("date")
    bodies = {
        qwen.ARTICLES_URL: articles(unknown, ARTICLE, newer),
        qwen.LEGACY_URL: encoded([old_unknown, LEGACY]),
    }

    class Reader:
        def get(self, url, *, origin=None):
            assert origin == ("https", "qwen.ai")
            return httpx.Response(200, content=bodies[url], request=httpx.Request("GET", url))

    entries = qwen.collect_research(Reader())
    assert [entry.url.rsplit("=", 1)[-1] for entry in entries] == [
        "qwen4-max", "sapo", "chinese-clip", "unknown-date", "ofa"
    ]


def test_legacy_unrelated_group_has_specific_diagnostic():
    record = copy.deepcopy(LEGACY)
    record["id"] = "other-section"
    with pytest.raises(FetchError, match="分组"):
        qwen.parse_legacy(encoded([record]))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/blog?id=sapo",
        "https://qwen.ai.evil.example/blog?id=sapo",
        "https://qwen.ai@evil.example/blog?id=sapo",
        "http://qwen.ai/blog?id=sapo",
        "https://qwen.ai:443/blog?id=sapo",
        "https://qwen.ai/blog/?id=sapo",
        "https://qwen.ai/blog?id=../sapo",
        "https://qwen.ai/blog?id=%73apo",
        "https://qwen.ai/blog?id=sapo&extra=1",
        "https://qwen.ai/blog?id=sapo#fragment",
        "https://qwen.ai/blog?id=",
        "https://qwen.ai/blog?id=sapo\n",
    ],
)
def test_resolve_noncanonical_or_unsafe_urls_never_request(url):
    class NoRequests:
        def get(self, *args, **kwargs):
            raise AssertionError("noncanonical article requested")

    assert qwen.resolve_article(url, NoRequests(), fast_settings()) is None


@pytest.mark.parametrize(
    ("slug", "expected_body"),
    [("sapo", "<article><p>Public article body.</p></article>"),
     ("chinese-clip", None), ("missing-article", None)],
)
def test_resolve_matches_actual_metadata_and_body(public_dns, slug, expected_body):
    record = copy.deepcopy(ARTICLE)
    record["content"] = "<article><p>Public article body.</p></article>"
    calls = []

    def handler(request):
        endpoint = str(request.url)
        calls.append(endpoint)
        payload = articles(record) if endpoint == qwen.ARTICLES_URL else encoded([LEGACY])
        return httpx.Response(200, content=payload)

    settings = fast_settings()
    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        if slug == "missing-article":
            with pytest.raises(FetchError, match="未找到文章 missing-article"):
                qwen.resolve_article(f"https://qwen.ai/blog?id={slug}", client, settings)
        else:
            entry = qwen.resolve_article(f"https://qwen.ai/blog?id={slug}", client, settings)
            assert entry.url == f"https://qwen.ai/blog?id={slug}"
            assert entry.title == (ARTICLE if slug == "sapo" else LEGACY)["title"]
            assert entry.feed_content == expected_body
            assert entry.summary
    assert calls == [qwen.ARTICLES_URL, qwen.LEGACY_URL]


def test_resolve_keeps_existing_reader_budget(public_dns):
    from secondbrain.source_discovery import _Reader

    settings = fast_settings()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=articles(ARTICLE))

    with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        reader = _Reader(client, settings)
        reader.remaining = 1
        with pytest.raises(FetchError, match="上限"):
            qwen.resolve_article("https://qwen.ai/blog?id=sapo", reader, settings)
    assert calls == [qwen.ARTICLES_URL]
