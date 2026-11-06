"""html_index 信源：没有 RSS / 可用 sitemap 的博客，直接解析列表页（ADR 0020）。

列表页中所有 <a href> 转成绝对 URL，按 url_pattern 过滤、去重并保持页面顺序；可选 max_links 截断。
发布时间：抓正文页时从页面 meta 读取（与 sitemap 条目相同，page_meta=True）；读不到时用
列表页上该链接自带的日期（<a> 属性中的 YYYY-MM-DD，或 <a> 内 <time datetime>）；都没有时
为未知（入库后按抓取时间参与时间窗与排序，详情页显示"发布 未知"）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import plausible

_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def _date(text: str | None) -> datetime | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    try:
        return plausible(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC))
    except ValueError:
        return None


class _Links(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[tuple[str, datetime | None]] = []
        self._current: list | None = None  # [url, date]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                url = urldefrag(urljoin(self.base, href.strip())).url
                date = next((d for _, v in attrs if (d := _date(v))), None)
                self._current = [url, date]
        elif tag == "time" and self._current is not None and self._current[1] is None:
            self._current[1] = _date(dict(attrs).get("datetime"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self.links.append((self._current[0], self._current[1]))
            self._current = None


def _slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or url


def parse_index(html: str, base_url: str, url_pattern: str, max_links: int | None = None):
    """返回匹配 url_pattern 的文章链接 [RawEntry]（去重、保持页面顺序）。"""
    parser = _Links(base_url)
    parser.feed(html)
    pattern = re.compile(url_pattern)
    seen: set[str] = set()
    entries: list[RawEntry] = []
    for url, date in parser.links:
        if url in seen or not pattern.search(url):
            continue
        seen.add(url)
        entries.append(RawEntry(url=url, title=_slug(url), published_at=date, page_meta=True))
        if max_links and len(entries) >= max_links:
            break
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url and source.url_pattern
    resp = client.get(source.url)
    entries = parse_index(resp.text, str(resp.url), source.url_pattern, source.max_links)
    if not entries:
        raise FetchError(f"列表页中没有匹配 url_pattern 的链接：{source.url}")
    return entries
