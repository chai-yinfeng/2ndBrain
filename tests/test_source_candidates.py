from datetime import UTC, datetime
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
import pytest
from conftest import fast_settings

from secondbrain.models import SourceConfig
from secondbrain.net import FetchError
from secondbrain.source_candidates import feed_scope_candidate
from secondbrain.sources import rss

HOME = "https://example.com/research/"
FEED = "https://example.com/news/rss.xml"
RESEARCH = "https://example.com/index/research-result/"
PRODUCT = "https://example.com/index/new-product/"


def _feed(*items: tuple[str, list[str]]) -> bytes:
    root = Element("rss", version="2.0")
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = "News"
    SubElement(channel, "link").text = "https://example.com/news/"
    SubElement(channel, "description").text = "All official updates"
    for number, (url, categories) in enumerate(items):
        item = SubElement(channel, "item")
        SubElement(item, "title").text = f"Article {number}"
        SubElement(item, "link").text = url
        SubElement(item, "description").text = "<p>Full feed summary.</p>"
        SubElement(item, "author").text = "Editor"
        SubElement(item, "pubDate").text = "Wed, 04 Nov 2026 12:00:00 GMT"
        for category in categories:
            SubElement(item, "category").text = category
    return tostring(root)


def _mixed_feed() -> bytes:
    return _feed((RESEARCH, ["Research", "Science"]), (PRODUCT, ["Product"]))


def test_parse_rss_preserves_categories_and_metadata():
    entries = rss.parse_feed(_mixed_feed())
    assert entries.homepage == "https://example.com/news/"
    assert entries[0].categories == ["Research", "Science"]
    assert entries[1].categories == ["Product"]
    assert entries[0].author == "Editor"
    assert entries[0].summary == "Full feed summary."
    assert entries[0].published_at == datetime(2026, 11, 4, 12, tzinfo=UTC)


def test_parse_atom_preserves_category_terms_not_labels():
    entries = rss.parse_feed(b'''<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>News</title><id>https://example.com/news/</id>
          <entry><id>one</id><title>Article</title>
            <link href="https://example.com/index/one/"/>
            <category term="Research" label="Human-readable title"/>
            <category term="Science"/>
          </entry>
        </feed>''')
    assert entries[0].categories == ["Research", "Science"]


@pytest.mark.parametrize("categories", [["research"], ["RESEARCH"], ["rEsEaRcH"]])
def test_fetch_actually_filters_case_insensitively(make_client, categories):
    client, router = make_client({FEED: httpx.Response(200, content=_mixed_feed())})
    source = SourceConfig(id="news", type="rss", url=FEED, rss_categories=categories)
    entries = rss.fetch(source, client, fast_settings())
    assert [entry.url for entry in entries] == [RESEARCH]
    assert entries[0].categories == ["Research", "Science"]
    assert entries.homepage == "https://example.com/news/"
    assert router.urls() == [FEED]


def test_fetch_without_filter_preserves_all_entries_and_homepage(make_client):
    client, _ = make_client({FEED: httpx.Response(200, content=_mixed_feed())})
    source = SourceConfig(id="news", type="rss", url=FEED)
    entries = rss.fetch(source, client, fast_settings())
    assert [entry.url for entry in entries] == [RESEARCH, PRODUCT]
    assert entries.homepage == "https://example.com/news/"


def test_fetch_multiple_categories_uses_union_and_keeps_uncategorized_only_without_filter(
    make_client,
):
    uncategorized = "https://example.com/index/uncategorized/"
    data = _feed((RESEARCH, ["Research"]), (PRODUCT, ["Product"]), (uncategorized, []))
    client, _ = make_client({FEED: httpx.Response(200, content=data)})
    source = SourceConfig(
        id="news", type="rss", url=FEED, rss_categories=["research", "PRODUCT"]
    )
    assert [entry.url for entry in rss.fetch(source, client, fast_settings())] == [
        RESEARCH, PRODUCT
    ]
    source.rss_categories = []
    assert len(rss.fetch(source, client, fast_settings())) == 3


def test_fetch_filter_with_no_matches_keeps_homepage(make_client):
    client, _ = make_client({FEED: httpx.Response(200, content=_mixed_feed())})
    source = SourceConfig(id="news", type="rss", url=FEED, rss_categories=["Unlisted"])
    entries = rss.fetch(source, client, fast_settings())
    assert entries == []
    assert entries.homepage == "https://example.com/news/"


