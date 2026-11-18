"""CLI 与 Web 共用的运维流程：完整运行（带进程间锁）、编入 wiki、应用信源 trust 建议（ADR 0013）。

每个流程通过 echo 回调输出进度文字：CLI 传 typer.echo，Web 后台任务传任务日志。
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from secondbrain import config, db, ingest
from secondbrain.llm.base import LLMError, Usage, make_backend
from secondbrain.llm.usage import record_call
from secondbrain.net import HttpClient
from secondbrain.pipeline.run import RunReport, run_pipeline
from secondbrain.profile.loader import load_profile
from secondbrain.wiki.store import open_wiki

Echo = Callable[[str], None]
OK, FAIL, WARN = "✓", "✗", "!"
RUN_LANES = {
    "must_read": "必读",
    "worth_it": "值得读",
    "refresher": "温故",
    "serendipity": "意外",
    "deferred": "顺延的深读",
    "fyi": "速览",
    "archived": "归档",
}


NO_SOURCES = "还没有启用的信源，去 /sources 添加（信源目录里可以逐个预览、添加）"


def active_sources(sources: list) -> list:
    """会被抓取的信源：启用且不是 needs_scraper。"""
    return [s for s in sources if s.enabled and not s.needs_scraper]


class OpsError(Exception):
    """配置、后端或锁的问题；消息可直接展示给用户。"""


def _require_active(conn, item_id: int) -> None:
    if db.is_trashed(conn, item_id):
        raise OpsError(f"材料 #{item_id} 已回收，请先恢复后再处理")


class RunLocked(OpsError):
    """已有一次 sb run（CLI、定时任务或 Web）在运行。"""


# ---------- 进程间锁 ----------


def lock_path() -> Path:
    return config.data_dir() / "run.lock"


@contextmanager
def run_lock(path: Path | None = None) -> Iterator[None]:
    """非阻塞的排他锁（fcntl.flock）。进程退出或崩溃时内核自动释放，不会留下死锁。"""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RunLocked(f"已有 sb run 在运行（锁 {path}），本次不执行") from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def is_running(path: Path | None = None) -> bool:
    try:
        with run_lock(path):
            return False
    except RunLocked:
        return True


# ---------- 完整运行（sb run） ----------


def _settings():
    try:
        settings = config.load_settings()
        return settings, config.pipeline_settings(settings), config.fetch_settings(settings)
    except (config.ConfigError, OSError, ValueError) as exc:
        raise OpsError(str(exc)) from exc


def _backend(settings, fs):
    name = config.llm_backend(settings)
    try:
        return name, make_backend(name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
    except LLMError as exc:
        raise OpsError(str(exc)) from exc


def run_once(
    echo: Echo, *, limit: int | None = None, no_fetch: bool = False, all_time: bool = False
) -> RunReport:
    """抓取 → L0 → L1 → L2 → 分级，写入当天 digest。持有 run 锁期间执行。
    all_time：抓取不按 max_age_days 截断、L0 不排除超出时间窗的新条目（ADR 0016）。"""
    config.load_env()
    settings, fs, fetch_fs = _settings()
    backend_name, backend = _backend(settings, fs)
    with run_lock():
        profile_file, created = config.ensure_profile()
        if created:
            echo(f"{WARN} 已从 example 生成 {profile_file}，请按自己的情况修改")
        profile = load_profile(profile_file)
        for warning in profile.warnings:
            echo(f"{WARN} profile：{warning}")
        echo(f"LLM 后端：{backend_name}；profile 解析到 {len(profile.domains)} 个领域")
        if all_time:
            echo("不限时间窗：只去重，信源与 L0 都不按时间窗排除")
        try:
            sources = config.load_sources()
        except config.ConfigError as exc:
            raise OpsError(str(exc)) from exc
        if not active_sources(sources):
            echo(NO_SOURCES)  # 空信源：友好提示，不抓取、不调用 LLM（ADR 0018）
            return RunReport()

        conn = db.connect()
        try:
            if not no_fetch:
                with HttpClient(fetch_fs) as client:
                    results = ingest.fetch_sources(
                        conn, sources, client, fetch_fs, limit=fs.run.fetch_limit, all_time=all_time
                    )
                added = sum(r.added for r in results)
                failed = [r for r in results if r.status == "failed"]
                echo(f"抓取：新增 {added} 条，失败 {len(failed)} 个信源")
                for r in failed:
                    echo(f"  {FAIL} {r.source_id}：{r.message}")
            now = datetime.now(UTC)
            report = run_pipeline(
                conn,
                fs,
                backend,
                profile,
                limit=limit,
                now=now,
                today=datetime.now().date(),
                clock=lambda: datetime.now(UTC),
                wiki=open_wiki(config.wiki_dir(), config.raw_dir()),
                all_time=all_time,
            )
        finally:
            conn.close()

    r = report
    rejected = "，".join(f"{k} {v}" for k, v in r.l0_rejected.most_common()) or "无"
    echo(f"L0：检查 {r.l0_checked} 条，排除 {sum(r.l0_rejected.values())} 条（{rejected}）")
    echo(f"L1：送入 {r.l1_selected} 条，完成 {r.l1_assessed} 条，失败 {r.l1_failed} 条")
    echo(f"L2：选中 {r.l2_selected} 条，完成 {r.l2_assessed} 条，失败 {r.l2_failed} 条")
    if r.concepts_placed:
        echo(f"wiki：到期概念 {r.concepts_placed} 个放入温故栏")
    if r.backfill_screened or r.backfill_picked:
        echo(
            f"回补：粗筛 {r.backfill_screened} 条，选中 {r.backfill_picked} 条，"
            f"放入温故 {r.backfill_placed} 条"
        )
    for error in r.errors:
        echo(f"  {FAIL} {error}")
    echo(f"本次 LLM 调用 {r.calls} 次，估算费用 ${r.cost_usd:.4f}")
    if r.aborted:
        echo(f"{FAIL} {r.aborted}")
        return r
    lanes = "，".join(f"{label} {r.lanes[k]}" for k, label in RUN_LANES.items() if r.lanes[k])
    echo(f"分级：{lanes or '无新条目'}")
    echo(
        f"今日已排 {r.minutes_used} / {fs.daily_budget_minutes} 分钟；"
        f"积压必读 {r.backlog_kept} 条，降级 {r.backlog_demoted} 条"
    )
    return r


# ---------- 编入 wiki ----------


@dataclass
class IngestSummary:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    cost: float = 0.0
    failures: int = 0
    pages: int = 0  # wiki 现有页面数
    errors: list[str] = field(default_factory=list)
    cached: list[int] = field(default_factory=list)


def ingest_items(ids: list[int], echo: Echo, *, force: bool = False) -> IngestSummary:
    """增量编入 wiki；相同正文与个人笔记直接复用，付费调用仍服从单次费用上限。"""
    from secondbrain.wiki.ingest import ingest_item, material_input_hash, promote_material

    summary = IngestSummary()
    config.load_env()
    settings, fs, _ = _settings()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    if not ids:
        summary.pages = len(wiki.pages)
        return summary
    backend = None
    profile = None
    conn = db.connect()

    def account(stage: str, usage: Usage) -> None:
        summary.cost += record_call(
            conn, backend.name, stage, usage, datetime.now(UTC), fs.pricing, fs.pricing_schedule
        )
        conn.commit()
        if summary.cost > fs.run.max_cost_usd:
            message = f"实际费用已超过 run.max_cost_usd=${fs.run.max_cost_usd}，停止"
            conn.execute(
                "UPDATE llm_calls SET error = ? WHERE id = last_insert_rowid()", (message,)
            )
            conn.commit()
            error = LLMError(message)
            error.stage = stage
            error.accounted = True
            raise error

    try:
        for iid in dict.fromkeys(ids):
            row = db.item_detail(conn, iid)
            if row is None:
                echo(f"{FAIL} 条目不存在：{iid}")
                summary.failures += 1
                continue
            if db.is_trashed(conn, iid):
                message = f"材料 #{iid} 已回收，请先恢复后再整理"
                echo(f"{FAIL} {message}")
                summary.errors.append(message)
                summary.failures += 1
                continue
            notes = material_notes(iid)
            material = wiki.pages.get(f"material-{iid}")
            compiled = (
                row["processing_status"] == "processed"
                and material is not None
                and material.meta.get("input_hash") == material_input_hash(row, notes)
            )
            if compiled and not force:
                _require_active(conn, iid)
                if row["reading_state"] == "read":
                    summary.updated += [
                        slug for slug in promote_material(wiki, iid)
                        if slug not in summary.updated
                    ]
                summary.cached.append(iid)
                echo(f"#{iid} {row['title']}：整理结果未变，复用已有页面（无模型调用）")
                continue
            if not compiled:
                conn.execute("UPDATE items SET processing_status = 'pending' WHERE id = ?", (iid,))
                conn.commit()
            if backend is None:
                try:
                    _, backend = _backend(settings, fs)
                except OpsError as exc:
                    echo(f"{FAIL} #{iid}：{exc}")
                    summary.errors.append(str(exc))
                    summary.failures += 1
                    break
                profile_file, _ = config.ensure_profile()
                profile = load_profile(profile_file)
            today = datetime.now().date()
            try:
                _, estimate, _ = ingest_item(
                    conn, wiki, backend, profile, row, fs, today,
                    estimate_only=True, user_notes=notes,
                )
            except LLMError as exc:
                echo(f"{FAIL} #{iid}：{exc}")
                summary.errors.append(str(exc))
                summary.failures += 1
                continue
            echo(f"#{iid} {row['title']}：预估 ${estimate:.4f}")
            if summary.cost + estimate > fs.run.max_cost_usd:
                echo(f"{FAIL} 预估费用超过上限 run.max_cost_usd=${fs.run.max_cost_usd}，停止")
                summary.errors.append("预估费用超过 run.max_cost_usd 上限")
                summary.failures += 1
                break
            before = summary.cost
            try:
                result, _, _ = ingest_item(
                    conn, wiki, backend, profile, row, fs, today,
                    on_usage=account, user_notes=notes,
                )
            except LLMError as exc:
                if not getattr(exc, "accounted", False):
                    summary.cost += record_call(
                        conn, backend.name, getattr(exc, "stage", "WIKI"), exc.usage,
                        datetime.now(UTC), fs.pricing, fs.pricing_schedule,
                        error=str(exc), model=backend.model_name("strong"),
                    )
                    conn.commit()
                summary.errors.append(str(exc))
                echo(f"{FAIL} #{iid}：{exc}")
                summary.failures += 1
                # 用量未知时无法确认剩余预算；不继续发起付费调用。
                if exc.usage is None or summary.cost >= fs.run.max_cost_usd:
                    break
                continue
            cost = summary.cost - before
            summary.created += [s for s in result.created if s not in summary.created]
            summary.updated += [s for s in result.updated if s not in summary.updated]
            echo(f"  新建：{', '.join(result.created) or '无'}")
            echo(f"  更新：{', '.join(result.updated) or '无'}")
            for line in result.rejected:
                echo(f"  {FAIL} 已拒绝：{line}")
            echo(f"  费用 ${cost:.4f}")
    finally:
        conn.close()
    summary.pages = len(wiki.pages)
    return summary


def material_notes(item_id: int, text: str | None = None) -> str:
    """个人笔记独立保存在同一 vault，机器整理不写此文件。"""
    conn = db.connect()
    try:
        if not db.item_exists(conn, item_id):
            raise OpsError(f"材料不存在：{item_id}")
    finally:
        conn.close()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    path = wiki.root / "notes" / f"material-{item_id}-notes.md"
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _discussion_reference(value: str) -> str | None:
    """仅保存公开格式的链接，不请求外部讨论，也不把无效值或凭据带入错误消息。"""
    from urllib.parse import urlsplit

    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "\\" not in value
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        )
        _ = parsed.port  # 验证端口格式与范围。
    except ValueError:
        valid = False
    if not valid:
        raise OpsError("讨论链接须为不含账号密码的有效 http(s) 链接")
    return value


def register_read(
    item_id: int,
    echo: Echo,
    *,
    notes: str | None = None,
    discussion_url: str | None = None,
    force: bool = False,
) -> dict:
    """登记外部阅读事实，再增量整理；整理失败不回滚已读或用户输入。"""
    reference = _discussion_reference(discussion_url) if discussion_url is not None else None
    conn = db.connect()
    try:
        if not db.item_exists(conn, item_id):
            raise OpsError(f"材料不存在：{item_id}")
        _require_active(conn, item_id)
        db.mark_read(conn, item_id, datetime.now(UTC))
        if discussion_url is not None:
            db.set_ai(
                conn, item_id, "reading_record", {"discussion_url": reference},
                None, datetime.now(UTC),
            )
            conn.commit()
    finally:
        conn.close()
    if notes is not None:
        material_notes(item_id, notes)
    try:
        summary = ingest_items([item_id], echo, force=force)
    except OpsError as exc:
        echo(f"{FAIL} #{item_id}：{exc}")
        summary = IngestSummary(failures=1, errors=[str(exc)])
    return {
        "item_id": item_id,
        "ingest": summary.__dict__,
        "cached": item_id in summary.cached,
    }


# ---------- 信源 trust 建议 ----------


def ensure_sources_file() -> tuple[Path, bool]:
    """返回 (config/sources.yaml, 是否刚初始化)；初始关注为空，已有文件不动。"""
    target = config.config_dir() / "sources.yaml"
    if target.exists():
        return target, False
    from secondbrain import sources_file

    sources_file.write("sources: []\n")
    return target, True


def edit_sources(edit) -> list:
    """按文本编辑 config/sources.yaml（保留注释）：不存在时先初始化空关注清单，
    edit(text) 返回新文本，校验通过后原子写入（ADR 0018）。"""
    from secondbrain import sources_file
    from secondbrain.source_management import source_lock

    with source_lock():
        path, _ = ensure_sources_file()
        return sources_file.write(edit(path.read_text(encoding="utf-8")))


# ---------- 深度速读与补前置（M8，ADR 0015） ----------

NO_TRANSCRIPT = "no_transcript"


@dataclass
class SummaryOutcome:
    data: dict
    cached: bool
    cost: float = 0.0


def _record(conn, backend, fs, calls, now) -> float:
    total = 0.0
    for stage, usage in calls:
        if usage is not None:
            total += record_call(
                conn, backend.name, stage, usage, now, fs.pricing, fs.pricing_schedule
            )
    return total


def _peak_guard(estimate: float, fs, now: datetime) -> float:
    """预估按高峰全价；当前不是高峰时乘以 off_peak_multiplier 再与 run.max_cost_usd 比较。"""
    from secondbrain.llm.usage import is_peak

    actual = (
        estimate
        if is_peak(now, fs.pricing_schedule)
        else estimate * (fs.pricing_schedule.off_peak_multiplier)
    )
    if actual > fs.run.max_cost_usd:
        raise OpsError(f"预估费用 ${actual:.3f} 超过上限 run.max_cost_usd=${fs.run.max_cost_usd}")
    return actual


def deep_summary(item_id: int, echo: Echo, *, force: bool = False) -> SummaryOutcome:
    """深度速读：已有结果直接返回（force 时重新生成）。读全文，长内容分块 map-reduce。"""
    from secondbrain.pipeline.run import system_prompt
    from secondbrain.pipeline.summary import deep_summary as run_summary
    from secondbrain.pipeline.summary import estimate_summary_cost
    from secondbrain.wiki.loop import known_knowledge

    config.load_env()
    settings, fs, _ = _settings()
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            raise OpsError(f"条目不存在：{item_id}")
        _require_active(conn, item_id)
        cached = db.get_ai(conn, item_id, "summary")
        if cached and not force:
            echo(f"#{item_id} 已有深度速读（{cached['_created_at']}），直接显示")
            return SummaryOutcome(cached, cached=True)
        if row["extract_method"] == NO_TRANSCRIPT or not (row["content"] or row["summary"]):
            raise OpsError("该条目没有正文（或视频没有字幕），无法速读")
        now = datetime.now(UTC)
        estimate = _peak_guard(estimate_summary_cost(len(row["content"] or ""), fs), fs, now)
        echo(f"#{item_id} {row['title']}：预估 ${estimate:.4f}")
        _, backend = _backend(settings, fs)
        profile_file, _ = config.ensure_profile()
        wiki = open_wiki(config.wiki_dir(), config.raw_dir())
        known = known_knowledge(wiki, [row], fs.l1.item_chars, fs.wiki.known_max_chars)
        system = system_prompt("summary_system", load_profile(profile_file))
        result = run_summary(backend, system, row, fs, known, progress=echo)
        cost = _record(conn, backend, fs, result.calls, now)
        conn.commit()
        if result.response is None:
            raise OpsError(f"{result.error}（已花费约 ${cost:.4f}）")
        data = result.response.model_dump()
        db.set_ai(conn, item_id, "summary", data, backend.model_name("strong"), now)
        conn.commit()
        echo(f"完成，费用 ${cost:.4f}")
        return SummaryOutcome(db.get_ai(conn, item_id, "summary"), cached=False, cost=cost)
    finally:
        conn.close()


@dataclass
class PrereqEntry:
    name: str
    why: str = ""
    page_slug: str | None = None  # wiki 中已有的页面
    page_title: str | None = None
    mastery: str | None = None
    explanation: str | None = None  # AI 生成的讲解（wiki 中没有时）
    explainers: list[dict] = field(default_factory=list)  # 已入库的讲解类条目


@dataclass
class PrereqReport:
    entries: list[PrereqEntry]
    cost: float = 0.0
    generated: int = 0  # 本次新生成的讲解数


def _prereq_key(name: str) -> str:
    """去重用的轻量归一：小写、去首尾空白、去掉末尾复数 s（induction heads ≡ induction head）。"""
    key = name.strip().lower()
    return key[:-1] if len(key) > 3 and key.endswith("s") else key


def prereq_names(conn, item_id: int) -> list[tuple[str, str]]:
    """前置知识 [(名字, 为什么需要)]：已有深度速读时只用速读的（更具体、带 why），
    否则用 L2 的 prerequisites；按名字轻量归一后去重。"""
    summary = db.get_ai(conn, item_id, "summary")
    if summary is not None:
        pairs = [(p["name"].strip(), p.get("why", "")) for p in summary.get("prerequisites", [])]
    else:
        l2 = db.item_assessment(conn, item_id, "L2") or {}
        pairs = [(name.strip(), "") for name in l2.get("prerequisites", [])]
    merged: dict[str, tuple[str, str]] = {}
    for name, why in pairs:
        if name:
            merged.setdefault(_prereq_key(name), (name, why))
    return list(merged.values())


def find_explainers(conn, item_id: int, name: str, limit: int) -> list[dict]:
    """已入库条目中讲这个知识点的 explainer / survey / tutorial（标题或 L1 topics 匹配）。"""
    from secondbrain.llm.schemas import REFRESHER_TYPES

    if limit <= 0:
        return []
    target = name.lower()
    l1 = db.latest_assessments(conn, "L1")
    ids = [i for i, a in l1.items() if i != item_id and a.get("content_type") in REFRESHER_TYPES]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, url FROM items i WHERE id IN ({marks})"
        " AND NOT EXISTS (SELECT 1 FROM material_lifecycle m"
        " WHERE m.item_id = i.id AND m.trashed = 1) ORDER BY id DESC", ids
    ).fetchall()
    found = []
    for r in rows:
        topics = [t.replace("-", " ").lower() for t in l1[r["id"]].get("topics", [])]
        if target in r["title"].lower() or any(t == target or target in t for t in topics):
            found.append({"id": r["id"], "title": r["title"], "url": r["url"]})
            if len(found) >= limit:
                break
    return found


def prerequisites(item_id: int, echo: Echo, *, force: bool = False) -> PrereqReport:
    """补前置：wiki 已有页面的给链接与掌握度；没有的一次 strong 调用批量生成讲解（缓存）。"""
    from secondbrain.pipeline.summary import STAGE_PREREQ, estimate_prereq_cost, explain_prereqs

    config.load_env()
    settings, fs, _ = _settings()
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            raise OpsError(f"条目不存在：{item_id}")
        _require_active(conn, item_id)
        wiki = open_wiki(config.wiki_dir(), config.raw_dir())
        entries = []
        for name, why in prereq_names(conn, item_id):
            entry = PrereqEntry(name, why)
            page = wiki.find(name)
            if page is not None:
                entry.page_slug, entry.page_title = page.slug, page.title
                entry.mastery = page.meta.get("mastery")
            entry.explainers = find_explainers(
                conn, item_id, name, fs.summary.explainers_per_prereq
            )
            entries.append(entry)
        report = PrereqReport(entries)
        if not entries:
            echo(f"#{item_id} 没有前置知识（L2 与深度速读都没有列出）")
            return report
        cache = (db.get_ai(conn, item_id, "prereq") or {}).get("explanations", {})
        missing = [e for e in entries if e.page_slug is None]
        need = missing if force else [e for e in missing if e.name not in cache]
        if need:
            now = datetime.now(UTC)
            estimate = _peak_guard(estimate_prereq_cost(len(need), fs), fs, now)
            echo(f"为 {len(need)} 个知识点生成讲解：预估 ${estimate:.4f}")
            _, backend = _backend(settings, fs)
            needs = [{"name": e.name, "why": e.why} for e in need]
            try:
                generated, usage = explain_prereqs(backend, row, needs)
            except LLMError as exc:
                report.cost = _record(conn, backend, fs, [(STAGE_PREREQ, exc.usage)], now)
                conn.commit()
                raise OpsError(str(exc)) from exc
            report.cost = _record(conn, backend, fs, [(STAGE_PREREQ, usage)], now)
            report.generated = len(generated)
            cache = {**cache, **generated}
            model = backend.model_name("strong")
            db.set_ai(conn, item_id, "prereq", {"explanations": cache}, model, now)
            conn.commit()
        for e in missing:
            e.explanation = cache.get(e.name)
        return report
    finally:
        conn.close()


def save_prereq_page(item_id: int, name: str) -> tuple[str, bool]:
    """把补前置的 AI 讲解存为概念页（不调用 LLM）；返回 (slug, 是否新建)。已存在则直接返回。

    frontmatter 字段齐全：mastery 陌生，sources 指向该条目，首次复习间隔 wiki.first_review_days。"""
    import hashlib

    from secondbrain.wiki.store import Page, WikiError, add_list_items, new_meta, slugify

    _, fs, _ = _settings()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        _require_active(conn, item_id)
        existing = wiki.find(name)
        if existing is not None:
            return existing.slug, False
        cache = (db.get_ai(conn, item_id, "prereq") or {}).get("explanations", {})
    finally:
        conn.close()
    if row is None:
        raise OpsError(f"条目不存在：{item_id}")
    explanation = cache.get(name)
    if not explanation:
        raise OpsError(f"还没有「{name}」的讲解，请先点「补前置」")
    try:
        slug = slugify(name)
    except WikiError:  # 中文等无法转成 slug 的名字
        slug = "concept-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    today = datetime.now().date()
    summary = explanation.replace("\n", " ").strip()[:80]
    meta = new_meta(
        name,
        "concept",
        today,
        summary=summary,
        mastery="陌生",
        sources=[item_id],
        interval_days=fs.wiki.first_review_days,
    )
    body = (
        f"# {name}\n\n"
        f"> AI 生成的前置知识讲解（来自条目 #{item_id} 的「补前置」），未经人工核对。\n\n"
        f"{explanation.strip()}\n"
    )
    body = add_list_items(body, "来源", [f"[{row['title']}]({row['url']})（条目 #{item_id}）"])
    wiki.save_page(Page(slug, meta, body))
    wiki.rebuild_index()
    wiki.append_log(today, "prereq", name, [f"新建 [[{slug}]]（AI 讲解，来源 #{item_id}）"])
    return slug, True


# ---------- 运维页设置（M10，ADR 0017） ----------

KEY_NAMES = {"deepseek": "DEEPSEEK_API_KEY"}


def ping() -> dict:
    """测试连接：当前后端 + cheap 模型的极小调用，记入用量（stage PING）。
    返回 {ok, backend, model, ms, error}；error 只含错误类型 / 状态码。"""
    import time as time_mod

    config.load_env()
    settings, fs, _ = _settings()
    name = config.llm_backend(settings)
    model = fs.llm.models["cheap"] if name != "fake" else "fake-cheap"
    try:
        _, backend = _backend(settings, fs)
    except OpsError as exc:
        return {"ok": False, "backend": name, "model": model, "ms": 0, "error": str(exc)}
    start = time_mod.monotonic()
    try:
        usage = backend.ping()
    except LLMError as exc:
        ms = round((time_mod.monotonic() - start) * 1000)
        return {"ok": False, "backend": name, "model": model, "ms": ms, "error": str(exc)}
    ms = round((time_mod.monotonic() - start) * 1000)
    conn = db.connect()
    try:
        now = datetime.now(UTC)
        record_call(conn, backend.name, "PING", usage, now, fs.pricing, fs.pricing_schedule)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "backend": name, "model": backend.model_name("cheap"), "ms": ms}


def _example_llm() -> dict:
    path = config.config_dir() / "settings.example.yaml"
    return (config._read_mapping(path).get("llm") or {}) if path.is_file() else {}


def save_llm_choice(cheap: str, strong: str, backend: str) -> list[str]:
    """把模型 / 后端写进 config/settings.yaml：与 example 相同的键从文件中删除，
    不同的才写入（不复制整份 example）。返回改动说明。"""
    merged = config.pipeline_settings(config.load_settings())
    prices = set(merged.pricing)
    for model in (cheap, strong):
        if model not in prices:
            raise OpsError(f"未知模型：{model}（应为价格表中的模型）")
    if backend not in config.KNOWN_LLM_BACKENDS:
        raise OpsError(f"未知后端：{backend}")
    example = _example_llm()
    defaults = example.get("models") or {}
    user = config.user_settings()
    llm = dict(user.get("llm") or {})
    models = dict(llm.get("models") or {})
    changes = []
    for key, value in (("cheap", cheap), ("strong", strong)):
        if value == defaults.get(key):
            models.pop(key, None)
        else:
            models[key] = value
        changes.append(f"{key} = {value}")
    if backend == (example.get("backend") or config.DEFAULT_LLM_BACKEND):
        llm.pop("backend", None)
    else:
        llm["backend"] = backend
    changes.append(f"backend = {backend}")
    _store_llm(user, llm, models)
    return changes


def reset_llm_choice() -> None:
    """恢复默认：从 config/settings.yaml 删除 llm.models.cheap / strong 与 llm.backend。"""
    user = config.user_settings()
    llm = dict(user.get("llm") or {})
    models = dict(llm.get("models") or {})
    for key in ("cheap", "strong"):
        models.pop(key, None)
    llm.pop("backend", None)
    _store_llm(user, llm, models)


def _store_llm(user: dict, llm: dict, models: dict) -> None:
    if models:
        llm["models"] = models
    else:
        llm.pop("models", None)
    if llm:
        user["llm"] = llm
    else:
        user.pop("llm", None)
    if not user and not (config.config_dir() / "settings.yaml").exists():
        return  # 与 example 相同：不创建个人设置文件
    config.save_user_settings(user)


# ---------- 统一材料导入（M13，ADR 0021） ----------

MANUAL_STATES = ("unread", "reading", "read", "later")
READ_FEEDBACK = ("gained", "too_easy", "too_hard", "worthless")
_ARXIV_URL = re.compile(
    r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/"
    r"([^\s?#]+?)(v\d+)?(?:\.pdf)?/?$", re.I,
)


@dataclass
class AddResult:
    url: str
    status: str  # 新增 | 已存在 | 失败
    item_id: int | None = None
    title: str = ""
    message: str = ""
    acquisition: dict = field(default_factory=dict)


MAX_PDF_UPLOAD = 30 * 1024 * 1024


def pdf_upload_text(data: bytes, max_pages: int, *, metadata: dict | None = None) -> str:
    """上传的 PDF（≤ 30MB，必须以 %PDF 开头）→ 文本；不合格或提取不到文本时抛 OpsError。"""
    from secondbrain.extract.pdf import extract_pdf_text

    if len(data) > MAX_PDF_UPLOAD:
        raise OpsError(f"PDF 超过 {MAX_PDF_UPLOAD // 1024 // 1024}MB 上限")
    if not data.startswith(b"%PDF"):
        raise OpsError("上传的文件不是 PDF")
    try:
        text = extract_pdf_text(data, max_pages, metadata=metadata)
    except Exception as exc:  # pypdf 对损坏文件的各种异常
        raise OpsError(f"PDF 无法解析：{type(exc).__name__}") from exc
    if not text:
        raise OpsError("PDF 中提取不到文本（可能是扫描件）")
    return text


def validate_manual(
    urls: list[str],
    state: str | None,
    feedback: str | None,
    ingest: bool,
    text: str | None,
    pdf: bytes | None = None,
) -> list[str]:
    """检查表单 / 命令行参数；返回去重后的 URL 列表，不合法时抛 OpsError。"""
    from urllib.parse import urlsplit

    from secondbrain.sources.homepage import safe_url

    cleaned = []
    for raw in urls:
        url = raw.strip()
        if not url:
            continue
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise OpsError("不是有效的 http(s) 链接") from exc
        if parsed.username is not None or parsed.password is not None:
            raise OpsError("材料链接不接受 URL 中的账号或密码")
        if safe_url(url) is None:
            raise OpsError("不是有效的 http(s) 链接")
        if url not in cleaned:
            cleaned.append(url)
    if not cleaned and not (text and text.strip()) and not pdf:
        raise OpsError("请至少给出一个 URL、PDF 或正文")
    if state is not None and state not in MANUAL_STATES:
        raise OpsError(f"状态应为 unread / reading / read / later：{state}")
    if feedback and (state != "read" or feedback not in READ_FEEDBACK):
        raise OpsError("读后反馈只能在「已读」时选择：gained / too_easy / too_hard / worthless")
    if text and text.strip() and len(cleaned) > 1:
        raise OpsError("正文粘贴只能用于单个 URL")
    if pdf and len(cleaned) > 1:
        raise OpsError("PDF 上传只能用于单个 URL")
    if text and text.strip() and pdf is not None:
        raise OpsError("正文粘贴与 PDF 上传二选一")
    return cleaned


class ManualFetchError(Exception):
    """手动添加时抓取失败；消息是给用户看的具体原因。"""

    def __init__(self, message: str, acquisition: dict | None = None):
        super().__init__(message)
        self.acquisition = acquisition or {}


@dataclass
class ManualFetched:
    entry: object  # RawEntry（entry.doi / entry.arxiv_id 带论文标识）
    content: str
    method: str
    canonical: str
    note: str = ""  # 如"仅摘要"
    pdf_data: bytes | None = None
    truncated: bool = False
    raw_data: bytes | None = None
    total_pages: int | None = None
    extracted_pages: int | None = None
    completeness: str | None = None
    acquisition: dict = field(default_factory=dict)


_CHALLENGE = re.compile(
    r"just a moment\.\.\.|cf-chl|challenge-platform|attention required! \| cloudflare"
    r"|captcha-delivery|please verify you are a human",
    re.I,
)
_ACCESS_GATE = re.compile(
    r'type\s*=\s*["\']password|sign in to (?:access|continue)|'
    r'log in to (?:access|continue)|institutional login|access denied|'
    r'purchase (?:this |the )?(?:article|paper)', re.I,
)
FALLBACK_HINT = "可粘贴正文或上传 PDF"
MIN_ARTICLE_CHARS = 200  # 网页提取到的正文短于该值时视为不是文章页（导航页、登录页等）


def manual_canonical(url: str) -> str:
    """去重用的规范 URL：arXiv → abs 页；含 DOI → https://doi.org/<doi>；其余规范化。"""
    from secondbrain.sources.arxiv import abs_url
    from secondbrain.sources.semantic_scholar import doi_in_url
    from secondbrain.urls import canonicalize_url

    m = _ARXIV_URL.search(url)
    if m:
        return canonicalize_url(abs_url(m.group(1)))
    doi = doi_in_url(url)
    return f"https://doi.org/{doi}" if doi else canonicalize_url(url)


