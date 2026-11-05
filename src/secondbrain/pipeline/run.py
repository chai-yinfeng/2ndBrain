"""一次完整运行：L0 → L1 → L2 → 回补 → 分级 → 写 digest。抓取由调用方（CLI）在此之前完成。"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from secondbrain import db
from secondbrain.llm.base import LLMBackend, LLMError, Usage
from secondbrain.llm.schemas import REFRESHER_TYPES, L1Response, L2Response
from secondbrain.llm.usage import record_call
from secondbrain.models import FilterSettings
from secondbrain.pipeline import tiering
from secondbrain.pipeline.l0_rules import l0_reason
from secondbrain.pipeline.l1_screen import interleave_by_source, l1_messages, match_results
from secondbrain.pipeline.l2_deep import DeepResult, deep_read, estimate_deep_cost, is_paper
from secondbrain.profile.feedback import few_shot_block, recent_feedback, too_hard_prerequisites
from secondbrain.profile.loader import Profile, render_mastery, weak_prerequisites
from secondbrain.wiki.loop import known_knowledge, related_item
from secondbrain.wiki.review import due_concepts
from secondbrain.wiki.store import Page, Wiki

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"

# L1 中模型漏掉的条目最多再单独评估一次。
L1_ATTEMPTS = 2

# 没有字幕的视频不进入 L2
NO_TRANSCRIPT = "no_transcript"


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
    backfill_screened: int = 0
    backfill_picked: int = 0
    backfill_placed: int = 0
    concepts_placed: int = 0  # 放入温故栏的到期概念
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


class _Runner:
    """一次 run 中共享的状态：数据库、设置、后端、用量记录。"""

    def __init__(self, conn, fs, backend, profile, now, clock, report, wiki=None):
        self.conn, self.fs, self.backend, self.profile = conn, fs, backend, profile
        self.now, self.clock, self.report, self.wiki = now, clock, report, wiki
        self.hard_prereqs = ""  # 近期"太难"条目的前置知识（L1 user 消息，ADR 0013）

    def record(self, stage: str, usage: Usage | None) -> float:
        if usage is None:
            return 0.0
        cost = record_call(
            self.conn,
            self.backend.name,
            stage,
            usage,
            self.clock(),
            self.fs.pricing,
            self.fs.pricing_schedule,
        )
        self.report.calls += 1
        self.report.cost_usd += cost
        return cost

    def l0(self, rows: list[sqlite3.Row], ignore_age: bool = False) -> list[sqlite3.Row]:
        passed = []
        for row in rows:
            self.report.l0_checked += 1
            reason = l0_reason(row, self.fs.l0, self.now, ignore_age=ignore_age)
            if reason:
                self.report.l0_rejected[reason.split("：")[0]] += 1
                db.add_assessment(self.conn, row["id"], "L0", {"reason": reason}, self.now)
                db.set_status(self.conn, [row["id"]], "l0_rejected")
            else:
                passed.append(row)
        self.conn.commit()
        return passed

    def screen(self, rows: list[sqlite3.Row], system: str, status: str) -> tuple[int, int]:
        """L1 批量粗筛并写入评估；返回 (完成数, 失败数)。模型漏掉的条目单独再评一次。"""
        fs, backend = self.fs, self.backend
        batches = [rows[i : i + fs.l1.batch_size] for i in range(0, len(rows), fs.l1.batch_size)]
        by_id = {row["id"]: row for row in rows}

        def one(batch):
            results, usages, error = {}, [], None
            pending = batch
            for _ in range(L1_ATTEMPTS):
                known = known_knowledge(
                    self.wiki, pending, fs.l1.item_chars, fs.wiki.known_max_chars
                )
                known = "\n\n".join(part for part in (known, self.hard_prereqs) if part)
                messages = l1_messages(system, pending, fs.l1.item_chars, known)
                try:
                    response, usage = backend.complete_json(messages, L1Response, "cheap")
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

        done = failed = 0
        for batch, (results, usages, error) in zip(
            batches, _warm_then_parallel(one, batches, fs.llm.concurrency), strict=True
        ):
            for usage in usages:
                self.record("L1", usage)
            if error:
                self.report.errors.append(f"L1：{error}")
            failed += len(batch) - len(results)
            for item_id, a in results.items():
                row = by_id[item_id]
                score = tiering.rank_score(
                    a.relevance, a.quality, a.novelty, a.refresher_value, row["trust"], fs.score
                )
                data = a.model_dump(exclude={"id"})
                model = backend.model_name("cheap")
                db.add_assessment(self.conn, item_id, "L1", data, self.now, model, score)
                db.set_status(self.conn, [item_id], status)
                done += 1
        self.conn.commit()
        return done, failed

    def estimate(self, rows: list[sqlite3.Row], system: str) -> float:
        models = (self.backend.model_name("cheap"), self.backend.model_name("strong"))
        return sum(
            estimate_deep_cost(
                row,
                system,
                self.fs.l2,
                models,
                self.fs.pricing,
                self.fs.pricing_schedule,
                self.clock(),
            )
            for row in rows
        )

    def store_l2(self, row: sqlite3.Row, result: DeepResult) -> bool:
        for stage, usage in result.calls:
            self.record(stage, usage)
        if result.response is None:
            return False
        s = result.response.scores
        score = tiering.rank_score(
            s.relevance, s.quality, s.novelty, s.refresher_value, row["trust"], self.fs.score
        )
        model = self.backend.model_name("strong")
        data = result.response.model_dump()
        db.add_assessment(self.conn, row["id"], "L2", data, self.now, model, score)
        return True

    def deep_many(self, rows: list[sqlite3.Row], system: str) -> tuple[int, int]:
        """并发精读多条（第一条先单独执行以预热缓存）；返回 (完成数, 失败数)。"""

        def one(row):
            return deep_read(self.backend, system, row, self.fs.l2)

        done = failed = 0
        for row, result in zip(
            rows, _warm_then_parallel(one, rows, self.fs.llm.concurrency), strict=True
        ):
            if self.store_l2(row, result):
                db.set_status(self.conn, [row["id"]], "l2")
                done += 1
            else:
                failed += 1
                self.report.errors.append(f"L2 #{row['id']}：{result.error}")
        self.conn.commit()
        return done, failed


def backfill_priority(row: sqlite3.Row, l1: dict, profile: Profile) -> tuple:
    """回补池排序：explainer / survey / tutorial 优先，其次命中"接触过 / 想回炉"领域，
    再按 refresher_value 与排序分。"""
    topics = [t.replace("-", " ") for t in l1.get("topics", [])]
    return (
        l1.get("content_type") in REFRESHER_TYPES,
        bool(weak_prerequisites(profile, topics)),
        l1.get("refresher_value", 0),
        l1.get("_score") or 0,
    )


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
    wiki: Wiki | None = None,
    all_time: bool = False,
) -> RunReport:
    """wiki：用于 L1 的"已知知识"与到期概念复习（M6）；为 None 时跳过这两项。
    all_time：本次的新条目在 L0 不受时间窗限制（ADR 0016）。"""
    report = RunReport()
    run = _Runner(conn, fs, backend, profile, now, clock or (lambda: now), report, wiki)
    day = today.isoformat()

    # ---------- L0（常规条目；回补池单独处理） ----------
    passed = run.l0(db.items_by_status(conn, ("new",), backfill=False), ignore_age=all_time)

    # ---------- L1 ----------
    selected = interleave_by_source(passed, limit)
    report.l1_selected = len(selected)
    pers = fs.personalize
    recent = recent_feedback(conn, now, pers.feedback_window_days, pers.summary_max_items)
    examples = few_shot_block(recent, pers.few_shot_per_polarity)
    run.hard_prereqs = too_hard_prerequisites(
        conn, now, pers.feedback_window_days, pers.too_hard_prereq_max_chars
    )
    l1_system = system_prompt("l1_system", profile, examples)
    report.l1_assessed, report.l1_failed = run.screen(selected, l1_system, "l1")

    # ---------- L2 ----------
    l1_rows = db.items_by_status(conn, ("l1",), backfill=False)
    l1_data = db.latest_assessments(conn, "L1")
    readable = [row for row in l1_rows if row["extract_method"] != NO_TRANSCRIPT]
    l1_cands = [_candidate(row, l1_data[row["id"]], None, fs) for row in readable]
    l2_ids = tiering.select_for_l2(l1_cands, fs)
    report.l2_selected = len(l2_ids)
    rows_by_id = {row["id"]: row for row in readable}
    l2_rows = [rows_by_id[i] for i in l2_ids]
    l2_system = system_prompt("l2_system", profile)

    estimate = run.estimate(l2_rows, l2_system)
    if report.cost_usd + estimate > fs.run.max_cost_usd:
        report.aborted = (
            f"L2 预估费用 ${estimate:.3f}（加上已花费 ${report.cost_usd:.3f}）"
            f"超过上限 run.max_cost_usd=${fs.run.max_cost_usd}，未调用 L2、未分级"
        )
        conn.commit()
        return report
    report.l2_assessed, report.l2_failed = run.deep_many(l2_rows, l2_system)

    # ---------- 回补（ADR 0009） ----------
    _backfill(run, day, l1_system, l2_system)

    # ---------- 分级 ----------
    rows = db.items_by_status(conn, ("l1", "l2", "deferred"))
    l1_data = db.latest_assessments(conn, "L1")
    l2_data = db.latest_assessments(conn, "L2")
    deferred = db.deferred_since(conn)
    cands = [
        _candidate(row, l1_data[row["id"]], l2_data.get(row["id"]), fs, deferred.get(row["id"]))
        for row in rows
        if row["id"] in l1_data
    ]
    backfill_ids = {c.item_id for c in cands if c.is_backfill}
    counts, minutes = db.day_usage(conn, day, tiering.TOP_LANES, {c.item_id for c in cands})
    concept_rows = db.digest_concepts(conn, day)
    counts["refresher"] = counts.get("refresher", 0) + len(concept_rows)
    minutes += sum(r["est_minutes"] for r in concept_rows)
    concepts = _concept_candidates(run, today, {r["slug"] for r in concept_rows}, len(cands))
    placements = tiering.tier(cands + concepts.candidates(), fs, today, counts, minutes)
    for p in placements:
        if p.item_id in concepts:
            if p.lane == "refresher":
                page, related = concepts[p.item_id].page, concepts[p.item_id].related
                mastery = page.meta.get("mastery")
                db.upsert_digest_concept(
                    conn, day, page.slug, page.title, mastery, p.est_minutes, related, now
                )
                report.lanes["refresher"] += 1
                report.concepts_placed += 1
            continue
        if p.item_id in backfill_ids and p.lane not in ("refresher", "deferred"):
            # 回补条目今天没排进温故栏：留在回补池（已有 L2，下次不再花费）
            db.set_status(conn, [p.item_id], "backfill_l1")
            continue
        db.upsert_digest(conn, day, p, now)
        db.set_status(conn, [p.item_id], "deferred" if p.lane == "deferred" else "tiered")
        report.lanes[p.lane] += 1
        if p.item_id in backfill_ids:
            report.backfill_placed += 1
    report.minutes_used = minutes + sum(
        p.budget
        for p in placements
        if p.lane in tiering.TOP_LANES
        and not (p.item_id in backfill_ids and p.lane != "refresher")
        and (p.item_id not in concepts or p.lane == "refresher")
    )

    kept, demoted = tiering.apply_backlog(db.past_must_reads(conn, day), today, fs.backlog_max_days)
    db.demote_must_reads(conn, demoted, day)
    report.backlog_kept, report.backlog_demoted = len(kept), len(demoted)
    conn.commit()
    return report


@dataclass(frozen=True)
class _ConceptCandidate:
    page: Page
    related: int | None


class _Concepts(dict):
    """占位 item_id（负数）→ _ConceptCandidate；迭代时产出 tiering.Candidate。"""

    def __init__(self, fs: FilterSettings):
        super().__init__()
        self.fs = fs

    def candidates(self) -> list[tiering.Candidate]:
        return [
            tiering.Candidate(
                item_id=item_id,
                score=100.0,  # 到期复习优先于普通温故条目
                relevance=10,
                quality=10,
                novelty=0,
                refresher_value=10,
                content_type="explainer",
                role="core",
                est_minutes=self.fs.wiki.review_minutes,
                has_l2=True,
                is_concept=True,
            )
            for item_id in self
        ]


def _concept_candidates(run: _Runner, today: date, already: set[str], offset: int) -> _Concepts:
    """到期概念（最多 wiki.review_slots 个，当天已排过的不再排），附上近期讲到它的条目。"""
    concepts = _Concepts(run.fs)
    if run.wiki is None or run.fs.wiki.review_slots <= 0:
        return concepts
    due = [p for p in due_concepts(run.wiki, today) if p.slug not in already]
    if not due:
        return concepts
    since = db.format_time(run.now - timedelta(days=run.fs.wiki.recent_days))
    recent = [
        r
        for r in db.items_by_status(run.conn, ("l1", "l2", "tiered", "deferred"))
        if r["fetched_at"] >= since
    ]
    l1, l2 = db.latest_assessments(run.conn, "L1"), db.latest_assessments(run.conn, "L2")
    for n, page in enumerate(due[: run.fs.wiki.review_slots], start=1):
        concepts[-(offset + n)] = _ConceptCandidate(page, related_item(page, recent, l1, l2))
    return concepts


def _backfill(run: _Runner, day: str, l1_system: str, l2_system: str) -> None:
    """从回补池补足当天的回补名额：必要时先 L1 粗筛一批，再挑优先级最高的做 L2。"""
    conn, fs, report = run.conn, run.fs, run.report
    needed = fs.backfill.daily_limit - db.backfill_placed(conn, day)
    if needed <= 0:
        return
    screened = db.items_by_status(conn, ("backfill_l1",), backfill=True)
    if len(screened) < fs.backfill.screen_size:
        fresh = run.l0(db.items_by_status(conn, ("new",), backfill=True), ignore_age=True)
        batch = interleave_by_source(fresh, fs.backfill.screen_size - len(screened))
        done, _ = run.screen(batch, l1_system, "backfill_l1")
        report.backfill_screened = done
        screened = db.items_by_status(conn, ("backfill_l1",), backfill=True)
    l1_data = db.latest_assessments(conn, "L1")
    l2_data = db.latest_assessments(conn, "L2")
    pool = [r for r in screened if r["id"] in l1_data and r["extract_method"] != NO_TRANSCRIPT]
    pool.sort(key=lambda r: backfill_priority(r, l1_data[r["id"]], run.profile), reverse=True)
    picks = pool[:needed]
    report.backfill_picked = len(picks)
    if not picks:
        return
    to_read = [r for r in picks if r["id"] not in l2_data]
    estimate = run.estimate(to_read, l2_system)
    if report.cost_usd + estimate > fs.run.max_cost_usd:
        report.errors.append(f"回补：预估费用 ${estimate:.3f} 将超过上限，今天跳过")
        report.backfill_picked = 0
        return
    run.deep_many(to_read, l2_system)
    db.set_status(conn, [r["id"] for r in picks if r["id"] in l2_data], "l2")
    conn.commit()


@dataclass
class DeepOutcome:
    response: L2Response | None
    estimate: float = 0.0
    cost: float = 0.0
    error: str | None = None


def deep_one(
    conn: sqlite3.Connection,
    fs: FilterSettings,
    backend: LLMBackend,
    profile: Profile,
    row: sqlite3.Row,
    now: datetime,
) -> DeepOutcome:
    """按需精读单条（sb deep / Web "精读"按钮）。不改变条目在 digest 中的位置。"""
    if row["extract_method"] == NO_TRANSCRIPT:
        return DeepOutcome(None, error="该视频没有字幕，无法精读")
    report = RunReport()
    run = _Runner(conn, fs, backend, profile, now, lambda: now, report)
    system = system_prompt("l2_system", profile)
    estimate = run.estimate([row], system)
    if estimate > fs.run.max_cost_usd:
        return DeepOutcome(
            None, estimate, error=f"预估费用 ${estimate:.3f} 超过上限 ${fs.run.max_cost_usd}"
        )
    result = deep_read(backend, system, row, fs.l2, concurrency=fs.llm.concurrency)
    ok = run.store_l2(row, result)
    conn.commit()
    return DeepOutcome(result.response if ok else None, estimate, report.cost_usd, result.error)


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
        is_backfill=bool(row["is_backfill"]),
    )


def _warm_then_parallel(fn, args: list, concurrency: int) -> list:
    """先单独执行第一个调用（让 system 前缀进入 DeepSeek 的上下文缓存），其余并发执行。
    返回值与 args 一一对应、顺序一致。"""
    if not args:
        return []
    first = fn(args[0])
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return [first, *pool.map(fn, args[1:])]
