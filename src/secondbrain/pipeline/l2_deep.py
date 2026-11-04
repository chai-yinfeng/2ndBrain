"""L2 精读：strong 模型逐条阅读截断后的正文。"""

from __future__ import annotations

import json
import re
import sqlite3

from secondbrain.models import L2Settings

PAPER_METHODS = {"arxiv_html", "pdf"}

_KNOWN_HEADING = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*|[IVX]+)\.?\s+)?"
    r"(abstract|introduction|conclusions?|concluding remarks|discussion|summary|related work|"
    r"background|methods?|methodology|experiments?|results|evaluation|limitations|appendix|"
    r"references|acknowledg\w*)\b[^.]{0,60}$",
    re.I,
)
_NUMBERED_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVX]+)\.?\s+[A-Z][^.]{2,70}$")


def is_paper(row: sqlite3.Row) -> bool:
    return row["extract_method"] in PAPER_METHODS or "arxiv.org/abs/" in row["url"]


def _heading_kind(line: str) -> str | None:
    """返回 abstract / introduction / conclusion / other；不是标题时返回 None。"""
    if len(line) > 90:
        return None
    m = _KNOWN_HEADING.match(line)
    if m:
        word = m.group(1).lower()
        if word == "abstract":
            return "abstract"
        if word == "introduction":
            return "introduction"
        if word.startswith("conclu"):
            return "conclusion"
        return "other"
    return "other" if _NUMBERED_HEADING.match(line) else None


def paper_sections(content: str) -> dict[str, str]:
    """按标题切分论文正文，返回首次出现的 abstract / introduction / conclusion。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.splitlines():
        kind = _heading_kind(line.strip())
        if kind is not None:
            current = kind if kind != "other" and kind not in sections else None
            if current:
                sections[current] = []
            continue
        if current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}


def build_l2_text(content: str, summary: str | None, paper: bool, s: L2Settings) -> str:
    """论文优先 abstract + introduction + conclusion；找不到章节时取正文开头。"""
    if paper:
        found = paper_sections(content)
        if "introduction" in found or "conclusion" in found:
            parts = []
            abstract = found.get("abstract") or summary
            if abstract:
                parts.append("[Abstract]\n" + abstract[: s.section_max_chars])
            for name in ("introduction", "conclusion"):
                if name in found:
                    parts.append(f"[{name.title()}]\n" + found[name][: s.section_max_chars])
            return "\n\n[...]\n\n".join(parts)[: s.max_input_chars]
    return content[: s.max_input_chars]


def l2_messages(system: str, row: sqlite3.Row, s: L2Settings) -> list[dict[str, str]]:
    paper = is_paper(row)
    content = row["content"] or row["summary"] or ""
    item = {
        "id": row["id"],
        "title": row["title"],
        "source": row["source_id"],
        "url": row["url"],
        "is_paper": paper,
        "full_length_chars": len(content),
        "text": build_l2_text(content, row["summary"], paper, s),
    }
    user = "请精读以下条目，按规定格式输出 json：\n" + json.dumps(
        {"item": item}, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