def _url_ids(url: str) -> tuple[str | None, str | None]:
    """URL 本身带的 (doi, arxiv_id)。"""
    from secondbrain.sources.semantic_scholar import doi_in_url

    m = _ARXIV_URL.search(url)
    return (None, m.group(1) + (m.group(2) or "")) if m else (doi_in_url(url), None)


def _fetch_reason(exc: Exception) -> str:
    text = str(exc)
    if "HTTP 401" in text:
        return "HTTP 401：站点要求登录，未取得公开正文"
    if "HTTP 403" in text:
        return "HTTP 403：站点拒绝机器访问（被拦截）"
    if "HTTP 404" in text:
        return "HTTP 404：页面不存在"
    if "HTTP 429" in text:
        return "HTTP 429：被限流"
    if "Timeout" in text:
        return "超时：站点没有及时响应"
    return f"抓取失败：{text}"


def _identity(entry, content: str, expected) -> None:
    """明确冲突立即拒绝，不能把错误论文静默降级成所选论文的摘要。"""
    doi, aid = db.discover_ids(content)
    for label, wanted, actuals in (
        ("DOI", expected.doi, (entry.doi, doi)),
        ("arXiv", expected.arxiv_id, (entry.arxiv_id, aid)),
    ):
        wanted = re.sub(r"v\d+$", "", wanted or "", flags=re.I).casefold()
        for actual in actuals:
            actual = re.sub(r"v\d+$", "", actual or "", flags=re.I).casefold()
            if wanted and actual and wanted != actual:
                raise ValueError(f"材料身份冲突：下载记录的 {label} 与请求不同")


