"""离线 fake 后端：根据标题 / 关键词 / profile 确定性地生成评分，不访问网络。

- relevance：条目文本命中的关键词数（内置 AI infra 关键词 ∪ profile 中的英文词）。
- quality / novelty / refresher_value / content_type：标题关键词优先，其余由标题哈希决定，
  保证同样的输入总得到同样的输出，且一批条目里各 lane 都有机会出现。
"""

from __future__ import annotations

import hashlib
import json
import re

from secondbrain.llm.base import LLMError, T, Tier, Usage
from secondbrain.llm.schemas import (
    REFRESHER_TYPES,
    ChunkNotes,
    DeepSummary,
    L1Response,
    L2Response,
    LintFindings,
    PrereqExplanations,
    ProfileProposal,
    SourceProfile,
    WikiPlan,
)

DEFAULT_KEYWORDS = (
    "inference",
    "serving",
    "kv cache",
    "attention",
    "llm",
    "gpu",
    "kernel",
    "cuda",
    "triton",
    "distributed",
    "consensus",
    "parallel",
    "quantization",
    "speculative",
    "transformer",
    "agent",
    "batching",
    "vllm",
    "sglang",
    "scheduling",
)

_TYPE_PATTERNS = (
    ("survey", re.compile(r"\b(survey|review|overview|landscape)\b", re.I)),
    (
        "tutorial",
        re.compile(r"\b(tutorial|guide|how to|walkthrough|from scratch|hands-on)\b", re.I),
    ),
    (
        "explainer",
        re.compile(r"\b(explain\w*|introduction|intro to|understanding|primer|what is)\b", re.I),
    ),
    ("opinion", re.compile(r"\b(opinion|thoughts|essay|why i|reflections?)\b", re.I)),
)
_HASH_TYPES = {0: "explainer", 1: "survey", 2: "opinion"}

L1_COMPARABLE_CHARS = 4000

_STAMPED_LINE = re.compile(r"^(?:- )?\[(?:\d{1,2}:)?\d{1,2}:\d{2}\][^\n]*", re.M)

_PROFILE_RE = re.compile(r"<profile>(.*?)</profile>", re.S)


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def _keywords(system: str) -> set[str]:
    words = set(DEFAULT_KEYWORDS)
    m = _PROFILE_RE.search(system)
    if m:
        words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z+-]{3,}", m.group(1))}
    return words


def _payload(messages: list[dict[str, str]]) -> dict:
    """user 消息以单行 json 结尾（前面可能有"已知知识"等说明）。"""
    content = messages[-1]["content"]
    start = content.rfind("\n{") + 1 if "\n{" in content else content.index("{")
    return json.loads(content[start:])


def _assess(title: str, text: str, keywords: set[str]) -> dict:
    h = _digest(title)
    haystack = f"{title} {text}".lower()
    hits = sorted(k for k in keywords if k in haystack)
    content_type = next((t for t, rx in _TYPE_PATTERNS if rx.search(title)), None)
    content_type = content_type or _HASH_TYPES.get(h[3] % 10, "frontier")
    is_refresher = content_type in REFRESHER_TYPES
    return {
        "relevance": min(9, 2 + 2 * len(hits)),
        "quality": 5 + h[0] % 5,
        "novelty": (3 if is_refresher else 6) + h[1] % 4,
        "refresher_value": (7 + h[2] % 3) if is_refresher else h[2] % 4,
        "content_type": content_type,
        "topics": hits[:3],
    }


