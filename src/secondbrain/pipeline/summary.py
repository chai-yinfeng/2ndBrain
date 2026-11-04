"""深度速读与补前置（M8，ADR 0015）：按需调用，结果缓存在 item_ai 表。

深度速读与 L2 的区别：L2 用于分级（论文只读 abstract / introduction / conclusion，输出 TL;DR、
要点、估时与评分）；深度速读读全文（长内容 cheap 分块提要 → strong 汇总），输出问题与动机、
核心方法、关键结果、局限、与已知知识的关系、值得精读的章节和前置知识，不影响分级。
"""

from __future__ import annotations

import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from secondbrain.llm.base import LLMBackend, LLMError, Usage
from secondbrain.llm.schemas import (
    ChunkNotes,
    DeepSummary,
    PrereqExplanations,
)
from secondbrain.llm.usage import full_price
from secondbrain.models import FilterSettings
from secondbrain.pipeline.l2_deep import (
    EST_CHARS_PER_TOKEN,
    EST_MAP_OUTPUT_TOKENS,
    PROMPTS_DIR,
    DeepPlan,
    _item_payload,
    chunk_text,
    map_messages,
    notes_text,
)

EST_SUMMARY_OUTPUT_TOKENS = 2500
EST_PREREQ_OUTPUT_TOKENS_EACH = 700  # 每个知识点约 300–500 字
SYSTEM_EXTRA_CHARS = 3000  # system prompt（含 profile）的大致长度
STAGE_MAP, STAGE_REDUCE, STAGE_PREREQ = "SUMMARY_MAP", "SUMMARY", "PREREQ"


def plan_summary(row: sqlite3.Row, fs: FilterSettings) -> DeepPlan:
    """读全文（论文也不截取章节）：不超过 single_max_chars 直接交给 strong，否则分块 map-reduce。"""
    s = fs.summary
    content = (row["content"] or row["summary"] or "")[: s.max_total_chars]
    full = len(row["content"] or row["summary"] or "")
    kind = "full" if full <= s.max_total_chars else "truncated"
    if len(content) <= s.single_max_chars:
        return DeepPlan("single", kind, text=content)
    chunks = chunk_text(content, fs.l2.chunk_chars, fs.l2.chunk_overlap_chars)[: s.max_chunks]
    return DeepPlan("mapreduce", "chunk_notes", chunks=chunks)


def summary_messages(
    system: str, row: sqlite3.Row, text: str, kind: str, known: str = ""
) -> list[dict[str, str]]:
    user = "请为以下条目写深度速读，按规定格式输出 json：\n" + json.dumps(
        {"item": _item_payload(row, text, kind)}, ensure_ascii=False
    )
    if known:
        user = f"{known}\n\n{user}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


@dataclass
class SummaryResult:
    response: DeepSummary | None
    calls: list[tuple[str, Usage | None]] = field(default_factory=list)  # (stage, usage)
    error: str | None = None


def deep_summary(
    backend: LLMBackend,
    system: str,
    row: sqlite3.Row,
    fs: FilterSettings,
    known: str = "",
    progress=lambda line: None,
) -> SummaryResult:
    """长内容：cheap 逐块提要（SUMMARY_MAP，第一块先单独跑以预热缓存）→ strong 汇总（SUMMARY）。"""
    plan = plan_summary(row, fs)
    result = SummaryResult(None)
    text, kind = plan.text, plan.text_kind
    if plan.mode == "mapreduce":
        total = len(plan.chunks)
        progress(f"全文 {len(row['content'] or '')} 字符，分 {total} 块提要（cheap 模型）")

        def summarize(index: int):
            try:
                messages = map_messages(row, plan.chunks[index], index + 1, total)
                return backend.complete_json(messages, ChunkNotes, "cheap"), None
            except LLMError as exc:
                return (None, exc.usage), f"第 {index + 1}/{total} 段提要失败：{exc}"

        outputs = [summarize(0)]
        with ThreadPoolExecutor(max_workers=max(1, fs.llm.concurrency)) as pool:
            outputs += list(pool.map(summarize, range(1, total)))
        notes = []
        for (n, usage), error in outputs:
            result.calls.append((STAGE_MAP, usage))
            if error and result.error is None:
                result.error = error
            if n is not None:
                notes.append(n)
        if result.error:
            return result
        text = notes_text(notes)
    progress("汇总（strong 模型）")
    try:
        response, usage = backend.complete_json(
            summary_messages(system, row, text, kind, known), DeepSummary, "strong"
        )
    except LLMError as exc:
        result.calls.append((STAGE_REDUCE, exc.usage))
        result.error = str(exc)
        return result
    result.calls.append((STAGE_REDUCE, usage))
    result.response = response
    return result


