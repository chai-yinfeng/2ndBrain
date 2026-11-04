"""把"学了"的条目编进 wiki：LLM 输出受限的编辑操作（WikiPlan），代码校验后落盘。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from secondbrain import db
from secondbrain.llm.base import LLMBackend, Usage
from secondbrain.llm.schemas import PageUpdate, WikiPlan
from secondbrain.llm.usage import estimate_cost
from secondbrain.models import FilterSettings
from secondbrain.pipeline.run import PROMPTS_DIR
from secondbrain.profile.loader import Profile, mastery_for
from secondbrain.wiki.store import (
    WIKILINK_RE,
    Page,
    Wiki,
    WikiError,
    add_list_items,
    append_section,
    new_meta,
    replace_section,
    slugify,
    slugify_safe,
)

EST_CHARS_PER_TOKEN = 3
EST_OUTPUT_TOKENS = 8000  # 实测一次 ingest 输出约 7–8k token


@dataclass
class IngestResult:
    item_id: int
    title: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    usage: Usage | None = None
    error: str | None = None


def item_payload(row: sqlite3.Row, l2: dict | None, s: FilterSettings) -> dict:
    """条目内容：短文整篇；长文只给 L2 的 TL;DR、要点（控制成本）。"""
    content = row["content"] or row["summary"] or ""
    payload = {
        "title": row["title"],
        "source": row["source_id"],
        "url": row["url"],
        "tldr": (l2 or {}).get("tldr"),
        "key_points": (l2 or {}).get("key_points", []),
        "prerequisites": (l2 or {}).get("prerequisites", []),
    }
    if len(content) <= s.wiki.ingest_max_chars or not l2:
        payload["text"] = content[: s.wiki.ingest_max_chars]
    return payload


def related_pages(wiki: Wiki, payload: dict, s: FilterSettings) -> list[Page]:
    text = " ".join(
        [
            payload["title"],
            payload.get("tldr") or "",
            *payload["key_points"],
            *payload["prerequisites"],
        ]
    )
    return wiki.mentioned_in(text, s.wiki.related_pages)


def ingest_messages(
    wiki: Wiki, payload: dict, related: list[Page], s: FilterSettings
) -> list[dict]:
    system = (PROMPTS_DIR / "wiki_ingest.md").read_text(encoding="utf-8")
    index = wiki.index_path.read_text(encoding="utf-8")[: s.wiki.index_max_chars]
    pages = [
        {
            "slug": p.slug,
            "type": p.type,
            "title": p.title,
            "excerpt": p.body[: s.wiki.related_page_chars],
        }
        for p in related
    ]
    user = "请把以下条目编进 wiki，按规定格式输出 json：\n" + json.dumps(
        {"item": payload, "index": index, "related_pages": pages}, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def estimate_ingest_cost(messages: list[dict], model: str, s: FilterSettings, ts) -> float:
    chars = sum(len(m["content"]) for m in messages)
    tokens = chars // EST_CHARS_PER_TOKEN
    usage = Usage(model, tokens, 0, tokens, EST_OUTPUT_TOKENS)
    return estimate_cost(usage, s.pricing, s.pricing_schedule, ts)[0]


def _clean_links(text: str) -> str:
    """把 [[Some Title]] 规范为 [[some-title]]，便于 Obsidian 与断链检查。"""

    def fix(match):
        slug = slugify_safe(match.group(1))
        return f"[[{slug}]]" if slug else match.group(0)

    return WIKILINK_RE.sub(fix, text)


def apply_plan(
    wiki: Wiki,
    plan: WikiPlan,
    row: sqlite3.Row,
    profile: Profile,
    today: date,
) -> IngestResult:
    """校验并执行编辑操作；非法操作被拒绝并记录原因。最后重建 index、追加 log。"""
    result = IngestResult(row["id"], row["title"])
    result.rejected.extend(f"解析时丢弃 {d}" for d in plan.dropped)
    source_line = f"[{row['title']}]({{raw}})（[原文]({row['url']})）"
    touched: dict[str, Page] = {}

    def add_source(page: Page) -> None:
        sources = list(page.meta.get("sources") or [])
        if row["id"] not in sources:
            sources.append(row["id"])
        page.meta["sources"] = sources
        page.meta["updated"] = today.isoformat()
        link = source_line.format(raw=wiki.raw_link(row["id"], page.type))
        page.body = add_list_items(page.body, "来源", [link])

    for op in plan.creates:
        try:
            slug = slugify(op.slug)
            wiki.page_path(slug, op.type)
        except WikiError as exc:
            result.rejected.append(f"create {op.slug!r}：{exc}")
            continue
        existing = wiki.find(slug) or wiki.find(op.title)
        if existing is not None:  # 已存在：改为追加小节，避免重复页面
            existing.body = append_section(
                existing.body, f"补充：{row['title']}", _clean_links(op.body)
            )
            add_source(existing)
            touched[existing.slug] = existing
            result.updated.append(existing.slug)
            continue
        mastery = None
        if op.type == "concept":
            mastery = mastery_for(profile, [op.title, slug.replace("-", " "), *op.aliases])
        meta = new_meta(
            op.title,
            op.type,
            today,
            summary=op.summary.strip(),
            aliases=op.aliases,
            mastery=mastery,
        )
        page = Page(slug, meta, f"# {op.title}\n\n{_clean_links(op.body).strip()}\n")
        add_source(page)
        wiki.pages[slug] = page
        touched[slug] = page
        result.created.append(slug)

    for op in plan.updates:
        page = wiki.find(op.slug)
        if page is None:
            result.rejected.append(f"update {op.slug!r}：页面不存在")
            continue
        problem = _apply_update(wiki, page, op)
        if problem:
            result.rejected.append(f"update {op.slug!r}（{op.action}）：{problem}")
            continue
        add_source(page)
        touched[page.slug] = page
        if page.slug not in result.created and page.slug not in result.updated:
            result.updated.append(page.slug)

    for page in touched.values():
        wiki.save_page(page)
    wiki.rebuild_index()
    lines = [f"新建 [[{s}]]" for s in result.created] + [f"更新 [[{s}]]" for s in result.updated]
    lines += [f"拒绝：{r}" for r in result.rejected]
    wiki.append_log(today, "ingest", row["title"], lines or ["无改动"])
    return result


def _apply_update(wiki: Wiki, page: Page, op: PageUpdate) -> str | None:
    if op.action in ("append_section", "replace_section"):
        if not op.section or not op.text:
            return "需要 section 与 text"
        fn = append_section if op.action == "append_section" else replace_section
        page.body = fn(page.body, op.section, _clean_links(op.text))
    elif op.action == "add_links":
        slugs = []
        for link in op.links:
            try:
                slugs.append(slugify(link))
            except WikiError:
                continue
        if not slugs:
            return "没有有效的链接"
        page.body = add_list_items(page.body, "相关", [f"[[{s}]]" for s in slugs])
    else:
        new = [a for a in op.aliases if a and a not in page.aliases]
        if not new:
            return "没有新的别名"
        page.meta["aliases"] = page.aliases + new
    return None


def pending_items(conn: sqlite3.Connection) -> list[int]:
    done = db.ingested_ids(conn)
    return [i for i in db.learned_item_ids(conn) if i not in done]


def ingest_item(
    conn: sqlite3.Connection,
    wiki: Wiki,
    backend: LLMBackend,
    profile: Profile,
    row: sqlite3.Row,
    s: FilterSettings,
    today: date,
    *,
    estimate_only: bool = False,
) -> tuple[IngestResult | None, float, list[dict]]:
    """返回 (结果, 预估费用, 消息)。estimate_only 时只计算预估，不调用 LLM、不写盘。"""
    l2 = db.item_assessment(conn, row["id"], "L2")
    payload = item_payload(row, l2, s)
    messages = ingest_messages(wiki, payload, related_pages(wiki, payload, s), s)
    estimate = estimate_ingest_cost(messages, backend.model_name("strong"), s, datetime.now(UTC))
    if estimate_only:
        return None, estimate, messages
    wiki.write_raw(row["id"], row["title"], row["url"], row["content"] or row["summary"] or "")
    plan, usage = backend.complete_json(messages, WikiPlan, "strong")
    result = apply_plan(wiki, plan, row, profile, today)
    result.usage = usage
    db.record_ingest(conn, row["id"], result.created + result.updated, datetime.now(UTC))
    conn.commit()
    return result, estimate, messages
