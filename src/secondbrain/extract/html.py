"""HTML 正文提取（trafilatura）。"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura


def extract_main_text(html: str | bytes, url: str | None = None) -> str | None:
    """从完整网页中提取正文；提取不到返回 None。"""
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    return text.strip() if text else None


def html_fragment_to_text(fragment: str) -> str:
    """把 feed 中的 HTML 片段（content / summary）转成纯文本。"""
    if "<" not in fragment:
        return fragment.strip()
    wrapped = f"<html><body><article>{fragment}</article></body></html>"
    text = trafilatura.extract(wrapped, include_comments=False, favor_recall=True)
    if not text:
        text = trafilatura.html2txt(fragment)
    return (text or "").strip()


@dataclass
class PageMeta:
    title: str | None = None
    author: str | None = None
    date: str | None = None  # trafilatura 识别的发布日期（YYYY-MM-DD）


def extract_page_meta(html: str | bytes, url: str | None = None) -> PageMeta:
    """从完整网页中提取标题 / 作者 / 发布日期；识别不到的字段为 None。"""
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is None:
        return PageMeta()
    return PageMeta(title=meta.title or None, author=meta.author or None, date=meta.date or None)