def _acquisition(url: str, capture, method="none", status="missing") -> dict:
    actual = capture.actual_url
    version = re.search(r"arxiv\.org/(?:pdf|html|abs)/[^?#]+?(v\d+)(?:\.pdf)?(?:[?#]|$)",
                        actual or "")
    return {
        "requested_url": url, "actual_url": actual, "status": status,
        "method": method, "version": version.group(1) if version else "unknown",
        "attempts": list(capture.client.attempts),
    }


def _captured(fetched: ManualFetched, capture, requested: str) -> ManualFetched:
    fetched.pdf_data = capture.pdf_data if fetched.method in ("pdf", "manual_pdf") else None
    fetched.raw_data = capture.raw_data
    fetched.truncated = fetched.method == "pdf" and capture.pdf_truncated
    fetched.total_pages = capture.pdf_metadata.get("total_pages") if fetched.pdf_data else None
    fetched.extracted_pages = (
        capture.pdf_metadata.get("extracted_pages") if fetched.pdf_data else None
    )
    fetched.completeness = fetched.completeness or capture.completeness or (
        "missing" if not fetched.content.strip() else
        "abstract" if fetched.method == "abstract_only" else
        "partial" if fetched.truncated else "full"
    )
    if fetched.completeness == "abstract" and not fetched.note:
        fetched.note = "仅摘要"
    fetched.acquisition = _acquisition(
        requested, capture, fetched.method, fetched.completeness
    )
    return fetched


