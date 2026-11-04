"""L2 精读：strong 模型逐条阅读截断后的正文。"""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from secondbrain.llm.base import LLMBackend, LLMError, Usage
from secondbrain.llm.schemas import ChunkNotes, L2Response
from secondbrain.llm.usage import estimate_cost
from secondbrain.models import L2Settings, ModelPrice, PricingSchedule

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"

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


def _paper_text(content: str, summary: str | None, s: L2Settings) -> str | None:
    """论文的 abstract + introduction + conclusion（各节限长）；找不到章节返回 None。"""
    found = paper_sections(content)
    if "introduction" not in found and "conclusion" not in found:
        return None
    parts = []
    abstract = found.get("abstract") or summary
    if abstract:
        parts.append("[Abstract]\n" + abstract[: s.section_max_chars])
    for name in ("introduction", "conclusion"):
        if name in found:
            parts.append(f"[{name.title()}]\n" + found[name][: s.section_max_chars])
    return "\n\n[...]\n\n".join(parts)


def build_l2_text(content: str, summary: str | None, paper: bool, s: L2Settings) -> str:
    """论文优先 abstract + introduction + conclusion；找不到章节时取正文开头。"""
    if paper:
        sections = _paper_text(content, summary, s)
        if sections is not None:
            return sections[: s.max_input_chars]
    return content[: s.max_input_chars]


def is_video(row: sqlite3.Row) -> bool:
    return (row["extract_method"] or "").startswith("transcript")


@dataclass
class DeepPlan:
    mode: str  # single | mapreduce
    text_kind: str  # full | truncated | sections | chunk_notes
    text: str = ""  # single 模式的输入
    chunks: list[str] = field(default_factory=list)  # mapreduce 模式的分块


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """按行切块，每块不超过 size 字符（单行超长时硬切）；相邻块重叠约 overlap 字符的整行。"""
    lines: list[str] = []
    for line in text.splitlines():
        while len(line) > size:
            lines.append(line[:size])
            line = line[size:]
        lines.append(line)
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if current and length + len(line) + 1 > size:
            chunks.append("\n".join(current))
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(current):
                if tail_len + len(prev) + 1 > overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
            current, length = tail, tail_len
        current.append(line)
        length += len(line) + 1
    if current and "".join(current).strip():
        chunks.append("\n".join(current))
    return chunks


def plan_deep(row: sqlite3.Row, s: L2Settings) -> DeepPlan:
    """待读文本超过 mapreduce_threshold_chars 时分块 map-reduce，否则单次精读。"""
    content = row["content"] or row["summary"] or ""
    text, kind = content, "full"
    if is_paper(row):
        sections = _paper_text(content, row["summary"], s)
        if sections is not None:
            text, kind = sections, "sections"
    if len(text) > s.mapreduce_threshold_chars:
        chunks = chunk_text(text, s.chunk_chars, s.chunk_overlap_chars)[: s.max_chunks]
        return DeepPlan("mapreduce", "chunk_notes", chunks=chunks)
    if len(text) > s.max_input_chars:
        return DeepPlan("single", "truncated", text=text[: s.max_input_chars])
    return DeepPlan("single", kind, text=text)


def _item_payload(row: sqlite3.Row, text: str, text_kind: str) -> dict:
    content = row["content"] or row["summary"] or ""
    payload = {
        "id": row["id"],
        "title": row["title"],
        "source": row["source_id"],
        "url": row["url"],
        "is_paper": is_paper(row),
        "is_video": is_video(row),
        "full_length_chars": len(content),
        "text_kind": text_kind,
        "text": text,
    }
    if row["duration_seconds"]:
        payload["duration_minutes"] = round(row["duration_seconds"] / 60)
    return payload


