"""wiki → 过滤层的闭环：L1 的"已知知识"、到期概念的相关条目、前置知识与概念页匹配。"""

from __future__ import annotations

import sqlite3

from secondbrain.wiki.store import Page, Wiki

SOLID = "扎实"


def known_knowledge(
    wiki: Wiki | None, rows: list[sqlite3.Row], item_chars: int, max_chars: int
) -> str:
    """与本批候选相关的 wiki 页面及其掌握度（限 max_chars）；没有相关页面时返回空串。"""
    if wiki is None or max_chars <= 0 or not wiki.pages:
        return ""
    text = " ".join(
        f"{r['title']} {(r['content'] or r['summary'] or '')[:item_chars]}" for r in rows
    )
    pages = wiki.mentioned_in(text, limit=30)
    if not pages:
        return ""
    lines = ["## 已知知识（来自用户的 wiki）", ""]
    total = sum(len(line) for line in lines)
    for p in pages:
        summary = (p.meta.get("summary") or "").replace("\n", " ")
        mastery = p.meta.get("mastery") or "—"
        line = f"- {p.title}（{p.type}；掌握度：{mastery}）— {summary}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) if len(lines) > 2 else ""


def related_item(
    page: Page, candidates: list[sqlite3.Row], l1: dict[int, dict], l2: dict[int, dict]
) -> int | None:
    """近期条目中讲到该概念的一条（标题、L1 topics 或 L2 前置知识命中概念名），取最新的。"""
    names = [n for n in page.names() if len(n) >= 3]
    for row in candidates:  # 已按时间倒序
        terms = [row["title"].lower()]
        terms += [t.replace("-", " ").lower() for t in l1.get(row["id"], {}).get("topics", [])]
        terms += [p.lower() for p in l2.get(row["id"], {}).get("prerequisites", [])]
        if any(n in t or t == n for n in names for t in terms):
            return row["id"]
    return None


def wiki_refresh_link(wiki: Wiki | None, prerequisite: str) -> tuple[Page | None, bool]:
    """(匹配到的概念页, 是否需要回炉)。掌握度不是"扎实"的概念页需要回炉。"""
    if wiki is None:
        return None, False
    page = wiki.find(prerequisite)
    if page is None or page.type != "concept":
        return None, False
    return page, page.meta.get("mastery") != SOLID