class FakeBackend:
    name = "fake"

    def model_name(self, tier: Tier) -> str:
        return f"fake-{tier}"  # 不在价格表中，费用计 0

    def ping(self) -> Usage:
        return Usage(self.model_name("cheap"))

    def complete_json(
        self, messages: list[dict[str, str]], schema: type[T], tier: Tier
    ) -> tuple[T, Usage]:
        keywords = _keywords(messages[0]["content"])
        rewrite = schema.__name__ == "RewriteDraft"
        payload = json.loads(messages[1]["content"]) if rewrite else _payload(messages)
        if schema is L1Response:
            items = []
            for it in payload["items"]:
                a = _assess(it["title"], it.get("text", ""), keywords)
                hits = "、".join(a["topics"]) or "无"
                items.append(
                    {"id": it["id"], **a, "one_line_reason": f"（fake）命中关键词：{hits}"}
                )
            obj: dict = {"items": items}
        elif schema is L2Response:
            it = payload["item"]
            # 与 L1 看同样长度的开头，使 L1 / L2 的评分口径一致
            a = _assess(it["title"], it.get("text", "")[:L1_COMPARABLE_CHARS], keywords)
            sentences = [
                s.strip() for s in re.split(r"(?<=[.!?。])\s+", it.get("text", "")) if s.strip()
            ]
            stamped = [m.group(0) for m in _STAMPED_LINE.finditer(it.get("text", ""))]
            if stamped:  # 视频 / 逐段提要：要点带时间戳
                sentences = [line.removeprefix("- ").strip() for line in stamped]
            # 粗略估时：约 3000 字符 / 分钟，论文 ×1.5，限制在 5–45 分钟
            minutes = it.get("full_length_chars", 0) / 3000 * (1.5 if it.get("is_paper") else 1)
            obj = {
                "tldr": "（fake）" + (sentences[0][:160] if sentences else it["title"]),
                "key_points": [s[:120] for s in sentences[:3]] or [it["title"]],
                "why_for_you": f"（fake）与关键词 {'、'.join(a['topics']) or '无'} 相关。",
                "est_minutes": int(min(45, max(5, minutes))),
                "prerequisites": a["topics"][:2],
                "scores": {k: a[k] for k in ("relevance", "novelty", "quality", "refresher_value")},
            }
        elif schema is ChunkNotes:
            obj = _chunk_notes(payload)
        elif schema is ProfileProposal:
            obj = _profile_proposal(payload)
        elif schema is SourceProfile:
            obj = _source_profile(payload)
        elif schema is WikiPlan:
            obj = _wiki_plan(payload)
        elif schema is LintFindings:
            obj = {"issues": []}
        elif schema is DeepSummary:
            known = "相关知识内容" in messages[-1]["content"]
            obj = _deep_summary(payload["item"], keywords, known)
        elif schema is PrereqExplanations:
            title = payload["item"]["title"]
            obj = {
                "items": [
                    {
                        "name": p["name"],
                        "explanation": f"（fake）{p['name']}：直觉是……关键公式是……"
                        f"读懂《{title}》需要它，因为{p.get('why') or '正文用到了它'}。",
                    }
                    for p in payload["prerequisites"]
                ]
            }
        elif rewrite:
            page = payload["page"]
            request = messages[-1]["content"]
            sources = payload.get("sources") or []
            evidence = "\n".join(
                f"- {source.get('title') or source.get('url') or '来源'}"
                for source in sources if isinstance(source, dict)
            ) or "- 本页已有正文；未提供额外原文。"
            previous = next(
                (message["content"] for message in reversed(messages[:-1])
                 if message["role"] == "assistant"), ""
            )
            text = page["body"]
            facts = [
                line.strip("- #>") for line in text.splitlines()
                if line.strip() and not line.startswith(("#", "<!--"))
            ]
            body = (
                f"## {page['title']}：本轮重整\n\n"
                f"（fake 离线草稿）本轮关注：{request}\n\n"
                "### 核心内容\n\n"
                + "\n".join(f"- {line}" for line in dict.fromkeys(facts[:4]))
                + "\n\n### 来源依据\n\n" + evidence
                + ("\n\n### 对话调整\n\n依据上一轮草稿重新组织，未写入长期画像。"
                   if previous else "")
            )
            obj = {"body": body, "summary": f"（fake）按本轮语境重整：{request[:120]}"}
        else:
            raise LLMError(f"fake 后端不支持的 schema：{schema.__name__}")
        prompt_chars = sum(len(m["content"]) for m in messages)
        usage = Usage(
            model=self.model_name(tier),
            prompt_tokens=prompt_chars // 4,
            cache_miss_tokens=prompt_chars // 4,
            completion_tokens=len(json.dumps(obj, ensure_ascii=False)) // 4,
        )
        return schema.model_validate(obj), usage




def _profile_proposal(payload: dict) -> dict:
    """只根据反馈维护兴趣段落，不推断或调整掌握度。"""
    sections = [s["section"] for s in payload.get("sections", [])]
    dislike = next((s for s in sections if "不想看" in s), None)
    edits = []
    for fb in payload.get("feedback", []):
        kind, fid = fb["kind"], fb["id"]
        if kind in ("dismiss", "worthless") and dislike:
            edits.append(
                {
                    "section": dislike,
                    "action": "append",
                    "text": f"- 类似《{fb['title'][:40]}》的内容",
                    "reason": "（fake）标记不感兴趣 / 没价值",
                    "evidence": [fid],
                }
            )
    return {
        "summary": f"（fake）{len(edits)} 处兴趣段落修改",
        "section_edits": edits,
    }


def _deep_summary(it: dict, keywords: set[str], known: bool) -> dict:
    """确定性的深度速读：取正文句子（视频 / 逐段提要取带时间戳的行）填各栏。"""
    text = it.get("text", "")
    stamped = [m.group(0).removeprefix("- ").strip() for m in _STAMPED_LINE.finditer(text)]
    sentences = stamped or [s.strip() for s in re.split(r"(?<=[.!?。])\s+", text) if s.strip()]
    topics = _assess(it["title"], text[:L1_COMPARABLE_CHARS], keywords)["topics"]
    return {
        "problem": "（fake）" + (sentences[0][:160] if sentences else it["title"]),
        "method": [s[:120] for s in sentences[1:4]] or [it["title"]],
        "results": [s[:120] for s in sentences[4:6]] or ["（fake）正文没有给出数字"],
        "limitations": ["（fake）没有讨论局限"],
        "relation_to_known": "（fake）"
        + ("与相关知识页面的内容有关。" if known else "当前未提供直接相关的 wiki 内容。"),
        "sections_to_read": [{"name": "（fake）全文第 1 部分", "reason": "（fake）核心方法在这里"}],
        "prerequisites": [{"name": t, "why": f"（fake）正文用到 {t}"} for t in topics[:2]],
    }