def _pdf_text(client, url: str, fs, *, expected=None) -> str:
    """只接受真正 PDF；成功后才切换实际正文响应。"""
    from secondbrain.net import FetchError

    try:
        resp = client.get(url, max_bytes=fs.pdf_max_bytes)
        if not resp.content.startswith(b"%PDF"):
            raise ManualFetchError("公开 PDF 链接未返回 PDF（可能是登录页或验证页）")
        text = client.pdf_text(resp.content, fs.pdf_max_pages)
        if not text.strip():
            raise ManualFetchError("PDF 中没有可提取正文")
        if expected is not None:
            _identity(expected, text, expected)
        client.select(resp)
        return text
    except ValueError:
        raise
    except Exception as exc:
        # HTTP 拒绝不重试该地址；其它候选仍可继续。
        reason = _fetch_reason(exc) if isinstance(exc, FetchError) else str(exc)
        client.client.note(url, "unavailable", reason)
        return ""


def _fetch_page(url: str, client, fs, *, expected=None):
    """通用公开页面与 citation_pdf_url，摘要页不冒充完整论文。"""
    from urllib.parse import urljoin

    from secondbrain.extract.html import citation_meta, extract_main_text
    from secondbrain.models import RawEntry
    from secondbrain.net import FetchError
    from secondbrain.sources import sitemap
    from secondbrain.sources.base import parse_iso_datetime

    try:
        resp = client.get(url, max_bytes=fs.pdf_max_bytes)
    except FetchError as exc:
        raise ManualFetchError(_fetch_reason(exc)) from exc
    entry = RawEntry(url=url, title=url, page_meta=True)
    if resp.content.startswith(b"%PDF"):
        try:
            text = client.pdf_text(resp.content, fs.pdf_max_pages)
        except Exception as exc:
            raise ManualFetchError(f"PDF 无法解析：{type(exc).__name__}") from exc
        if not text.strip():
            raise ManualFetchError("PDF 中没有可提取正文")
        if expected is not None:
            _identity(entry, text, expected)
        client.select(resp)
        return entry, text, "pdf"
    if "pdf" in resp.headers.get("content-type", ""):
        raise ManualFetchError("PDF 响应不是有效 PDF")
    html = resp.text
    if _CHALLENGE.search(html[:20_000]):
        raise ManualFetchError("被拦截：站点返回了机器人验证页")
    if _ACCESS_GATE.search(html[:20_000]):
        raise ManualFetchError("站点返回登录、订阅或访问限制页面，未取得公开正文")
    text = extract_main_text(html, url=str(resp.url)) or ""
    sitemap.apply_page_meta(entry, html, str(resp.url))
    meta = citation_meta(html)
    if meta.get("title"):
        entry.title = " ".join(meta["title"].split())
    if meta.get("authors"):
        entry.author = ", ".join(meta["authors"])
    if meta.get("date"):
        entry.published_at = (
            parse_iso_datetime(meta["date"].replace("/", "-")) or entry.published_at
        )
    entry.doi, entry.arxiv_id = meta.get("doi"), meta.get("arxiv_id")
    if expected is not None:
        _identity(entry, "", expected)
        if (meta.get("title") and expected.title and expected.title != expected.url
                and re.findall(r"\w+", entry.title.casefold())
                != re.findall(r"\w+", expected.title.casefold())):
            raise ValueError("材料身份冲突：落地页标题与请求不同")
    if meta.get("pdf_url"):
        pdf_url = urljoin(str(resp.url), meta["pdf_url"])
        pdf = _pdf_text(client, pdf_url, fs, expected=expected or entry)
        if pdf:
            client.raw_data = resp.content
            return entry, pdf, "pdf"
    if len(text.strip()) < MIN_ARTICLE_CHARS:
        raise ManualFetchError(f"不是文章页：提取到的正文太短（{len(text.strip())} 字符）")
    paper = bool(entry.doi or entry.arxiv_id or meta.get("pdf_url")
                 or (expected is not None and (expected.doi or expected.arxiv_id)))
    completeness = "full"
    if paper:
        summary = (expected.summary or "") if expected is not None else ""
        completeness = "abstract" if len(text) < max(2000, 2 * len(summary)) else "partial"
    client.select(resp, raw_data=resp.content, completeness=completeness)
    if expected is not None:
        _identity(entry, text, expected)
    return entry, text, "html"