def test_same_category_can_suggest_filter_for_articles_outside_section_path():
    result = feed_scope_candidate(HOME, [RESEARCH], FEED, _mixed_feed())
    assert result["relation"] == "category_filter"
    assert result["scope_categories"] == ["Research"]
    assert result["scope_confirmed"] is True
    assert [entry.url for entry in result["entries"]] == [RESEARCH.rstrip("/")]
    assert result["matched_urls"] == [RESEARCH.rstrip("/")]
    assert result["feed_total"] == 2
    assert result["outside_count"] == 1
    assert "完整覆盖" in result["note"]
    assert "用户确认" in result["note"]
    assert {candidate["relation"] for candidate in result["alternatives"]} == {
        "sample_overlap", "category_filter"
    }


def test_same_name_without_article_overlap_is_only_limited_category_evidence():
    result = feed_scope_candidate(HOME, ["https://example.com/index/older/"], FEED, _mixed_feed())
    assert result["relation"] == "category_filter"
    assert result["scope_confirmed"] is True
    assert result["matched_urls"] == []
    assert result["uncovered_page_urls"] == ["https://example.com/index/older"]
    assert "仅有同名分类证据" in result["note"]
    assert "暂无页面文章 URL 重合" in result["note"]
    assert "可能遗漏" in result["note"]
    assert "完整覆盖" in result["note"]
    assert result["alternatives"] == []


def test_other_category_on_research_page_blocks_scope_confirmation_and_offers_both_choices():
    result = feed_scope_candidate(HOME, [RESEARCH, PRODUCT], FEED, _mixed_feed())
    assert result["relation"] == "category_filter"
    assert result["scope_confirmed"] is False
    assert result["uncovered_page_urls"] == [PRODUCT.rstrip("/")]
    assert "其他分类" in result["note"]
    assert "不能视为网页等价" in result["note"]
    assert "遗漏" in result["note"]
    broad, narrow = result["alternatives"]
    assert broad["relation"] == "sample_overlap"
    assert broad["scope_categories"] == []
    assert broad["scope_confirmed"] is True
    assert len(broad["entries"]) == 2
    assert "混入其他栏目" in broad["note"]
    assert narrow["relation"] == "category_filter"
    assert narrow["scope_confirmed"] is False
    assert [entry.url for entry in narrow["entries"]] == [RESEARCH.rstrip("/")]
    assert "alternatives" not in broad
    assert "alternatives" not in narrow


def test_only_product_page_sample_does_not_confirm_research_category_identity():
    result = feed_scope_candidate(HOME, [PRODUCT], FEED, _mixed_feed())
    assert result["scope_confirmed"] is False
    assert result["matched_urls"] == [PRODUCT.rstrip("/")]
    assert "仅有同名分类证据" in result["note"]
    assert "其他分类" in result["note"]


def test_partial_overlap_without_category_only_suggests_broader_feed():
    data = _feed((RESEARCH, ["Science"]), (PRODUCT, ["Product"]))
    result = feed_scope_candidate(HOME, [RESEARCH, "/index/older/"], FEED, data)
    assert result["relation"] == "sample_overlap"
    assert result["scope_confirmed"] is True
    assert result["scope_categories"] == []
    assert result["outside_count"] == 1
    assert len(result["entries"]) == 2
    assert result["uncovered_page_urls"] == ["https://example.com/index/older"]
    assert "更广" in result["note"]
    assert "混入" in result["note"]
    assert "不证明整站、历史或完整覆盖" in result["note"]


def test_same_domain_title_and_similar_slug_are_not_identity_evidence():
    result = feed_scope_candidate(
        HOME, ["https://example.com/research/research-result/"], FEED,
        _feed((RESEARCH, ["Science"])),
    )
    assert result["relation"] == "unverified"
    assert result["scope_confirmed"] is False
    assert result["entries"] == []
    assert result["matched_urls"] == []
    assert result["alternatives"] == []


def test_matching_is_not_site_specific_and_ignores_index_suffix():
    result = feed_scope_candidate(
        "https://elsewhere.example/SCIENCE/index.html", [],
        "https://elsewhere.example/feed.xml", _feed((RESEARCH, ["Science"])),
    )
    assert result["relation"] == "category_filter"
    assert result["scope_categories"] == ["Science"]