def _chunk_notes(payload: dict) -> dict:
    """确定性逐段提要：保留首部和尾部，视频保留时间戳。"""
    text = payload.get("text", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    excerpts = [line[:100] for line in lines[:3]]
    if lines:
        excerpts.append(lines[-1][-100:])
    points = []
    for line in dict.fromkeys(excerpts):
        m = re.match(r"\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*(.*)", line)
        if m:
            points.append({"t": m.group(1), "text": m.group(2)})
        else:
            points.append({"t": None, "text": line})
    return {"summary": f"（fake）第 {payload.get('part', '?')} 段", "points": points}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "item"


def _wiki_plan(payload: dict) -> dict:
    """确定性材料整理：材料身份来自 id，概念来自正文；不要求 L2 或读后反馈。"""
    item = payload["item"]
    material = item["material_slug"]
    index = payload.get("index", "")
    pages = payload.get("related_pages", [])
    existing = {p["slug"] for p in pages}
    text = item.get("text") or ""
    haystack = f"{item['title']} {text}".lower().replace("-", " ")
    names = [
        name
        for name in ("KV cache", "Continuous batching", "PagedAttention", *DEFAULT_KEYWORDS)
        if name.lower() in haystack
    ]
    names_by_key = {}
    for name in names:
        names_by_key.setdefault(name.lower(), name)
    names = list(names_by_key.values())[:2]
    concepts = []
    for name in names:
        match = next(
            (
                p["slug"]
                for p in pages
                if p["type"] == "concept"
                and name.lower()
                in {p["title"].lower(), *(alias.lower() for alias in p.get("aliases", []))}
            ),
            None,
        )
        concepts.append(match or _slug(name))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    points = list(dict.fromkeys([*(line[:160] for line in lines[:2]), text[-160:].strip()]))
    points = [point for point in points if point]
    body = "## 材料陈述\n\n" + "\n".join(f"- {point}" for point in points)
    if concepts:
        body += "\n\n## 关联依据\n\n" + "\n".join(
            f"- [[{concept}]]：材料中出现该概念。" for concept in concepts
        )
    creates = [
        {
            "slug": material,
            "type": "material",
            "title": item["title"],
            "summary": "（fake）" + item["title"][:60],
            "body": body,
        }
    ]
    updates = []
    for concept, name in zip(concepts, names, strict=True):
        if concept in existing or f"[[{concept}]]" in index:
            updates.append({"slug": concept, "action": "add_links", "links": [material]})
        else:
            creates.append(
                {
                    "slug": concept,
                    "type": "concept",
                    "title": name,
                    "summary": f"（fake）{name}",
                    "body": f"## 材料陈述\n\n（fake）材料提到 {name}。"
                    f"\n\n## 关联依据\n\n- [[{material}]]：提供该概念的材料出处。",
                }
            )
    return {
        "summary": f"（fake）整理材料及 {len(concepts)} 个关联概念",
        "creates": creates,
        "updates": updates,
    }


def _source_profile(payload: dict) -> dict:
    """仅从实际样本确定性提取领域，不把人工标签或采集协议当成新结论。"""
    samples = payload["samples"]
    text = " ".join(
        f"{sample['title']} {sample['summary']} {' '.join(sample['topics'])}"
        for sample in samples
    ).casefold()
    patterns = (
        ("人工智能", (
            "artificial intelligence", "machine learning", "llm", "transformer", "人工智能",
        )),
        ("计算机系统", ("database", "distributed", "kernel", "systems", "数据库")),
        ("数学", ("mathematics", "theorem", "algebra", "数学")),
        ("生物医学", ("biology", "medicine", "genome", "生物", "医学")),
    )
    domains = [domain for domain, words in patterns if any(word in text for word in words)]
    kinds = []
    if any(word in text for word in ("paper", "arxiv", "论文")):
        kinds.append("论文聚合")
    if any(word in text for word in ("tutorial", "course", "课程", "教程")):
        kinds.append("课程")
    if any(word in text for word in ("blog", "engineering", "技术")):
        kinds.append("技术博客")
    if any(word in text for word in ("news", "新闻")):
        kinds.append("新闻")
    return {
        "domains": domains[:6],
        "content_kinds": kinds[:4],
        "summary": f"（fake）根据 {len(samples)} 条内容样本：{samples[0]['title'][:120]}",
        "confidence": "medium" if domains and len(samples) >= 3 else "low",
    }