def _fetch_arxiv(arxiv_id: str, client, fs, fallback_title: str, *, metadata_only=False):
    from secondbrain.ingest import FAILED_METHODS, extract_content
    from secondbrain.models import RawEntry
    from secondbrain.net import FetchError
    from secondbrain.sources import arxiv

    expected = RawEntry(url=arxiv.abs_url(arxiv_id), title=fallback_title, arxiv_id=arxiv_id)
    entry = None
    try:
        resp = client.get(arxiv.API_URL, params={"id_list": arxiv_id})
        found = arxiv.parse_api_response(resp.content)
        if found:
            expected_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.I)
            entry = next((item for item in found if item.arxiv_id == expected_id), None)
            if entry is None:
                raise ValueError("材料身份冲突：arXiv API 未返回所请求的论文")
            _identity(entry, "", expected)
    except FetchError:
        pass
    entry = entry or expected
    # 保留用户明确指定的版本；API 记录通常省略版本。
    entry.arxiv_id = arxiv_id
    if metadata_only:
        return entry, "", "none"
    content, method = extract_content(entry, client, fs)
    if method in FAILED_METHODS or not content:
        if entry.summary:
            if entry is not expected:
                client.select(resp)
            client.completeness = "abstract"
            return entry, f"{entry.title}\n\n{entry.summary}", "abstract_only"
        raise ManualFetchError("arXiv 正文获取失败（HTML 与 PDF 都取不到）")
    _identity(entry, content, expected)
    return entry, content, method


