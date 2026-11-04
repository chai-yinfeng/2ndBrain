"""wiki 体检：断链 / 缺失页面、孤立页、frontmatter 缺字段、index 不一致；
可选用 LLM 检查矛盾。只报告，不修改。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from secondbrain.llm.base import LLMBackend, Usage
from secondbrain.llm.schemas import LintFindings
from secondbrain.wiki.store import REQUIRED_FIELDS, WIKILINK_RE, Wiki, slugify_safe


@dataclass
class LintReport:
    broken_links: dict[str, list[str]] = field(default_factory=dict)  # 页面 → 指向不存在页面的链接
    missing_pages: dict[str, int] = field(default_factory=dict)  # 被链接但不存在的 slug → 次数
    orphans: list[str] = field(default_factory=list)  # 没有任何其他页面链接到它
    missing_fields: dict[str, list[str]] = field(default_factory=dict)
    index_missing: list[str] = field(default_factory=list)  # 页面存在但不在 index 中
    index_dangling: list[str] = field(default_factory=list)  # index 中列出但页面不存在
    contradictions: list[str] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def problems(self) -> int:
        return (
            sum(len(v) for v in self.broken_links.values())
            + len(self.orphans)
            + sum(len(v) for v in self.missing_fields.values())
            + len(self.index_missing)
            + len(self.index_dangling)
            + len(self.contradictions)
        )


def lint(wiki: Wiki) -> LintReport:
    report = LintReport()
    inbound: dict[str, int] = {slug: 0 for slug in wiki.pages}
    for slug, page in sorted(wiki.pages.items()):
        missing = [f for f in REQUIRED_FIELDS if f not in page.meta]
        if missing:
            report.missing_fields[slug] = missing
        for target in sorted(page.links()):
            if target == slug:
                continue
            if target in wiki.pages:
                inbound[target] += 1
            else:
                report.broken_links.setdefault(slug, []).append(target)
                report.missing_pages[target] = report.missing_pages.get(target, 0) + 1
    report.orphans = sorted(s for s, n in inbound.items() if n == 0)
    index_text = wiki.index_path.read_text(encoding="utf-8") if wiki.index_path.exists() else ""
    listed = {slugify_safe(m) for m in WIKILINK_RE.findall(index_text)} - {""}
    report.index_missing = sorted(set(wiki.pages) - listed)
    report.index_dangling = sorted(listed - set(wiki.pages))
    return report


def llm_contradictions(wiki: Wiki, backend: LLMBackend, max_chars: int = 12_000) -> LintFindings:
    """把各页摘要与开头交给 cheap 模型，找可能相互矛盾的说法。"""
    pages, total = [], 0
    for p in sorted(wiki.pages.values(), key=lambda p: p.slug):
        excerpt = {
            "slug": p.slug,
            "title": p.title,
            "summary": p.meta.get("summary"),
            "text": p.body[:600],
        }
        total += len(json.dumps(excerpt, ensure_ascii=False))
        if total > max_chars:
            break
        pages.append(excerpt)
    system = (
        "你在检查一个个人知识 wiki 中可能相互矛盾或明显过时的说法。只依据给出的页面节选，"
        "只报告有把握的问题，没有就返回空列表。只输出 json："
        '{"issues": [{"pages": ["slug-a", "slug-b"], "description": "……"}]}'
    )
    user = "页面节选：\n" + json.dumps(pages, ensure_ascii=False)
    findings, _ = backend.complete_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        LintFindings,
        "cheap",
    )
    return findings