def test_url_normalization_preserves_query_and_path_case_identity():
    url = "https://example.com/index/Paper?edition=research&ref_src=section"
    page = "https://EXAMPLE.com:443/index/Paper/?edition=research&ref_src=section#details"
    data = _feed((url, []))
    result = feed_scope_candidate(HOME, [page], FEED, data)
    assert result["relation"] == "sample_overlap"
    assert result["matched_urls"] == [url]
    for other in (
        "https://example.com/index/Paper?edition=product&ref_src=section",
        "https://example.com/index/Paper?edition=research&ref_src=other",
        "https://example.com/index/paper?edition=research&ref_src=section",
    ):
        assert feed_scope_candidate(HOME, [other], FEED, data)["relation"] == "unverified"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,malicious",
    "file:///etc/passwd",
    "http://127.0.0.1/article",
    "http://127.1/article",
    "http://0x7f.0.0.1/article",
    "http://2130706433/article",
    "http://[::1]/article",
    "http://169.254.169.254/latest/meta-data",
    "https://localhost/article",
    "https://private.internal/article",
    "https://private.local/article",
    "https://user:password@example.com/article",
    "https://example.com\\@127.0.0.1/article",
    "https://example.com/%0d%0aLocation:evil",
    "https://example.com:invalid/article",
])
def test_unsafe_urls_never_become_evidence_or_suggestions(url):
    result = feed_scope_candidate(HOME, [url], FEED, _feed((url, ["Research"])))
    assert result["relation"] == "unverified"
    assert result["scope_confirmed"] is False
    assert result["entries"] == []
    assert result["matched_urls"] == []
    assert result["page_sample"] == []


@pytest.mark.parametrize("key", ["homepage", "feed_url"])
def test_unsafe_context_urls_never_produce_candidates(key):
    kwargs = {"homepage": HOME, "page_links": [RESEARCH], "feed_url": FEED, "data": _mixed_feed()}
    kwargs[key] = "http://127.0.0.1/"
    result = feed_scope_candidate(**kwargs)
    assert result["relation"] == "unverified"
    assert result["scope_confirmed"] is False
    assert result["entries"] == []


def test_empty_feed_is_safe_and_preserves_feed_homepage():
    data = _feed()
    entries = rss.parse_feed(data)
    assert entries == []
    assert entries.homepage == "https://example.com/news/"
    result = feed_scope_candidate(HOME, [], FEED, data)
    assert result["relation"] == "unverified"
    assert result["scope_confirmed"] is False
    assert result["feed_total"] == result["outside_count"] == 0
    assert "空 RSS/Atom" in result["note"]


@pytest.mark.parametrize("data", [
    b"<html><body>not a feed</body></html>",
    b'<html><body><rss version="2.0"><channel><title>Fake</title>'
    b'<item><title>Fake article</title><link>https://example.com/index/one</link>'
    b'</item></channel></rss></body></html>',
    b'<html><head><title>Research</title></head><body><article>'
    b'<a href="https://example.com/index/one">Paper</a></article></body></html>',
    b"<rss></rss><html>not a feed</html>",
    b"not xml at all",
    b"",
])
def test_html_and_non_feeds_are_rejected_before_filtering(make_client, data):
    with pytest.raises(FetchError, match="不是有效的 RSS/Atom"):
        rss.parse_feed(data)
    with pytest.raises(FetchError, match="不是有效的 RSS/Atom"):
        feed_scope_candidate(HOME, [RESEARCH], FEED, data)
    client, _ = make_client({FEED: httpx.Response(200, content=data)})
    source = SourceConfig(id="news", type="rss", url=FEED, rss_categories=["Research"])
    with pytest.raises(FetchError, match="不是有效的 RSS/Atom"):
        rss.fetch(source, client, fast_settings())


def test_candidate_results_and_samples_are_bounded_and_deduplicated():
    urls = [f"https://example.com/index/paper-{number}/" for number in range(75)]
    data = _feed(*[(url, ["Research"]) for url in urls])
    result = feed_scope_candidate(HOME, urls + urls, FEED, data)
    assert result["feed_total"] == 75
    assert len(result["entries"]) == 50
    assert len(result["matched_urls"]) == len(result["page_sample"]) == 10
    assert result["outside_count"] == 0
    for alternative in result["alternatives"]:
        assert len(alternative["entries"]) <= 50
        assert len(alternative["matched_urls"]) <= 10
        assert len(alternative["page_sample"]) <= 10
        assert len(alternative["uncovered_page_urls"]) <= 10
