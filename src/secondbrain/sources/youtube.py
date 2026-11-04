"""YouTube 频道 RSS（https://www.youtube.com/feeds/videos.xml?channel_id=...）。"""

from __future__ import annotations

import feedparser

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import clean_title, struct_time_to_datetime


def is_short(url: str) -> bool:
    return "/shorts/" in url


def parse_channel_feed(data: bytes) -> list[RawEntry]:
    feed = feedparser.parse(data)
    if not feed.entries and feed.bozo:
        raise FetchError(f"不是有效的 YouTube 频道 feed：{feed.get('bozo_exception')}")
    entries = []
    for e in feed.entries:
        video_id = e.get("yt_videoid")
        if not video_id:
            continue
        entries.append(
            RawEntry(
                url=e.get("link") or f"https://www.youtube.com/watch?v={video_id}",
                title=clean_title(e.get("title")),
                author=e.get("author"),
                summary=(e.get("summary") or "").strip() or None,  # media:description
                published_at=struct_time_to_datetime(e.get("published_parsed")),
                video_id=video_id,
            )
        )
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url
    return parse_channel_feed(client.get(source.url).content)
