"""HTML 正文提取（trafilatura）。"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

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
    parser = _DocumentMeta()
    parser.feed(html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html)
    title = meta.title if meta else None
    head_title = " ".join("".join(parser.title_parts).split())
    placeholder = not title or title.casefold().strip() in {
        "untitled", "untitled document", "home", "index", "welcome"
    }
    shortened = bool(title and len(title) <= 20 and len(head_title) > len(title) + 5
                     and head_title.casefold().startswith(title.casefold()))
    if placeholder or shortened:
        headings = sorted(parser.headings, key=lambda entry: entry[0])
        title = next((value for _, value in headings if value != title), None) or (
            head_title if shortened else None
        )
    return PageMeta(title=title or None, author=meta.author if meta else None,
                    date=meta.date if meta else None)


class _DocumentMeta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}
        self.title_parts: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2"):
            self._heading_tag, self._heading_parts = tag, []
        elif tag == "meta":
            a = dict(attrs)
            name = (a.get("name") or a.get("property") or "").strip().lower()
            if name.startswith("citation_") and a.get("content"):
                self.values.setdefault(name, []).append(a["content"].strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            title = " ".join("".join(self._heading_parts).split())
            if title:
                self.headings.append((tag, title))
            self._heading_tag = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._heading_tag:
            self._heading_parts.append(data)


def citation_meta(html: str) -> dict:
    """出版社页面的 Highwire citation_* meta（USENIX、ACM、IEEE 等都有）：
    {pdf_url, title, authors, date, doi, arxiv_id}，没有的键不出现。"""
    parser = _DocumentMeta()
    parser.feed(html)
    v = parser.values
    result: dict = {}
    for key, name in (
        ("pdf_url", "citation_pdf_url"),
        ("title", "citation_title"),
        ("doi", "citation_doi"),
        ("arxiv_id", "citation_arxiv_id"),
        ("venue", "citation_conference_title"),
    ):
        if v.get(name):
            result[key] = v[name][0]
    date = (v.get("citation_publication_date") or v.get("citation_date") or [None])[0]
    if date:
        result["date"] = date
    if v.get("citation_author"):
        result["authors"] = [
            " ".join(reversed([part.strip() for part in name.split(",")]))
            if name.count(",") == 1 else name
            for name in v["citation_author"]
        ]
    if not result.get("venue") and v.get("citation_journal_title"):
        result["venue"] = v["citation_journal_title"][0]
    if (result.get("arxiv_id") and date and not v.get("citation_publication_date")
            and not result.get("venue")):
        # An arXiv submission date is not the later proceedings publication year.
        result["preprint_year"] = int(date[:4]) if date[:4].isdigit() else None
    if result.get("doi"):
        result["doi"] = result["doi"].removeprefix("https://doi.org/").removeprefix("doi:").lower()
    return result
