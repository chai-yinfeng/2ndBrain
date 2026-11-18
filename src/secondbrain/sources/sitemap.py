"""sitemap.xml 信源：给没有 RSS 的站点用（ADR 0012）。

sitemap 只提供 URL（和可选的 <lastmod>），没有标题和发布时间。发布时间的优先级：
URL 中的日期（YYYY-MM-DD 或 YYYY/MM/DD）→ 正文页提取的日期 → <lastmod>（仅 `use_lastmod: true`）。
选取"最新 N 条"发生在抓正文页之前，所以只能用 URL 日期或 lastmod 排序；
抓正文页时（`ingest.extract_content`）再用页面元数据补全标题 / 作者，并用页面日期覆盖 lastmod。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from secondbrain.extract.html import extract_page_meta
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import clean_title, parse_iso_datetime, plausible

_URL_DATE_RE = re.compile(r"(?<!\d)(\d{4})([-/])(\d{2})\2(\d{2})(?!\d)")
MAX_SITEMAP_DEPTH = 2
MAX_SITEMAP_REQUESTS = 6
MAX_SITEMAP_ENTRIES = 500


class SitemapReader(Protocol):
    warnings: list[str]

    def get(self, url: str, *, origin: tuple[str, str] | None = None) -> httpx.Response: ...


def _local(tag: str) -> str:
    """`{http://www.sitemaps.org/schemas/sitemap/0.9}url` → `url`。"""
    return tag.rsplit("}", 1)[-1]


def date_in_url(url: str) -> datetime | None:
    """URL 路径中的 YYYY-MM-DD / YYYY/MM/DD（取第一个合法日期）；没有或不合理时返回 None。"""
    for m in _URL_DATE_RE.finditer(urlsplit(url).path):
        try:
            dt = datetime(int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=UTC)
        except ValueError:
            continue
        if plausible(dt):
            return dt
    return None


def _slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or url


def parse_sitemap(
    data: bytes, url_pattern: str | None = None, use_lastmod: bool = False
) -> list[RawEntry]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FetchError(f"不是有效的 sitemap XML：{exc}") from exc
    if _local(root.tag) == "sitemapindex":
        raise FetchError("sitemap index 需要通过 fetch 展开，请勿作为单个 urlset 解析")
    if _local(root.tag) != "urlset":
        raise FetchError(f"不是有效的 sitemap：根元素为 <{_local(root.tag)}>")
    pattern = re.compile(url_pattern) if url_pattern else None
    entries: list[RawEntry] = []
    for node in root:
        if _local(node.tag) != "url":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in node}
        loc = fields.get("loc")
        if not loc or (pattern and not pattern.search(loc)):
            continue
        published = date_in_url(loc)
        if published is None and use_lastmod:
            published = parse_iso_datetime(fields.get("lastmod"))
        entries.append(RawEntry(url=loc, title=_slug(loc), published_at=published, page_meta=True))
    return entries


def collect_sitemap(
    reader: SitemapReader,
    url: str,
    url_pattern: str | None = None,
    use_lastmod: bool = False,
    *,
    max_entries: int = MAX_SITEMAP_ENTRIES,
    origin: tuple[str, str] | None = None,
    initial_response: httpx.Response | None = None,
) -> list[RawEntry]:
    """有界展开同源 index；发现与真实采集共用，跳转/公网/体积限制由 reader 保证。"""
    from secondbrain.source_discovery import _origin, _public_url

    url = _public_url(url)
    origin = origin or _origin(url)
    pending = deque([(url, 0)])
    visited: set[str] = set()
    seen_entries: set[str] = set()
    entries: list[RawEntry] = []
    requests = 0
    while pending:
        current, depth = pending.popleft()
        current = _public_url(current)
        if current in visited:
            reader.warnings.append("sitemap index 含重复或循环引用，已跳过。")
            continue
        if requests >= MAX_SITEMAP_REQUESTS:
            reader.warnings.append(f"sitemap 最多读取 {MAX_SITEMAP_REQUESTS} 个文档。")
            break
        visited.add(current)
        response = (
            initial_response
            if initial_response is not None and current == url
            else reader.get(current, origin=origin)
        )
        visited.add(str(response.url))
        requests += 1
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type or re.match(
            br"\s*(?:<!doctype\s+html|<html\b)", response.content[:1024], re.I
        ):
            raise reader.error(
                str(response.url), "候选 sitemap 返回 HTML 页面，不是 XML，未继续解析", "format"
            )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise reader.error(
                str(response.url), f"不是有效的 sitemap XML：{exc}", "format"
            ) from exc
        if _local(root.tag) == "sitemapindex":
            if depth >= MAX_SITEMAP_DEPTH:
                reader.warnings.append(f"sitemap index 超过 {MAX_SITEMAP_DEPTH} 层，未继续展开。")
                continue
            for node in root:
                if _local(node.tag) != "sitemap":
                    continue
                loc = next(
                    ((child.text or "").strip() for child in node if _local(child.tag) == "loc"),
                    "",
                )
                if not loc:
                    continue
                target = _public_url(urljoin(str(response.url), loc))
                if _origin(target) != origin:
                    raise FetchError("sitemap index 引用了其他站点，未自动扩大抓取范围")
                if len(pending) >= MAX_SITEMAP_REQUESTS:
                    reader.warnings.append("sitemap index 子文档过多，仅检查有界候选。")
                    break
                pending.append((target, depth + 1))
            continue
        for entry in parse_sitemap(response.content, url_pattern, use_lastmod):
            try:
                entry.url = _public_url(urljoin(str(response.url), entry.url))
            except FetchError:
                continue
            if _origin(entry.url) != origin:
                continue
            if entry.url not in seen_entries:
                seen_entries.add(entry.url)
                entries.append(entry)
            if len(entries) >= max_entries:
                reader.warnings.append(f"sitemap 仅检查前 {max_entries} 个候选。")
                return entries
    return entries


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    from secondbrain.source_discovery import _Reader

    assert source.url
    reader = client if isinstance(client, _Reader) else _Reader(client, settings)
    return collect_sitemap(reader, source.url, source.url_pattern, source.use_lastmod)


def apply_page_meta(entry: RawEntry, html: str, url: str | None = None) -> None:
    """用正文页的元数据补全 sitemap 条目（原地修改）：标题、作者；URL 中没有日期时用页面日期。"""
    meta = extract_page_meta(html, url)
    if meta.title:
        entry.title = clean_title(meta.title)
    entry.author = entry.author or meta.author
    if date_in_url(entry.url) is None and not entry.keep_date:
        page_date = parse_iso_datetime(meta.date)
        if page_date:
            entry.published_at = page_date
