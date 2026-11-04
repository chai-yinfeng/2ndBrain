"""知识图谱数据（ADR 0013）：节点 = wiki 页面，边 = 页面间的 [[wikilink]]。

指向不存在页面的链接不画（lint 会报告）；raw/ 原文不是页面，不出现在图中。
"""

from __future__ import annotations

from datetime import date

from secondbrain.wiki.review import due_date
from secondbrain.wiki.store import Wiki


def build_graph(wiki: Wiki, today: date) -> dict:
    edges: set[tuple[str, str]] = set()
    for slug, page in wiki.pages.items():
        for target in page.links():
            if target != slug and target in wiki.pages:
                edges.add(tuple(sorted((slug, target))))
    degree = dict.fromkeys(wiki.pages, 0)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    nodes = []
    for slug, page in sorted(wiki.pages.items()):
        due = due_date(page) if page.type == "concept" else None
        nodes.append(
            {
                "id": slug,
                "label": page.title,
                "type": page.type,
                "mastery": page.meta.get("mastery"),
                "due": bool(due and due <= today),
                "degree": degree[slug],
                "url": f"/wiki/page/{slug}",
            }
        )
    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in sorted(edges)],
    }
