"""arXiv 官方 API（Atom）。"""

from __future__ import annotations

import re

import feedparser

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import clean_title, struct_time_to_datetime

API_URL = "https://export.arxiv.org/api/query"
_ID_RE = re.compile(r"arxiv\.org/abs/(.+?)(v\d+)?$")


def abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def parse_arxiv_id(entry_id: str) -> str | None:
    """`http://arxiv.org/abs/2411.01234v2` → `2411.01234`（去掉版本号）。"""
    m = _ID_RE.search(entry_id.strip())
    return m.group(1) if m else None


def parse_api_response(data: bytes) -> list[RawEntry]:
    feed = feedparser.parse(data)
    if feed.bozo and not feed.entries:
        raise FetchError(f"arXiv API 响应无法解析：{feed.get('bozo_exception')}")
    entries: list[RawEntry] = []
    for e in feed.entries:
        entry_id = e.get("id", "")
        if "api/errors" in entry_id:
            raise FetchError(f"arXiv API 错误：{clean_title(e.get('summary'))}")
        arxiv_id = parse_arxiv_id(entry_id)
        if not arxiv_id:
            continue
        authors = ", ".join(a.get("name", "") for a in e.get("authors", []) if a.get("name"))
        entries.append(
            RawEntry(
                url=abs_url(arxiv_id),
                title=clean_title(e.get("title")),
                author=authors or None,
                summary=clean_title(e.get("summary")) or None,
                published_at=struct_time_to_datetime(e.get("published_parsed")),
                arxiv_id=arxiv_id,
            )
        )
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    params = {
        "search_query": source.query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": "0",
        "max_results": str(settings.arxiv_max_results),
    }
    resp = client.get(API_URL, params=params)
    return parse_api_response(resp.content)
