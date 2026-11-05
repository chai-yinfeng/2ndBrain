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
        payload = _payload(messages)
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
        elif schema is WikiPlan:
            obj = _wiki_plan(payload)
        elif schema is LintFindings:
            obj = {"issues": []}
        elif schema is DeepSummary:
            known = "已知知识" in messages[-1]["content"]
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


def _match_row(table: list[dict], fb: dict) -> dict | None:
    """反馈条目的 topics / 标题词出现在某个领域的名称或说明里时，认为对应该领域。"""
    terms = [t.lower() for t in fb.get("topics") or []]
    terms += [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", fb.get("title", ""))]
    for row in table:
        text = f"{row['domain']} {row.get('note', '')}".lower()
        if any(t in text for t in terms):
            return row
    return None


_LEVELS = ("陌生", "接触过", "想回炉", "扎实")


def _shift_level(raw: str, delta: int) -> str | None:
    """raw 掌握度（可能带括号说明）升 / 降一级；无法识别或已到端点时返回 None。"""
    current = next((lv for lv in _LEVELS if raw.startswith(lv)), None)
    if current is None:
        return None
    rank = _LEVELS.index(current) + delta
    return _LEVELS[rank] if 0 <= rank < len(_LEVELS) else None


def _profile_proposal(payload: dict) -> dict:
    """确定性的 profile 提案（ADR 0013 的证据规则）：
    - 太浅 → 对应领域升一级（无对应领域则新增"接触过"行）；
    - 太难 → 对应领域降一级（无对应领域则新增"陌生"行）；
    - 不感兴趣 / 没价值 → 在含"不想看"的段落末尾追加一行。"""
    table = payload.get("table", [])
    sections = [s["section"] for s in payload.get("sections", [])]
    dislike = next((s for s in sections if "不想看" in s), None)
    changes: dict[str, dict] = {}
    edits = []
    labels = {"too_easy": "太浅", "too_hard": "太难"}
    for fb in payload.get("feedback", []):
        kind, fid = fb["kind"], fb["id"]
        if kind in labels:
            row = _match_row(table, fb)
            if row is not None:
                target = _shift_level(row["mastery"], 1 if kind == "too_easy" else -1)
                if target is None:
                    continue
                key = f"row-{row['row']}"
                change = {"action": "update", "row": row["row"], "mastery": target}
            else:
                domain = (fb.get("topics") or [fb["title"][:40]])[0]
                key = f"add-{domain}"
                target = "接触过" if kind == "too_easy" else "陌生"
                change = {"action": "add", "domain": domain, "mastery": target, "priority": "中"}
            entry = changes.setdefault(key, {**change, "reason": "（fake）", "evidence": []})
            entry["evidence"].append(fid)
            entry["reason"] = f"（fake）{len(entry['evidence'])} 次标记{labels[kind]}"
        elif kind in ("dismiss", "worthless") and dislike:
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
        "summary": f"（fake）{len(changes)} 处掌握度修改，{len(edits)} 处段落修改",
        "mastery_changes": list(changes.values()),
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
        + ("与已知知识中的页面相关。" if known else "与用户 wiki 中的已知知识没有直接关系。"),
        "sections_to_read": [{"name": "（fake）全文第 1 部分", "reason": "（fake）核心方法在这里"}],
        "prerequisites": [{"name": t, "why": f"（fake）正文用到 {t}"} for t in topics[:2]],
    }


def _chunk_notes(payload: dict) -> dict:
    """逐段提要：视频取前几段带时间戳的字幕，其余取前几行。"""
    points = []
    for line in payload.get("text", "").splitlines()[:4]:
        m = re.match(r"\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*(.*)", line)
        if m:
            points.append({"t": m.group(1), "text": m.group(2)[:100]})
        elif line.strip():
            points.append({"t": None, "text": line.strip()[:100]})
    return {"summary": f"（fake）第 {payload.get('part', '?')} 段", "points": points}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "item"


def _wiki_plan(payload: dict) -> dict:
    """确定性的 wiki 编辑：为条目建 paper 页；前置知识若已有页面则加链接，否则新建概念页。"""
    item = payload["item"]
    paper = _slug(item["title"])
    index = payload.get("index", "")
    existing = {p["slug"] for p in payload.get("related_pages", [])}
    concepts = [_slug(c) for c in (item.get("prerequisites") or [])[:3]]
    concepts = [c for c in dict.fromkeys(concepts) if c != paper]
    points = "\n".join(f"- {kp}" for kp in item.get("key_points") or []) or f"- {item['title']}"
    related = "\n".join(f"- [[{c}]]" for c in concepts)
    creates = [
        {
            "slug": paper,
            "type": "paper",
            "title": item["title"],
            "summary": "（fake）" + (item.get("tldr") or item["title"])[:60],
            "body": f"## 要点\n\n{points}" + (f"\n\n## 相关\n\n{related}" if related else ""),
        }
    ]
    updates = []
    for c, name in zip(concepts, item.get("prerequisites") or [], strict=False):
        if c in existing or f"[[{c}]]" in index:
            updates.append({"slug": c, "action": "add_links", "links": [paper]})
        else:
            creates.append(
                {
                    "slug": c,
                    "type": "concept",
                    "title": name,
                    "summary": f"（fake）{name}",
                    "body": f"## 是什么\n\n（fake）{name}。\n\n## 相关\n\n- [[{paper}]]",
                }
            )
    return {
        "summary": f"（fake）新建 {len(creates)} 页，更新 {len(updates)} 页",
        "creates": creates,
        "updates": updates,
    }
