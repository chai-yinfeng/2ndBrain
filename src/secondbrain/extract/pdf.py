"""PDF 文本提取（pypdf），只取前 max_pages 页。"""

from __future__ import annotations

import io
import logging
import re
import unicodedata

from pypdf import PdfReader

from secondbrain.bibliography import normalized_title, split_authors

# pypdf 对不规范的 PDF 会打印大量告警（如 XObject 调用次数超限），不影响已提取的文本。
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf_text(
    data: bytes, max_pages: int, *, metadata: dict | None = None
) -> str:
    reader = PdfReader(io.BytesIO(data))
    count = min(len(reader.pages), max_pages)
    if metadata is not None:
        metadata.update(total_pages=len(reader.pages), extracted_pages=count)
        metadata["bibliography"] = _bibliography(reader)
    texts = [(reader.pages[i].extract_text() or "").strip() for i in range(count)]
    return "\n\n".join(t for t in texts if t).strip()


def _clean_title(value: str) -> str | None:
    value = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
    if (len(value) < 12 or len(value) > 350
            or re.search(r"\.(?:pdf|mif|docx?|tex|ps)\b|https?://|untitled|microsoft word",
                         value, re.I)):
        return None
    return value


def _person(value: str) -> bool:
    words = value.split()
    return (2 <= len(words) <= 6 and not re.search(
        r"@|university|department|institute|laborator|administrator|anonymous|copyright|"
        r"\b(?:inc|corp|research|school|science|engineering|edition|report)\b",
        value, re.I) and all(
            re.fullmatch(r"[^\W\d_](?:[^\W\d_]|[.'’\-])*", word)
            and (word[0].isupper() or word in {"de", "del", "van", "von", "da", "di", "bin"})
            for word in words
        ))


def _author_line(line: str) -> list[str]:
    # Mixed final rows can contain an institution after the last author.
    if re.search(r"\bR\s*&\s*D\b", line):
        return []
    # Some typesetters double every word space; columns are wider still.
    gaps = re.findall(r" +", line.strip())
    separator = max(2, min(map(len, gaps), default=1) + 1)
    line = re.sub(rf" {{{separator},}}", "; ", line.strip())
    line = " ".join(line.split())
    names, fragment = [], ""
    for part in split_authors(line):
        if re.search(r"\b(?:inc|corp|university|department|institute|research)\b", part, re.I):
            break
        if _person(part):
            names.append(part)
            fragment = ""
        elif len(part.split()) == 1:
            combined = f"{fragment} {part}".strip()
            if fragment and _person(combined):
                names.append(combined)
                fragment = ""
            else:
                fragment = part
    return names


def _bibliography(reader: PdfReader) -> dict:
    # Publisher covers need not contain an abstract; inspect the opening pages
    # without changing the caller's extraction/page-limit accounting.
    for page in reader.pages[:3]:
        facts = _page_bibliography(page, reader.metadata or {})
        if facts:
            return facts
    title = _clean_title((reader.metadata or {}).get("/Title", ""))
    return {"title": title, "evidence": ["pdf:info-title"], "verified": True} if title else {}


def _page_bibliography(page, info: dict) -> dict:
    layout = page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False)
    layout = layout or page.extract_text() or ""
    header = re.split(r"(?im)^\s*(?:abstract\b|1[.\s]+introduction\b)", layout)[0]
    lines = [line.strip() for line in header.splitlines() if line.strip()]
    title_lines, authors, evidence = [], [], []
    for index, line in enumerate(lines):
        if line in {"TM", "®", "©"}:
            continue
        if "," in line and index + 1 < len(lines):
            tail = line.rsplit(",", 1)[-1].strip()
            head = lines[index + 1].split(",", 1)[0].strip()
            if len(tail.split()) == len(head.split()) == 1 and _person(f"{tail} {head}"):
                line += " " + head
        if re.search(r"proceedings|copyright|appears in|conference|symposium|journal|arxiv|"
                     r"https?://|technical report|\bedition\b", line, re.I):
            continue
        if authors and re.match(r"[\d*†‡∗⋆★]+[^\W\d_]", line):
            continue
        if re.search(r"\b(?:19|20)\d{2}\b", line) and not authors:
            # Frontmatter dates are not title text or author names.
            continue
        names = _author_line(line)
        following = _author_line(lines[index + 1]) if index + 1 < len(lines) else []
        author_row = (len(names) > 1 or authors or (
            title_lines and names and (
                len(following) == 1
                or (index + 1 < len(lines) and re.search(
                    r"university|department|institute|@|\b(?:inc|corp|research)\b",
                    lines[index + 1], re.I
                ))
            )
        ))
        if names and author_row:
            authors.extend(names)
        elif not authors:
            title_lines.append(line)
    title = _clean_title(" ".join(title_lines))
    if title:
        evidence.append("pdf:first-page-layout-title")
    if authors:
        evidence.append("pdf:first-page-layout-authors")
    info_title = _clean_title(info.get("/Title", ""))
    info_authors = split_authors(str(info.get("/Author", "")))
    info_authors = info_authors if info_authors and all(_person(a) for a in info_authors) else []
    # Info is supporting evidence, never a filename/admin/production timestamp.
    if info_title and (not title or normalized_title(info_title) == normalized_title(title)):
        title = title or info_title
        evidence.append("pdf:info-title")
        if not authors and info_authors:
            authors = info_authors
            evidence.append("pdf:info-authors")
    publication = []
    for line in layout.splitlines():
        if re.search(r"proceedings|symposium|conference|journal|copyright|©|"
                     r"\b[A-Z][A-Z0-9]{2,}\s*[’']\s*\d{2}\b", line, re.I):
            years = re.findall(r"\b(?:19|20)\d{2}\b", line)
            if not years:
                years = [str(1900 + int(y) if int(y) >= 70 else 2000 + int(y))
                         for y in re.findall(r"\b[A-Z][A-Z0-9]{2,}\s*[’']\s*(\d{2})\b", line)]
            if years:
                publication.append((" ".join(line.split()), years))
    years = {year for _, values in publication for year in values}
    if not years and re.search(r"technical report", header, re.I):
        years = set(re.findall(r"\b(?:19|20)\d{2}\b", header))
    year = int(next(iter(years))) if len(years) == 1 else None
    if year:
        evidence.append("pdf:first-page-publication-year")
    # A title alone is not evidence that an arbitrary PDF is an academic paper.
    academic = re.search(r"\babstract\b|technical report|arxiv\s*:", layout, re.I)
    if not title or not authors or not academic:
        return {}
    doi = re.findall(r"(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)(10\.\d{4,9}/[^\s<>]+)",
                     layout, re.I)
    doi = {value.rstrip(".,;)]}").lower() for value in doi}
    arxiv = set(re.findall(r"arxiv\s*:\s*(\d{4}\.\d{4,5})", layout, re.I))
    return {"title": title, "authors": list(dict.fromkeys(authors)), "year": year,
            "venue": publication[0][0] if publication else None,
            "doi": next(iter(doi)) if len(doi) == 1 else None,
            "arxiv_id": next(iter(arxiv)) if len(arxiv) == 1 else None,
            "kind": "paper", "evidence": evidence, "verified": True}
