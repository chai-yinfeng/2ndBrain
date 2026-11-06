"""Semantic Scholar Graph API（无需 key）：按 DOI 查论文元数据。
用于手动添加 DOI 链接（ADR 0019）。"""

from __future__ import annotations

import re

from secondbrain.net import FetchError, HttpClient

API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
FIELDS = "title,authors,year,abstract,openAccessPdf,externalIds,url"
# doi.org/10.x/...、dl.acm.org/doi/10.x/...、/doi/abs|pdf|full|epdf/10.x/...
_DOI = re.compile(
    r"(?:doi\.org/|/doi/(?:abs/|pdf/|full/|epdf/|fulltext/|reader/)?)(10\.\d{4,9}/[^\s?#]+)", re.I
)


def doi_in_url(url: str) -> str | None:
    """URL 中的 DOI（小写，去掉末尾的 / 与 .pdf）；没有时返回 None。"""
    m = _DOI.search(url)
    if not m:
        return None
    doi = m.group(1).rstrip("/")
    if doi.lower().endswith(".pdf"):
        doi = doi[:-4]
    return doi.lower()


def lookup_doi(client: HttpClient, doi: str) -> dict:
    """返回 S2 的论文记录。HTTP 错误按 HttpClient 的重试设置处理（429 也只重试有限次）；
    失败时抛 FetchError，消息说明原因。"""
    try:
        resp = client.get(API.format(doi=doi), params={"fields": FIELDS})
    except FetchError as exc:
        text = str(exc)
        if "429" in text:
            raise FetchError("Semantic Scholar 限流（HTTP 429），稍后再试") from exc
        if "404" in text:
            raise FetchError(f"Semantic Scholar 没有收录 DOI {doi}") from exc
        raise FetchError(f"Semantic Scholar 查询失败：{text}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise FetchError("Semantic Scholar 返回的不是 JSON") from exc
    if not isinstance(data, dict) or not data.get("title"):
        raise FetchError("Semantic Scholar 返回的记录没有标题")
    return data
