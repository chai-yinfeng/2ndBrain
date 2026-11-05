"""RSS / Atom 信源（feedparser）。"""

from __future__ import annotations

import feedparser

from secondbrain.extract.html import html_fragment_to_text
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import Fetched, clean_title, struct_time_to_datetime


def parse_feed(data: bytes) -> list[RawEntry]:
    feed = feedparser.parse(data)
    if not feed.entries:
        if feed.bozo:
            raise FetchError(f"不是有效的 RSS/Atom：{feed.get('bozo_exception')}")
        return []
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
            )
        )
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url
    resp = client.get(source.url)
    return parse_feed(resp.content)
