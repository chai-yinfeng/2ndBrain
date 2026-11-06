"""各类型信源的抓取与解析。"""

from __future__ import annotations

from collections.abc import Callable

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.sources import arxiv, hf_papers, html_index, rss, sitemap, youtube

Fetcher = Callable[[SourceConfig, HttpClient, FetchSettings], list[RawEntry]]

FETCHERS: dict[str, Fetcher] = {
    "rss": rss.fetch,
    "arxiv": arxiv.fetch,
    "hf_papers": hf_papers.fetch,
    "youtube": youtube.fetch,
    "sitemap": sitemap.fetch,
    "html_index": html_index.fetch,
}