def _fetch_doi(doi: str, url: str, client, fs, *, metadata_only=False) -> ManualFetched:
    """摘要只是回退：继续尝试明确 PDF / arXiv 及原始与 DOI 公开落地页。"""
    from secondbrain.models import RawEntry
    from secondbrain.net import FetchError
    from secondbrain.sources import crossref
    from secondbrain.sources.semantic_scholar import lookup_doi

    reasons, metadata = [], []
    s2 = cr = None
    try:
        s2 = lookup_doi(client, doi)
        supplied = (s2.get("externalIds") or {}).get("DOI")
        if supplied and supplied.casefold() != doi.casefold():
            raise ValueError("材料身份冲突：Semantic Scholar 返回了不同 DOI")
        metadata.append((s2.get("abstract") or "", client.last_response))
    except FetchError as exc:
        reasons.append(str(exc))
    # 即便 S2 有摘要，也需要 Crossref 的已索引公开地址。
    try:
        cr = crossref.lookup_doi(client, doi)
        message = client.last_response.json().get("message") or {}
        supplied = message.get("DOI")
        if supplied and supplied.casefold() != doi.casefold():
            raise ValueError("材料身份冲突：Crossref 返回了不同 DOI")
        if (s2 and re.findall(r"\w+", s2["title"].casefold())
                != re.findall(r"\w+", cr["title"].casefold())):
            raise ValueError("材料身份冲突：Crossref 与 Semantic Scholar 标题不同")
        metadata.append((cr.get("abstract") or "", client.last_response))
    except FetchError as exc:
        reasons.append(str(exc))
    title = (s2 or {}).get("title") or (cr or {}).get("title") or url
    abstract, abstract_response = max(metadata, key=lambda pair: len(pair[0]), default=("", None))
    authors = ", ".join(a["name"] for a in (s2 or {}).get("authors") or [] if a.get("name"))
    authors = authors or ", ".join((cr or {}).get("authors") or [])
    year = (s2 or {}).get("year")
    published = (cr or {}).get("published") or (
        datetime(int(year), 1, 1, tzinfo=UTC) if year else None
    )
    entry = RawEntry(url=url, title=title, author=authors or None, summary=abstract or None,
                     published_at=published, doi=doi)
    canonical = f"https://doi.org/{doi}"
    if not metadata_only:
        arxiv_id = ((s2 or {}).get("externalIds") or {}).get("ArXiv")
        if arxiv_id:
            try:
                found, text, method = _fetch_arxiv(arxiv_id, client, fs, title)
                _identity(found, text, entry)
                if method != "abstract_only":
                    found.doi = doi
                    found.summary = max((found.summary or "", abstract), key=len) or None
                    found.url = url
                    return ManualFetched(found, text, method, manual_canonical(
                        f"https://arxiv.org/abs/{arxiv_id}"
                    ))
            except ManualFetchError as exc:
                reasons.append(str(exc))
        pdf_urls = [((s2 or {}).get("openAccessPdf") or {}).get("url"),
                    *(cr or {}).get("pdf_urls", [])]
        for pdf_url in dict.fromkeys(u for u in pdf_urls if u):
            text = _pdf_text(client, pdf_url, fs, expected=entry)
            if text:
                return ManualFetched(entry, text, "pdf", canonical)
        partial = None
        for landing in dict.fromkeys([url, canonical]):
            try:
                page, text, method = _fetch_page(landing, client, fs, expected=entry)
                if entry.title == url:
                    entry.title = page.title
                entry.author = entry.author or page.author
                entry.published_at = entry.published_at or page.published_at
                fetched = _captured(ManualFetched(entry, text, method, canonical), client, url)
                if fetched.completeness == "full":
                    return fetched
                if partial is None or len(text) > len(partial.content):
                    partial = fetched
            except ManualFetchError as exc:
                reasons.append(str(exc))
        if partial is not None and (
            partial.completeness != "abstract" or len(partial.content) > len(abstract)
        ):
            return partial
    if not s2 and not cr:
        raise ManualFetchError("；".join(reasons) or "未取得论文元数据或公开正文")
    if abstract_response is not None:
        client.select(abstract_response, completeness="abstract" if abstract else "missing")
    content = title + ("\n\n" + abstract if abstract else "")
    return ManualFetched(entry, content, "abstract_only", canonical,
                         note="仅摘要" if abstract else "仅标题，S2 / Crossref 都没有摘要",
                         completeness="abstract" if abstract else "missing")


