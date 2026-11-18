"""Qwen Research 的公开 JSON 数据；不执行 SPA JavaScript。

research 页面（qwen-ai-fe/0.0.91/js/p_research-index.js）将 page_config 中
9496.js 的 Uv 旧文章白名单与 type=qwen_ai 的新文章合并。Research 是页面范围，
包含 Release / Open-Source 等标签，不等于 tags=Research，也不等于旧博客 RSS。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Protocol

import httpx

from secondbrain.extract.html import html_fragment_to_text
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.base import clean_title, parse_iso_datetime

HOMEPAGE = "https://qwen.ai/research"
ARTICLES_URL = "https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=en-US"
LEGACY_URL = "https://qwen.ai/api/page_config?code=research.research-list&language=en"
MAX_RESEARCH_ENTRIES = 200
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_UNKNOWN_DATE = datetime.min.replace(tzinfo=UTC)

# 公开前端 0.0.91 的 Uv：page_config 也含已迁移记录，不能将整张配置表当作展示列表。
LEGACY_RESEARCH_IDS = frozenset(
    "agent chinese-clip codeqwen1.5 global-load-balance gspo ofa ofasys qvq-72b-preview "
    "qvq-max-preview qwen qwen-agent-2405 qwen-image qwen-image-edit qwen-image-edit-2509 "
    "qwen-max-0428 qwen-moe qwen-mt qwen-tts qwen-vl qwen-vlo qwen1.5 qwen1.5-110b "
    "qwen1.5-32b qwen2 qwen2-audio qwen2-math qwen2-vl qwen2.5 qwen2.5-1m qwen2.5-coder "
    "qwen2.5-coder-family qwen2.5-llm qwen2.5-math qwen2.5-math-prm qwen2.5-max "
    "qwen2.5-omni qwen2.5-turbo qwen2.5-vl qwen2.5-vl-32b qwen3 qwen3-asr-flash "
    "qwen3-coder qwen3-embedding qwen3-livetranslate qwen3-max qwen3-next qwen3-omni "
    "qwen3-tts qwen3-vl qwen3guard qwq-32b qwq-32b-preview qwq-max-preview".split()
)


class ResearchReader(Protocol):
    def get(self, url: str, *, origin: tuple[str, str] | None = None) -> httpx.Response: ...


def _json(data: bytes) -> object:
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise FetchError(
            "Qwen Research 响应不是有效 JSON（可能返回 SPA 页面或接口已变化）"
        ) from exc


def _text(record: dict, name: str, *, required: bool = False) -> str:
    value = record.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise FetchError(f"Qwen Research 字段 {name} 缺失或不是有效文本")
    return value


def _records(value: object) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise FetchError("Qwen Research 文章列表格式不符：应为对象列表")
    if len(value) > MAX_RESEARCH_ENTRIES:
        raise FetchError(f"Qwen Research 超过条目上限（{MAX_RESEARCH_ENTRIES}）")
    return value


def _entry(record: dict, *, legacy: bool) -> RawEntry:
    slug = _text(record, "id" if legacy else "path", required=True)
    if not _SLUG.fullmatch(slug) or slug in {".", ".."}:
        raise FetchError("Qwen Research 文章路径不是安全的单段标识")
    metadata = record if legacy else record.get("extra")
    if not isinstance(metadata, dict):
        raise FetchError("Qwen Research 缺少文章 extra 元数据")
    summary = _text(metadata, "description") or _text(metadata, "introduction")
    return RawEntry(
        url=f"https://qwen.ai/blog?id={slug}",
        title=clean_title(_text(record, "title", required=True)),
        author=clean_title(_text(metadata, "author")) or None,
        summary=html_fragment_to_text(summary) or None,
        feed_content=(_text(record, "content") or None) if not legacy else None,
        published_at=parse_iso_datetime(_text(metadata, "date")),
        keep_date=True,
    )


def parse_articles(data: bytes) -> list[RawEntry]:
    """只接收 Research 页面使用的 qwen_ai / en-US 分组；不根据标签缩小范围。"""
    payload = _json(data)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise FetchError("Qwen Research 接口未返回 success=true")
    container = payload.get("data")
    if not isinstance(container, dict) or "articles" not in container:
        raise FetchError("Qwen Research 接口缺少 data.articles")
    records = _records(container["articles"])
    entries: dict[str, RawEntry] = {}
    for record in records:
        group = _text(record, "type", required=True)
        language = _text(record, "language", required=True)
        if group != "qwen_ai" or language != "en-US":
            continue
        entry = _entry(record, legacy=False)
        entries.setdefault(entry.url, entry)
    if records and not entries:
        raise FetchError("Qwen Research 接口未返回 qwen_ai / en-US 分组")
    return list(entries.values())


def parse_legacy(data: bytes) -> list[RawEntry]:
    """旧配置只采用前端明确展示的 ID，不将配置里的迁移副本重复导入。"""
    entries: dict[str, RawEntry] = {}
    records = _records(_json(data))
    for record in records:
        slug = _text(record, "id", required=True)
        if slug not in LEGACY_RESEARCH_IDS:
            continue
        entry = _entry(record, legacy=True)
        entries.setdefault(entry.url, entry)
    if records and not entries:
        raise FetchError("Qwen Research 配置未返回已验证的旧文章分组")
    return list(entries.values())


def collect_research(reader: ResearchReader, url: str = ARTICLES_URL) -> list[RawEntry]:
    """两次固定公开 GET；传入发现 reader 时共享请求、跳转与下载预算。"""
    if url != ARTICLES_URL:
        raise FetchError("Qwen Research 只允许已验证的官方公开 GET 接口")
    entries: dict[str, RawEntry] = {}
    for endpoint, parser in ((ARTICLES_URL, parse_articles), (LEGACY_URL, parse_legacy)):
        response = reader.get(endpoint, origin=("https", "qwen.ai"))
        if str(response.url) != endpoint:
            raise FetchError("Qwen Research 接口发生路径或参数跳转，未自动采用")
        for entry in parser(response.content):
            entries.setdefault(entry.url, entry)
        if len(entries) > MAX_RESEARCH_ENTRIES:
            raise FetchError(f"Qwen Research 超过合并条目上限（{MAX_RESEARCH_ENTRIES}）")
    # 预览可能只保留前 50 条；先按可靠日期排序，未知日期保持原始稳定顺序。
    return sorted(
        entries.values(), key=lambda entry: entry.published_at or _UNKNOWN_DATE, reverse=True
    )


def fetch(source: SourceConfig, client: HttpClient, settings: FetchSettings) -> list[RawEntry]:
    from secondbrain.source_discovery import _Reader

    assert source.url
    reader = client if isinstance(client, _Reader) else _Reader(client, settings)
    return collect_research(reader, source.url)


def resolve_article(
    url: str, client: HttpClient, settings: FetchSettings
) -> RawEntry | None:
    """精确文章 URL 从同一公开 Research 数据解析；不请求网页或任意文章接口。

    非标准 URL 不联网并返回 None；未知 ID、接口/预算异常抛明确 FetchError。
    旧记录仍只有元数据和摘要，绝不将摘要填入 feed_content 冒充正文。
    """
    prefix = "https://qwen.ai/blog?id="
    if not url.startswith(prefix):
        return None
    slug = url[len(prefix):]
    if not _SLUG.fullmatch(slug):
        return None

    from secondbrain.source_discovery import _Reader

    reader = client if isinstance(client, _Reader) else _Reader(client, settings)
    for entry in collect_research(reader):
        if entry.url == url:
            return entry
    raise FetchError(f"Qwen Research 公开接口未找到文章 {slug}，未采用 SPA 页面内容")