def l2_messages(
    system: str,
    row: sqlite3.Row,
    s: L2Settings,
    plan: DeepPlan | None = None,
    text: str | None = None,
) -> list[dict[str, str]]:
    """单次精读的消息。map-reduce 的汇总阶段传入 text（逐段提要）。"""
    plan = plan or plan_deep(row, s)
    if text is None:
        text, kind = plan.text, plan.text_kind
    else:
        kind = "chunk_notes"
    user = "请精读以下条目，按规定格式输出 json：\n" + json.dumps(
        {"item": _item_payload(row, text, kind)}, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def map_messages(row: sqlite3.Row, chunk: str, index: int, total: int) -> list[dict[str, str]]:
    system = (PROMPTS_DIR / "l2_map_system.md").read_text(encoding="utf-8")
    payload = {
        "title": row["title"],
        "source": row["source_id"],
        "is_video": is_video(row),
        "part": f"{index}/{total}",
        "text": chunk,
    }
    user = "请为这一段做提要，按规定格式输出 json：\n" + json.dumps(payload, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def notes_text(notes: list[ChunkNotes]) -> str:
    """把各段提要拼成汇总阶段的输入；保留时间戳。"""
    blocks = []
    for i, n in enumerate(notes, start=1):
        lines = [f"[第 {i}/{len(notes)} 段] {n.summary}"]
        lines += [f"- [{p.t}] {p.text}" if p.t else f"- {p.text}" for p in n.points]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@dataclass
class DeepResult:
    response: L2Response | None
    calls: list[tuple[str, Usage | None]] = field(default_factory=list)  # (stage, usage)
    error: str | None = None


def deep_read(
    backend: LLMBackend, system: str, row: sqlite3.Row, s: L2Settings, concurrency: int = 1
) -> DeepResult:
    """精读一条。长内容：cheap 模型逐块提要（stage L2_MAP）→ strong 模型汇总（stage L2）。

    concurrency > 1 时，第一块先单独提要（预热 map prompt 的缓存），其余块并发。"""
    plan = plan_deep(row, s)
    result = DeepResult(None)
    text = None
    if plan.mode == "mapreduce":
        total = len(plan.chunks)

        def summarize(index: int):
            try:
                messages = map_messages(row, plan.chunks[index], index + 1, total)
                return backend.complete_json(messages, ChunkNotes, "cheap"), None
            except LLMError as exc:
                return (None, exc.usage), f"第 {index + 1}/{total} 段提要失败：{exc}"

        outputs = [summarize(0)]
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            outputs += list(pool.map(summarize, range(1, total)))
        notes = []
        for (n, usage), error in outputs:
            result.calls.append(("L2_MAP", usage))
            if error and result.error is None:
                result.error = error
            if n is not None:
                notes.append(n)
        if result.error:
            return result
        text = notes_text(notes)
    try:
        response, usage = backend.complete_json(
            l2_messages(system, row, s, plan, text), L2Response, "strong"
        )
    except LLMError as exc:
        result.calls.append(("L2", exc.usage))
        result.error = str(exc)
        return result
    result.calls.append(("L2", usage))
    result.response = response
    return result


# 费用预估：输入按每 3 个字符 1 token（偏保守）
EST_CHARS_PER_TOKEN = 3
# 输出 token 按实测（M2 / M5 真实运行）取偏高值
EST_L2_OUTPUT_TOKENS = 3000
EST_MAP_OUTPUT_TOKENS = 1500


def estimate_deep_cost(
    row: sqlite3.Row,
    system: str,
    s: L2Settings,
    models: tuple[str, str],
    pricing: dict[str, ModelPrice],
    schedule: PricingSchedule,
    ts: datetime,
) -> float:
    """models = (cheap, strong)。"""
    cheap, strong = models
    plan = plan_deep(row, s)

    def cost(model: str, chars: int, out: int) -> float:
        tokens = chars // EST_CHARS_PER_TOKEN
        usage = Usage(model, tokens, 0, tokens, out)
        return estimate_cost(usage, pricing, schedule, ts)[0]

    if plan.mode == "single":
        return cost(strong, len(system) + len(plan.text) + 500, EST_L2_OUTPUT_TOKENS)
    total = sum(cost(cheap, len(c) + 800, EST_MAP_OUTPUT_TOKENS) for c in plan.chunks)
    notes_chars = len(plan.chunks) * EST_MAP_OUTPUT_TOKENS * EST_CHARS_PER_TOKEN
    return total + cost(strong, len(system) + notes_chars + 500, EST_L2_OUTPUT_TOKENS)