def _fetch_manual(
    url: str, client, fs, fallback: str | None = None, fallback_method: str = "manual_text"
) -> ManualFetched:
    """所有 URL 共用公开获取；显式正文 / PDF 只借助网络补元数据。"""
    from secondbrain.ingest import ContentCapture
    from secondbrain.models import RawEntry

    if not isinstance(client, ContentCapture):
        client = ContentCapture(client)
    canonical = manual_canonical(url)
    doi, arxiv_id = _url_ids(url)
    result, reasons = None, []
    try:
        if arxiv_id:
            entry, content, method = _fetch_arxiv(
                arxiv_id, client, fs, url, metadata_only=bool(fallback)
            )
            result = ManualFetched(entry, content, method, canonical)
        elif doi:
            result = _fetch_doi(doi, url, client, fs, metadata_only=bool(fallback))
        elif not fallback:
            entry, content, method = _fetch_page(url, client, fs)
            result = ManualFetched(entry, content, method, canonical)
    except ValueError as exc:
        exc.acquisition = _acquisition(url, client)
        exc.acquisition["actual_url"] = None
        raise
    except ManualFetchError as exc:
        client.client.note(url, "unavailable", str(exc))
        reasons.append(str(exc))
    if fallback:
        entry = result.entry if result else RawEntry(url=url, title=url, doi=doi)
        if entry.title == url:
            first = next((ln.strip() for ln in fallback.splitlines() if ln.strip()), url)
            entry.title = first[:120]
        result = ManualFetched(entry, fallback, fallback_method, canonical)
        client.actual_url, client.raw_data, client.completeness = None, None, "full"
    if result is None:
        raise ManualFetchError("；".join(reasons) + f"。{FALLBACK_HINT}",
                               _acquisition(url, client))
    result.entry.doi = (result.entry.doi or doi or "").lower() or None
    result.entry.arxiv_id = result.entry.arxiv_id or arxiv_id
    # _fetch_doi 可已保存较早的最佳页面，不能用后续失败请求覆盖其原件/来源。
    if result.acquisition:
        result.acquisition["attempts"] = list(client.client.attempts)
        return result
    return _captured(result, client, url)


def _material(fetched: ManualFetched, fs, *, title: str | None = None, kind="url"):
    """保留完整原文，正文处理窗口与原件完整度单独记录。"""
    from secondbrain.urls import content_hash

    entry = fetched.entry
    original = fetched.content
    truncated = fetched.truncated or len(original) > fs.content_max_chars
    return db.NewItem(
        source_id=None,
        url=entry.url,
        url_canonical=fetched.canonical,
        content_hash=content_hash(title or entry.title, original or entry.url),
        title=title or entry.title,
        author=entry.author,
        summary=entry.summary,
        content=original[:fs.content_max_chars],
        extract_method=fetched.method,
        published_at=entry.published_at,
        doi=entry.doi,
        arxiv_id=entry.arxiv_id,
        original_text=original,
        completeness=fetched.completeness or (
            "missing" if not original.strip() else
            "abstract" if fetched.method == "abstract_only" else
            "partial" if fetched.truncated else "full"
        ),
        truncated=truncated,
        pdf_data=fetched.pdf_data,
        origin_kind=kind,
        origin_locator=entry.url or f"sha256:{db.text_fingerprint(original)}",
        raw_data=fetched.raw_data,
        total_pages=fetched.total_pages,
        extracted_pages=fetched.extracted_pages,
        provenance={"acquisition": fetched.acquisition},
    )


REFETCH_NO_GAIN = "重新抓取未获得更完整正文"


