"""sitemap.xml 信源：给没有 RSS 的站点用（ADR 0012）。

sitemap 只提供 URL（和可选的 <lastmod>），没有标题和发布时间。发布时间的优先级：
URL 中的日期（YYYY-MM-DD 或 YYYY/MM/DD）→ 正文页提取的日期 → <lastmod>（仅 `use_lastmod: true`）。
选取"最新 N 条"发生在抓正文页之前，所以只能用 URL 日期或 lastmod 排序；
抓正文页时（`ingest.extract_content`）再用页面元数据补全标题 / 作者，并用页面日期覆盖 lastmod。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urlsplit

from secondbrain.extract.html import extract_page_meta
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import clean_title, parse_iso_datetime, plausible

_URL_DATE_RE = re.compile(r"(?<!\d)(\d{4})([-/])(\d{2})\2(\d{2})(?!\d)")


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
        raise FetchError("暂不支持 sitemap index，请配置具体的 urlset sitemap 地址")
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


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    assert source.url
    resp = client.get(source.url)
    return parse_sitemap(resp.content, source.url_pattern, source.use_lastmod)


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
