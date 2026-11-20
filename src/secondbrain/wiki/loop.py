"""wiki 相关内容参考；不把页面存在或历史字段当作用户掌握度。"""

from __future__ import annotations

import sqlite3

from secondbrain.wiki.store import Wiki


def related_knowledge(
    wiki: Wiki | None, rows: list[sqlite3.Row], item_chars: int, max_chars: int
) -> str:
    """提供可见相关页面摘要，不评价用户是否已知或需要复习。"""
    if wiki is None or max_chars <= 0 or not wiki.pages:
        return ""
    text = " ".join(
        f"{r['title']} {(r['content'] or r['summary'] or '')[:item_chars]}" for r in rows
    )
    pages = [
        page
        for page in wiki.mentioned_in(text, limit=len(wiki.pages))
        if page.is_visible
    ][:30]
    if not pages:
        return ""
    lines = ["## 相关知识内容（内容参考，不代表用户已掌握）", ""]
    total = sum(len(line) for line in lines)
    for p in pages:
        summary = (p.meta.get("summary") or "").replace("\n", " ")
        warning = "；来源已变化，待复核" if p.material_sync_status == "review" else ""
        line = f"- {p.title}（{p.kind}{warning}）— {summary}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) if len(lines) > 2 else ""


