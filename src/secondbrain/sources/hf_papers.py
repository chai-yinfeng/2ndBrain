"""Hugging Face Daily Papers（公开 JSON API）。条目 URL 统一为 arXiv abs 页，便于跨信源去重。"""

from __future__ import annotations

import json

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.arxiv import abs_url
from secondbrain.sources.base import clean_title, parse_iso_datetime


def parse_daily_papers(data: bytes) -> list[RawEntry]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise FetchError(f"HF Daily Papers 响应不是 JSON：{exc}") from exc
    if not isinstance(payload, list):
        raise FetchError("HF Daily Papers 响应格式不符：顶层应为列表")
    entries: list[RawEntry] = []
    for record in payload:
        paper = record.get("paper") or record
        arxiv_id = paper.get("id")
        if not arxiv_id:
            continue
        authors = ", ".join(a.get("name", "") for a in paper.get("authors", []) if a.get("name"))
        entries.append(
            RawEntry(
                url=abs_url(arxiv_id),
                title=clean_title(paper.get("title") or record.get("title")),
                author=authors or None,
                summary=clean_title(paper.get("summary") or record.get("summary")) or None,
                # 用进入 Daily Papers 的时间，而不是 arXiv 首发时间
                published_at=parse_iso_datetime(
                    record.get("publishedAt") or paper.get("publishedAt")
                ),
                arxiv_id=arxiv_id,
            )
        )
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url
    resp = client.get(source.url)
    return parse_daily_papers(resp.content)
