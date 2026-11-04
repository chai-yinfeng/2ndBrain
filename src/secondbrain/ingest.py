"""采集编排：逐信源抓取 → 时间窗过滤 → URL 去重 → 正文提取 → 入库。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pypdf.errors import PyPdfError

from secondbrain import db
from secondbrain.extract.html import extract_main_text, html_fragment_to_text
from secondbrain.extract.pdf import extract_pdf_text
from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources import FETCHERS
from secondbrain.urls import canonicalize_url, content_hash

# arXiv HTML 版提取到的正文短于该值时视为无效，改用 PDF。
MIN_ARXIV_HTML_CHARS = 1000

# 提取方式中表示"没拿到正文"的取值。
FAILED_METHODS = {"summary", "none"}


@dataclass
class SourceResult:
    source_id: str
    status: Literal["ok", "failed", "skipped"]
    added: int = 0
    duplicates: int = 0
    extract_failed: int = 0
    message: str = ""


def skip_reason(source: SourceConfig) -> str | None:
    if not source.enabled:
        return "未启用"
    if source.type == "youtube":
        return "youtube 待 M5 支持"
    if source.needs_scraper:
        return "needs_scraper，待支持"
    return None


def _pdf_text(client: HttpClient, url: str, fs: FetchSettings) -> str:
    resp = client.get(url, max_bytes=fs.pdf_max_bytes)
    return extract_pdf_text(resp.content, fs.pdf_max_pages)


def extract_content(entry: RawEntry, client: HttpClient, fs: FetchSettings) -> tuple[str, str]:
    """返回 (正文, 提取方式)。拿不到正文时退回摘要。"""
    if entry.arxiv_id:
        try:
            resp = client.get(f"https://arxiv.org/html/{entry.arxiv_id}")
            text = extract_main_text(resp.text, url=str(resp.url))
            if text and len(text) >= MIN_ARXIV_HTML_CHARS:
                return text, "arxiv_html"
        except FetchError:
            pass
        try:
            text = _pdf_text(client, f"https://arxiv.org/pdf/{entry.arxiv_id}", fs)
            if text:
                return text, "pdf"
        except (FetchError, PyPdfError):
            pass
    else:
        feed_text = html_fragment_to_text(entry.feed_content) if entry.feed_content else ""
        if len(feed_text) >= fs.min_feed_content_chars:
            return feed_text, "feed"
        try:
            resp = client.get(entry.url, max_bytes=fs.pdf_max_bytes)
            if "pdf" in resp.headers.get("content-type", ""):
                text = extract_pdf_text(resp.content, fs.pdf_max_pages)
                method = "pdf"
            else:
                text = extract_main_text(resp.text, url=str(resp.url)) or ""
                method = "html"
            if text:
                return text, method
        except (FetchError, PyPdfError):
            pass
        if feed_text:
            return feed_text, "feed"
    if entry.summary:
        return entry.summary, "summary"
    return "", "none"


def collect(
    source: SourceConfig,
    client: HttpClient,
    fs: FetchSettings,
    known: frozenset[str],
    limit: int | None,
    now: datetime,
) -> tuple[list[db.NewItem], int]:
    """抓取单个信源，返回 (待入库的新条目, 已知重复数)。只做网络与解析，不写库。"""
    cutoff = now - timedelta(days=source.max_age_days)
    entries = [
        e
        for e in FETCHERS[source.type](source, client, fs)
        if not (e.published_at and e.published_at < cutoff)
    ]
    # 只看最新的 limit 条：新增数 ≤ limit，且重复运行时不会逐次补抓更旧的条目。
    entries.sort(key=lambda e: e.published_at or now, reverse=True)
    if limit is not None:
        entries = entries[:limit]
    items: list[db.NewItem] = []
    seen: set[str] = set()
    duplicates = 0
    for entry in entries:
        canonical = canonicalize_url(entry.url)
        if canonical in known or canonical in seen:
            duplicates += 1
            continue
        seen.add(canonical)
        text, method = extract_content(entry, client, fs)
        text = text[: fs.content_max_chars]
        items.append(
            db.NewItem(
                source_id=source.id,
                url=entry.url,
                url_canonical=canonical,
                content_hash=content_hash(entry.title, text),
                title=entry.title,
                author=entry.author,
                summary=entry.summary,
                content=text,
                extract_method=method,
                published_at=entry.published_at,
            )
        )
    return items, duplicates


def fetch_sources(
    conn: sqlite3.Connection,
    sources: list[SourceConfig],
    client: HttpClient,
    fs: FetchSettings,
    limit: int | None = None,
    now: datetime | None = None,
    on_result: Callable[[SourceResult], None] | None = None,
) -> list[SourceResult]:
    """抓取所有信源。单个信源失败只记录在其结果中，不影响其他信源。"""
    now = now or datetime.now(UTC)
    db.upsert_sources(conn, sources)
    known = db.known_urls(conn)
    results: list[SourceResult] = []
    with ThreadPoolExecutor(max_workers=fs.concurrency) as pool:
        futures = {
            s.id: pool.submit(collect, s, client, fs, known, limit, now)
            for s in sources
            if skip_reason(s) is None
        }
        for source in sources:
            reason = skip_reason(source)
            if reason:
                result = SourceResult(source.id, "skipped", message=reason)
            else:
                try:
                    items, duplicates = futures[source.id].result()
                except FetchError as exc:
                    result = SourceResult(source.id, "failed", message=str(exc))
                except Exception as exc:  # 单个信源的任何异常都不应中断整体
                    result = SourceResult(
                        source.id, "failed", message=f"{type(exc).__name__}: {exc}"
                    )
                else:
                    result = SourceResult(source.id, "ok", duplicates=duplicates)
                    for item in items:
                        if db.insert_item(conn, item, now):
                            result.added += 1
                            if item.extract_method in FAILED_METHODS:
                                result.extract_failed += 1
                        else:
                            result.duplicates += 1
                    conn.commit()
            results.append(result)
            if on_result:
                on_result(result)
    return results
