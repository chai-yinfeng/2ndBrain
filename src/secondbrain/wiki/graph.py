"""知识图谱数据（ADR 0013）：节点 = wiki 页面，边 = 页面间的 [[wikilink]]。

指向不存在页面的链接不画（lint 会报告）；raw/ 原文不是页面，不出现在图中。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from secondbrain.wiki.review import due_date
from secondbrain.wiki.store import Page, Wiki


def link_index(wiki: Wiki) -> dict[str, dict]:
    """从当前 Markdown 快照派生连接；图谱、页面反链与体检共享消歧规则。"""
    index = {
        slug: {"outgoing": [], "backlinks": [], "missing": [], "ambiguous": []}
        for slug in wiki.pages
    }
    for slug, page in sorted(wiki.pages.items()):
        outgoing = {}
        for name in sorted(page.links()):
            matches = wiki.resolve(name, source=page)
            if not matches:
                if not wiki.is_resource_link(name, source=page):
                    index[slug]["missing"].append(name)
            elif len(matches) > 1:
                index[slug]["ambiguous"].append(name)
            elif matches[0].slug != slug:
                outgoing[matches[0].slug] = matches[0]
        index[slug]["outgoing"] = [outgoing[key] for key in sorted(outgoing)]
        for target in outgoing:
            index[target]["backlinks"].append(page)
    return index


def page_connections(wiki: Wiki, page: Page) -> dict:
    """outgoing/backlinks 为 Page 列表，missing/ambiguous 为原始目标名列表。"""
    return link_index(wiki)[page.slug]


def build_graph(wiki: Wiki, today: date) -> dict:
    connections = link_index(wiki)
    edges: set[tuple[str, str]] = set()
    for slug, links in connections.items():
        for target in links["outgoing"]:
            edges.add(tuple(sorted((slug, target.slug))))
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
                "aliases": page.aliases,
                "sources": page.meta.get("sources") or [],
                "material_id": page.meta.get("material_id"),
                "mastery": page.meta.get("mastery"),
                "due": bool(due and due <= today),
                "degree": degree[slug],
                "url": f"/wiki/page/{quote(slug, safe='/')}",
            }
        )
    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in sorted(edges)],
    }
