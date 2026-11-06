"""Crossref REST API：按 DOI 查论文元数据，作为 Semantic Scholar 的兜底（ADR 0020）。

不加入 polite pool（需要提供邮箱，我们不提供），只带通用 UA。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

from secondbrain.net import FetchError, HttpClient

API = "https://api.crossref.org/works/{doi}"
_TAG = re.compile(r"<[^>]+>")


def jats_to_text(abstract: str | None) -> str:
    """Crossref 的摘要是 JATS XML：去掉标签与开头的 "Abstract" 标题，合并空白。"""
    if not abstract:
        return ""
    text = " ".join(unescape(_TAG.sub(" ", abstract)).split())
    return re.sub(r"^abstract\s*[:.]?\s*", "", text, flags=re.I)


def lookup_doi(client: HttpClient, doi: str) -> dict:
    """返回 {title, authors, published, abstract, pdf_urls}；失败时抛 FetchError（原因可读）。"""
    try:
        resp = client.get(API.format(doi=doi))
    except FetchError as exc:
        if "404" in str(exc):
            raise FetchError(f"Crossref 没有收录 DOI {doi}") from exc
        raise FetchError(f"Crossref 查询失败：{exc}") from exc
    try:
        msg = resp.json().get("message") or {}
    except ValueError as exc:
        raise FetchError("Crossref 返回的不是 JSON") from exc
    title = " ".join((msg.get("title") or [""])[0].split())
    if not title:
        raise FetchError("Crossref 返回的记录没有标题")
    authors = []
    for a in msg.get("author") or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p) or a.get("name")
        if name:
            authors.append(name)
    published = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            year, month, day = (list(parts) + [1, 1])[:3]
            published = datetime(int(year), int(month or 1), int(day or 1), tzinfo=UTC)
            break
    pdf_urls = [
        link["URL"]
        for link in msg.get("link") or []
        if link.get("URL") and "pdf" in (link.get("content-type") or "").lower()
    ]
    return {
        "title": title,
        "authors": authors,
        "published": published,
        "abstract": jats_to_text(msg.get("abstract")),
        "pdf_urls": pdf_urls,
    }