# ---------- 补前置 ----------


def prereq_messages(row: sqlite3.Row, needs: list[dict[str, str]]) -> list[dict[str, str]]:
    """needs：[{"name": ..., "why": ...}]（why 可为空）。"""
    system = (PROMPTS_DIR / "prereq_system.md").read_text(encoding="utf-8")
    payload = {"item": {"title": row["title"], "source": row["source_id"]}, "prerequisites": needs}
    user = "请为以下前置知识写简短讲解，按规定格式输出 json：\n" + json.dumps(
        payload, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def explain_prereqs(
    backend: LLMBackend, row: sqlite3.Row, needs: list[dict[str, str]]
) -> tuple[dict[str, str], Usage]:
    """一次 strong 调用批量生成讲解；返回 {name: 讲解}（只保留输入中的名字）。"""
    response, usage = backend.complete_json(
        prereq_messages(row, needs), PrereqExplanations, "strong"
    )
    wanted = {n["name"] for n in needs}
    return {e.name: e.explanation for e in response.items if e.name in wanted}, usage


# ---------- 费用预估（高峰全价；非高峰再乘 off_peak_multiplier） ----------


def _price(model: str, tokens_in: int, tokens_out: int, fs: FilterSettings) -> float:
    return full_price(Usage(model, tokens_in, 0, tokens_in, tokens_out), fs.pricing)


def estimate_summary_cost(content_chars: int, fs: FilterSettings) -> float:
    """按正文长度预估深度速读的高峰价（输入每 3 字符 1 token，偏保守）。"""
    cheap, strong = fs.llm.models["cheap"], fs.llm.models["strong"]
    chars = min(content_chars, fs.summary.max_total_chars)
    if chars <= fs.summary.single_max_chars:
        tokens_in = (chars + SYSTEM_EXTRA_CHARS) // EST_CHARS_PER_TOKEN
        return _price(strong, tokens_in, EST_SUMMARY_OUTPUT_TOKENS, fs)
    chunks = min(math.ceil(chars / fs.l2.chunk_chars), fs.summary.max_chunks)
    per_chunk = (fs.l2.chunk_chars + 800) // EST_CHARS_PER_TOKEN
    total = chunks * _price(cheap, per_chunk, EST_MAP_OUTPUT_TOKENS, fs)
    notes_tokens = chunks * EST_MAP_OUTPUT_TOKENS + SYSTEM_EXTRA_CHARS // EST_CHARS_PER_TOKEN
    return total + _price(strong, notes_tokens, EST_SUMMARY_OUTPUT_TOKENS, fs)


def summary_minutes(content_chars: int, fs: FilterSettings) -> int:
    """大致耗时（分钟）：单次约 1 分钟；分块时每轮并发约 0.5 分钟，另加汇总 1 分钟。"""
    chars = min(content_chars, fs.summary.max_total_chars)
    if chars <= fs.summary.single_max_chars:
        return 1
    chunks = min(math.ceil(chars / fs.l2.chunk_chars), fs.summary.max_chunks)
    return 1 + math.ceil(chunks / max(1, fs.llm.concurrency) * 0.5)


def estimate_prereq_cost(count: int, fs: FilterSettings) -> float:
    tokens_in = (1500 + 200 * count) // EST_CHARS_PER_TOKEN
    return _price(fs.llm.models["strong"], tokens_in, EST_PREREQ_OUTPUT_TOKENS_EACH * count, fs)
