"""一次完整运行：L0 → L1 → L2 → 分级 → 写 digest。抓取由调用方（CLI）在此之前完成。"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from secondbrain import db
from secondbrain.llm.base import LLMBackend, LLMError, Usage
from secondbrain.llm.schemas import L1Response, L2Response
from secondbrain.llm.usage import estimate_cost, record_call
from secondbrain.models import FilterSettings
from secondbrain.pipeline import tiering
from secondbrain.pipeline.l0_rules import l0_reason
from secondbrain.pipeline.l1_screen import interleave_by_source, l1_messages, match_results
from secondbrain.pipeline.l2_deep import is_paper, l2_messages
from secondbrain.profile.feedback import few_shot_block, recent_feedback
from secondbrain.profile.loader import Profile, render_mastery

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"

# L1 中模型漏掉的条目最多再单独评估一次。
L1_ATTEMPTS = 2

# L2 费用预估：输入按每 3 个字符 1 token（偏保守），输出按每条 1500 token。
EST_CHARS_PER_TOKEN = 3
EST_L2_OUTPUT_TOKENS = 1500


@dataclass
class RunReport:
    l0_checked: int = 0
    l0_rejected: Counter = field(default_factory=Counter)
    l1_selected: int = 0
    l1_assessed: int = 0
    l1_failed: int = 0
    l2_selected: int = 0
    l2_assessed: int = 0
    l2_failed: int = 0
    lanes: Counter = field(default_factory=Counter)
    minutes_used: int = 0
    backlog_kept: int = 0
    backlog_demoted: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    aborted: str | None = None


def system_prompt(name: str, profile: Profile, extra: str = "") -> str:
    """profile + 规则在最前（同一次 run 内不变，利于缓存）；extra（如反馈样例）接在末尾。"""
    template = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    text = template.replace("{profile}", profile.text.strip()).replace(
        "{mastery}", render_mastery(profile)
    )
    return f"{text.rstrip()}\n\n{extra}\n" if extra else text


def _scores(data: dict) -> dict:
    """L2 结果带 scores 子对象；L1 结果的分数在顶层。"""
    return data.get("scores", data)


def run_pipeline(
    conn: sqlite3.Connection,
    fs: FilterSettings,
    backend: LLMBackend,
    profile: Profile,
    *,
    limit: int | None,
    now: datetime,
    today: date,
    clock: Callable[[], datetime] | None = None,
) -> RunReport:
    report = RunReport()
    clock = clock or (lambda: now)

    def record(stage: str, usage: Usage | None) -> None:
        if usage is None:
            return
        report.calls += 1
        report.cost_usd += record_call(
            conn, backend.name, stage, usage, clock(), fs.pricing, fs.pricing_schedule
        )

    # ---------- L0 ----------
    passed: list[sqlite3.Row] = []
    for row in db.items_by_status(conn, ("new",)):
        report.l0_checked += 1
        reason = l0_reason(row, fs.l0, now)
        if reason:
            report.l0_rejected[reason.split("：")[0]] += 1
            db.add_assessment(conn, row["id"], "L0", {"reason": reason}, now)
            db.set_status(conn, [row["id"]], "l0_rejected")
        else:
            passed.append(row)
    conn.commit()

    # ---------- L1 ----------
    selected = interleave_by_source(passed, limit)
    report.l1_selected = len(selected)
    pers = fs.personalize
    recent = recent_feedback(conn, now, pers.feedback_window_days, pers.summary_max_items)
    examples = few_shot_block(recent, pers.few_shot_per_polarity)
    l1_system = system_prompt("l1_system", profile, examples)
    batches = [
        selected[i : i + fs.l1.batch_size] for i in range(0, len(selected), fs.l1.batch_size)
    ]
    by_id = {row["id"]: row for row in selected}

    def screen(batch):
        """评估一批；模型漏掉的条目单独再评一次。返回 (结果, 各次调用的 usage, 错误)。"""
        results, usages, error = {}, [], None
        pending = batch
        for _ in range(L1_ATTEMPTS):
            try:
                response, usage = backend.complete_json(
                    l1_messages(l1_system, pending, fs.l1.item_chars), L1Response, "cheap"
                )
            except LLMError as exc:
                usages.append(exc.usage)
                error = str(exc)
                break
            usages.append(usage)
            results |= match_results(pending, response.items)
            pending = [row for row in pending if row["id"] not in results]
            if not pending:
                break
        return results, usages, error

    for batch, (results, usages, error) in zip(
        batches, _warm_then_parallel(screen, batches, fs.llm.concurrency), strict=True
    ):
        for usage in usages:
            record("L1", usage)
        if error:
            report.errors.append(f"L1：{error}")
        report.l1_failed += len(batch) - len(results)
        for item_id, a in results.items():
            row = by_id[item_id]
            score = tiering.rank_score(
                a.relevance, a.quality, a.novelty, a.refresher_value, row["trust"], fs.score
            )
            data = a.model_dump(exclude={"id"})
            model = backend.model_name("cheap")
            db.add_assessment(conn, item_id, "L1", data, now, model, score)
            db.set_status(conn, [item_id], "l1")
            report.l1_assessed += 1
    conn.commit()

    # ---------- L2 ----------
    l1_rows = db.items_by_status(conn, ("l1",))
    l1_data = db.latest_assessments(conn, "L1")
    l1_cands = [_candidate(row, l1_data[row["id"]], None, fs) for row in l1_rows]
    l2_ids = tiering.select_for_l2(l1_cands, fs)
    report.l2_selected = len(l2_ids)
    rows_by_id = {row["id"]: row for row in l1_rows}
    l2_system = system_prompt("l2_system", profile)
    l2_inputs = {i: l2_messages(l2_system, rows_by_id[i], fs.l2) for i in l2_ids}

    estimate = _estimate_l2_cost(l2_inputs, backend.model_name("strong"), fs, clock())
    if report.cost_usd + estimate > fs.run.max_cost_usd:
        report.aborted = (
            f"L2 预估费用 ${estimate:.3f}（加上已花费 ${report.cost_usd:.3f}）"
            f"超过上限 run.max_cost_usd=${fs.run.max_cost_usd}，未调用 L2、未分级"
        )
        conn.commit()
        return report

    def deep(item_id):
        try:
            return backend.complete_json(l2_inputs[item_id], L2Response, "strong"), None
        except LLMError as exc:
            return (None, exc.usage), str(exc)

    for item_id, ((response, usage), error) in zip(
        l2_ids, _warm_then_parallel(deep, l2_ids, fs.llm.concurrency), strict=True
    ):
        record("L2", usage)
        if error:
            report.l2_failed += 1
            report.errors.append(f"L2 #{item_id}：{error}")
            continue
        s = response.scores
        score = tiering.rank_score(
            s.relevance,
            s.quality,
            s.novelty,
            s.refresher_value,
            rows_by_id[item_id]["trust"],
            fs.score,
        )
        db.add_assessment(conn, item_id, "L2", response.model_dump(), now, usage.model, score)
        db.set_status(conn, [item_id], "l2")
        report.l2_assessed += 1
    conn.commit()

    # ---------- 分级 ----------
    day = today.isoformat()
    rows = db.items_by_status(conn, ("l1", "l2", "deferred"))
    l1_data = db.latest_assessments(conn, "L1")
    l2_data = db.latest_assessments(conn, "L2")
    deferred = db.deferred_since(conn)
    cands = [
        _candidate(row, l1_data[row["id"]], l2_data.get(row["id"]), fs, deferred.get(row["id"]))
        for row in rows
        if row["id"] in l1_data
    ]
    counts, minutes = db.day_usage(conn, day, tiering.TOP_LANES, {c.item_id for c in cands})
    placements = tiering.tier(cands, fs, today, counts, minutes)
    for p in placements:
        db.upsert_digest(conn, day, p, now)
        db.set_status(conn, [p.item_id], "deferred" if p.lane == "deferred" else "tiered")
        report.lanes[p.lane] += 1
    report.minutes_used = minutes + sum(
        p.est_minutes for p in placements if p.lane in tiering.TOP_LANES
    )

    kept, demoted = tiering.apply_backlog(db.past_must_reads(conn, day), today, fs.backlog_max_days)
    db.demote_must_reads(conn, demoted, day)
    report.backlog_kept, report.backlog_demoted = len(kept), len(demoted)
    conn.commit()
    return report


def _candidate(
    row: sqlite3.Row,
    l1: dict,
    l2: dict | None,
    fs: FilterSettings,
    deferred_from: str | None = None,
) -> tiering.Candidate:
    s = _scores(l2) if l2 else l1
    score = tiering.rank_score(
        s["relevance"], s["quality"], s["novelty"], s["refresher_value"], row["trust"], fs.score
    )
    est = (
        l2["est_minutes"]
        if l2
        else tiering.estimate_minutes(row["content_len"] or 0, is_paper(row), fs.estimate)
    )
    return tiering.Candidate(
        item_id=row["id"],
        score=score,
        relevance=s["relevance"],
        quality=s["quality"],
        novelty=s["novelty"],
        refresher_value=s["refresher_value"],
        content_type=l1["content_type"],
        role=row["role"],
        est_minutes=est,
        has_l2=l2 is not None,
        deferred_from=deferred_from,
    )


def _estimate_l2_cost(
    inputs: dict[int, list[dict]], model: str, fs: FilterSettings, ts: datetime
) -> float:
    total = 0.0
    for messages in inputs.values():
        chars = sum(len(m["content"]) for m in messages)
        usage = Usage(
            model=model,
            prompt_tokens=chars // EST_CHARS_PER_TOKEN,
            cache_miss_tokens=chars // EST_CHARS_PER_TOKEN,
            completion_tokens=EST_L2_OUTPUT_TOKENS,
        )
        total += estimate_cost(usage, fs.pricing, fs.pricing_schedule, ts)[0]
    return total


def _warm_then_parallel(fn, args: list, concurrency: int) -> list:
    """先单独执行第一个调用（让 system 前缀进入 DeepSeek 的上下文缓存），其余并发执行。
    返回值与 args 一一对应、顺序一致。"""
    if not args:
        return []
    first = fn(args[0])
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return [first, *pool.map(fn, args[1:])]
