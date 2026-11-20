"""RSS / Atom 信源（feedparser）。"""

from __future__ import annotations

from io import BytesIO
from xml.etree.ElementTree import ParseError, iterparse

import feedparser

from secondbrain.extract.html import html_fragment_to_text
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import Fetched, clean_title, struct_time_to_datetime


def parse_feed(data: bytes) -> list[RawEntry]:
    # 只验证文档根元素，不对全文强制 XML 严格解析，以保留 feedparser 的容错行为。
    try:
        root = next(iterparse(BytesIO(data), events=("start",)))[1].tag
    except (ParseError, StopIteration) as exc:
        raise FetchError("不是有效的 RSS/Atom：缺少 feed 根元素") from exc
    if root not in {
        "rss",
        "feed",
        "{http://www.w3.org/2005/Atom}feed",
        "{http://purl.org/atom/ns#}feed",
        "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF",
    }:
        raise FetchError("不是有效的 RSS/Atom：文档根元素不是 feed")
    feed = feedparser.parse(data)
    # feedparser 也会从普通 HTML 提取 entries；不能将它们当作订阅条目。
    if not feed.version:
        raise FetchError(f"不是有效的 RSS/Atom：{feed.get('bozo_exception') or '缺少 feed 标识'}")
    if not feed.entries and feed.bozo:
        raise FetchError(f"不是有效的 RSS/Atom：{feed.get('bozo_exception')}")
    entries = Fetched()
    entries.homepage = feed.feed.get("link") or None
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        contents = [c.get("value", "") for c in e.get("content", [])]
        feed_content = max(contents, key=len) if contents else None
        summary = e.get("summary")
        entries.append(
            RawEntry(
                url=link,
                title=clean_title(e.get("title")) or link,
                author=e.get("author"),
                summary=html_fragment_to_text(summary) if summary else None,
                feed_content=feed_content or summary,
                published_at=struct_time_to_datetime(
                    e.get("published_parsed") or e.get("updated_parsed")
                ),
                categories=list(
                    dict.fromkeys(
                        tag["term"].strip()
                        for tag in e.get("tags", [])
                        if isinstance(tag.get("term"), str) and tag["term"].strip()
                    )
                ),
            )
        )
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url
    resp = client.get(source.url)
    entries = parse_feed(resp.content)
    if source.rss_categories:
        wanted = {category.strip().casefold() for category in source.rss_categories}
        # 原地筛选以保留 Fetched.homepage，而非退化为不带元数据的普通 list。
        entries[:] = [
            entry
            for entry in entries
            if any(category.casefold() in wanted for category in entry.categories)
        ]
    return entries