def _refetch(conn, item_id: int, url: str, client, fs) -> tuple[bool, str]:
    _require_active(conn, item_id)
    if not url:
        return False, "本地材料没有可重新抓取的 URL；请上传 PDF 或粘贴正文"
    try:
        fetched = _fetch_manual(url, client, fs)
        _, _, improved = db.persist_material(
            conn, _material(fetched, fs), datetime.now(UTC), item_id=item_id
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return False, f"{REFETCH_NO_GAIN}：{exc}"
    return improved, "" if improved else REFETCH_NO_GAIN


def add_manual(
    urls: list[str],
    echo: Echo,
    *,
    state: str | None = None,
    feedback: str | None = None,
    ingest: bool = False,
    text: str | None = None,
    pdf: bytes | None = None,
    refetch: bool = False,
    client=None,
    title: str | None = None,
    reference_only: bool = False,
    learning_notes: str | None = None,
    discussion_url: str | None = None,
    origin_source_id: str | None = None,
) -> dict:
    """URL / 独立 PDF / 文本共用去重、补全、来源记录；默认不改变已有阅读状态。"""
    import hashlib

    from secondbrain.models import RawEntry

    urls = validate_manual(urls, state, feedback, ingest, text, pdf)
    if reference_only and (not urls or text or pdf is not None):
        raise OpsError("网页引用只接受 URL；不抓取正文，请在登记阅读时提供学习笔记")
    if learning_notes is not None or discussion_url is not None:
        if state != "read" or len(urls) > 1:
            raise OpsError("学习记录仅用于单份已读材料")
        if discussion_url is not None:
            _discussion_reference(discussion_url)
    selected_source = None
    if origin_source_id:
        selected_source = next(
            (s for s in config.load_sources() if s.id == origin_source_id), None
        )
        if selected_source is None:
            raise OpsError("该来源不在已关注信源中；未伪造材料来源")
    config.load_env()
    _, _, fetch_fs = _settings()
    fallback = text if text and text.strip() else None
    fallback_method = "manual_text"
    pdf_metadata: dict = {}
    if pdf is not None:
        fallback = pdf_upload_text(pdf, fetch_fs.pdf_max_pages, metadata=pdf_metadata)
        fallback_method = "manual_pdf"
    own_client = client is None
    client = client or HttpClient(fetch_fs)
    results: list[AddResult] = []
    conn = db.connect()
    try:
        if selected_source is not None:
            db.upsert_sources(conn, [selected_source])
        for url in urls or [""]:
            label = url or ("本地 PDF" if pdf is not None else "文本")
            try:
                refetch_note = ""
                failed_acquisition = {}
                if reference_only:
                    row, must_fetch = None, False
                    doi, arxiv_id = _url_ids(url)
                    fetched = ManualFetched(
                        RawEntry(url=url, title=title or url, doi=doi, arxiv_id=arxiv_id),
                        "", "none",
                        manual_canonical(url), completeness="missing",
                        note="仅保存网页引用，未获取正文",
                    )
                elif not url:
                    digest = (
                        hashlib.sha256(pdf).hexdigest() if pdf is not None
                        else db.text_fingerprint(fallback)
                    )
                    display_title = title or next(
                        (line.strip()[:120] for line in fallback.splitlines() if line.strip()),
                        label,
                    )
                    fetched = ManualFetched(
                        RawEntry(url="", title=display_title),
                        fallback, fallback_method, f"urn:sha256:{digest}",
                    )
                else:
                    existing = db.item_by_canonical(conn, manual_canonical(url))
                    if existing:
                        _require_active(conn, existing)
                    row = db.item_detail(conn, existing) if existing else None
                    must_fetch = (
                        row is None or fallback or refetch or existing in db.incomplete_ids(conn)
                    )
                    fetched = None
                    if must_fetch:
                        try:
                            fetched = _fetch_manual(
                                url, client, fetch_fs, fallback, fallback_method
                            )
                        except ManualFetchError as exc:
                            if row is None or fallback:
                                raise
                            refetch_note = f"{REFETCH_NO_GAIN}：{exc}"
                            failed_acquisition = exc.acquisition
                    if fetched is None:
                        fetched = ManualFetched(
                            RawEntry(
                                url=url, title=row["title"], author=row["author"],
                                doi=row["doi"], arxiv_id=row["arxiv_id"],
                            ),
                            row["original_text"], row["extract_method"], manual_canonical(url),
                            truncated=bool(row["content_truncated"]),
                            completeness=row["content_completeness"],
                        )
                if not fetched.acquisition:
                    fetched.acquisition = {
                        "requested_url": url, "actual_url": None,
                        "status": fetched.completeness or (
                            "missing" if not fetched.content.strip() else
                            "abstract" if fetched.method == "abstract_only" else "full"
                        ),
                        "method": fetched.method, "version": "unknown",
                        "attempts": failed_acquisition.get("attempts", []),
                    }
                if fallback:
                    fetched.content, fetched.method = fallback, fallback_method
                    fetched.pdf_data = pdf
                    fetched.truncated = pdf_metadata.get("total_pages", 0) > pdf_metadata.get(
                        "extracted_pages", 0
                    )
                    fetched.total_pages = pdf_metadata.get("total_pages")
                    fetched.extracted_pages = pdf_metadata.get("extracted_pages")
                    fetched.completeness = "partial" if fetched.truncated else "full"
                    fetched.acquisition.update(
                        status=fetched.completeness, method=fallback_method, actual_url=None
                    )
                kind = (
                    "reference" if reference_only else "pdf" if pdf is not None
                    else "text" if fallback else "url"
                )
                material = _material(fetched, fetch_fs, title=title, kind=kind)
                material.origin_locator = url or fetched.canonical
                material.origin_canonical = manual_canonical(url) if url else None
                material.origin_source_id = origin_source_id
                if origin_source_id:
                    material.origin_kind = "source_selection"
                item_id, created, improved = db.persist_material(
                    conn, material, datetime.now(UTC)
                )
                conn.commit()
                status = (
                    (f"新增（{fetched.note}）" if fetched.note else "新增") if created else
                    "已存在（补全正文）" if improved else "已存在"
                )
                message = refetch_note
                if url and row is not None and must_fetch and not improved and not fallback:
                    message = message or REFETCH_NO_GAIN
                if not url and not (material.doi or material.arxiv_id):
                    message = "身份尚不确定：未发现 DOI/arXiv；仅按文件/正文指纹去重，不按标题合并"
                if state in ("unread", "reading", "read"):
                    db.set_reading_state(conn, item_id, state)
                if state == "later":
                    db.add_to_reading_list(conn, item_id)
                if feedback:
                    db.add_feedback(conn, item_id, feedback, None, datetime.now(UTC))
                row = db.item_detail(conn, item_id)
                result = AddResult(label, status, item_id, row["title"], message,
                                   fetched.acquisition)
            except Exception as exc:
                conn.rollback()
                result = AddResult(label, "失败", message=str(exc),
                                   acquisition=getattr(exc, "acquisition", {}))
            results.append(result)
            if result.item_id is None:
                echo(f"✗ {label}：{result.message}")
            else:
                extra = f"（{result.message}）" if result.message else ""
                echo(f"✓ {label}：{result.status} #{result.item_id} {result.title}{extra}")
    finally:
        conn.close()
        if own_client:
            client.close()
    summary = None
    ids = list(dict.fromkeys(r.item_id for r in results if r.item_id is not None))
    if (state == "read" or ingest) and ids:
        echo(f"编入 wiki：{len(ids)} 条")
        if state == "read" and len(ids) == 1:
            recorded = register_read(
                ids[0], echo, notes=learning_notes, discussion_url=discussion_url
            )
            summary = IngestSummary(**recorded["ingest"])
        else:
            summary = ingest_items(ids, echo)
    return {
        "results": [r.__dict__ for r in results],
        "ingest": summary.__dict__ if summary else None,
    }


def refetch_item(item_id: int, client=None) -> str:
    """详情页"重新抓取"：对条目的链接重新走一遍 _fetch_manual，仅在更完整时替换正文。
    返回给用户看的结果文字。"""
    config.load_env()
    _, _, fetch_fs = _settings()
    own_client = client is None
    client = client or HttpClient(fetch_fs)
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            raise OpsError(f"条目不存在：{item_id}")
        _require_active(conn, item_id)
        replaced, note = _refetch(conn, item_id, row["url"], client, fetch_fs)
        if not replaced:
            return note
        row = db.item_detail(conn, item_id)
        return f"已补全正文：{row['extract_method']}，{row['content_len']} 字符（刷新页面查看）"
    finally:
        conn.close()
        if own_client:
            client.close()


def complete_item(item_id: int, *, text: str | None = None, pdf: bytes | None = None) -> str:
    """显式补全仍走统一持久化；保留每次原件，不以摘要或较差正文覆盖全文。"""
    from secondbrain.models import RawEntry

    if bool(text and text.strip()) == bool(pdf):
        raise OpsError("请粘贴正文或上传 PDF（二选一）")
    _, _, fetch_fs = _settings()
    metadata: dict = {}
    if pdf:
        content = pdf_upload_text(pdf, fetch_fs.pdf_max_pages, metadata=metadata)
        method = "manual_pdf"
    else:
        content, method = text, "manual_text"
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            raise OpsError(f"条目不存在：{item_id}")
        _require_active(conn, item_id)
        fetched = ManualFetched(
            RawEntry(
                url=row["url"], title=row["title"], author=row["author"],
                doi=row["doi"], arxiv_id=row["arxiv_id"],
            ),
            content, method, row["url_canonical"], pdf_data=pdf,
            truncated=metadata.get("total_pages", 0) > metadata.get("extracted_pages", 0),
            total_pages=metadata.get("total_pages"),
            extracted_pages=metadata.get("extracted_pages"),
        )
        db.persist_material(
            conn, _material(fetched, fetch_fs, kind="pdf" if pdf else "text"),
            datetime.now(UTC), item_id=item_id,
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        raise OpsError(str(exc)) from exc
    finally:
        conn.close()
    return method
