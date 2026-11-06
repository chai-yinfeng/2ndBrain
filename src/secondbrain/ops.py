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
from secondbrain.llm.base import LLMError, make_backend
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


def ingest_items(ids: list[int], echo: Echo) -> IngestSummary:
    """把条目逐个编进 wiki（strong 模型）；累计预估费用超过 run.max_cost_usd 时停止。"""
    from secondbrain.wiki.ingest import ingest_item

    summary = IngestSummary()
    config.load_env()
    settings, fs, _ = _settings()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    if not ids:
        summary.pages = len(wiki.pages)
        return summary
    _, backend = _backend(settings, fs)
    profile_file, _ = config.ensure_profile()
    profile = load_profile(profile_file)
    conn = db.connect()
    try:
        for iid in ids:
            row = db.item_detail(conn, iid)
            if row is None:
                echo(f"{FAIL} 条目不存在：{iid}")
                summary.failures += 1
                continue
            today = datetime.now().date()
            _, estimate, _ = ingest_item(
                conn, wiki, backend, profile, row, fs, today, estimate_only=True
            )
            echo(f"#{iid} {row['title']}：预估 ${estimate:.4f}")
            if summary.cost + estimate > fs.run.max_cost_usd:
                echo(f"{FAIL} 预估费用超过上限 run.max_cost_usd=${fs.run.max_cost_usd}，停止")
                summary.failures += 1
                break
            now = datetime.now(UTC)
            try:
                result, _, _ = ingest_item(conn, wiki, backend, profile, row, fs, today)
            except LLMError as exc:
                if exc.usage:
                    summary.cost += record_call(
                        conn, backend.name, "WIKI", exc.usage, now, fs.pricing, fs.pricing_schedule
                    )
                    conn.commit()
                echo(f"{FAIL} #{iid}：{exc}")
                summary.failures += 1
                continue
            cost = record_call(
                conn, backend.name, "WIKI", result.usage, now, fs.pricing, fs.pricing_schedule
            )
            conn.commit()
            summary.cost += cost
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


# ---------- 信源 trust 建议 ----------


def ensure_sources_file() -> tuple[Path, bool]:
    """返回 (config/sources.yaml, 是否刚从 example 复制)。已存在时绝不覆盖。"""
    target = config.config_dir() / "sources.yaml"
    if target.exists():
        return target, False
    example = config.config_dir() / "sources.example.yaml"
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return target, True


def edit_sources(edit) -> list:
    """按文本编辑 config/sources.yaml（保留注释）：不存在时先从 example 复制，
    edit(text) 返回新文本，校验通过后原子写入（ADR 0018）。"""
    from secondbrain import sources_file

    path, _ = ensure_sources_file()
    return sources_file.write(edit(path.read_text(encoding="utf-8")))


def catalog_entries() -> list:
    """信源目录：sources.example.yaml 中、不在当前生效配置里的条目（按 id）。"""
    example = config.config_dir() / "sources.example.yaml"
    if not example.is_file():
        return []
    try:
        current = {s.id for s in config.load_sources()}
    except config.ConfigError:
        current = set()
    return [s for s in config.load_sources(example) if s.id not in current]


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
        f"SELECT id, title, url FROM items WHERE id IN ({marks}) ORDER BY id DESC", ids
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
    existing = wiki.find(name)
    if existing is not None:
        return existing.slug, False
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
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


# ---------- 手动添加文章（M11，ADR 0018） ----------

MANUAL_STATES = ("read", "later")
READ_FEEDBACK = ("gained", "too_easy", "too_hard", "worthless")
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?/?$")


@dataclass
class AddResult:
    url: str
    status: str  # 新增 | 已存在 | 失败
    item_id: int | None = None
    title: str = ""
    message: str = ""


def validate_manual(
    urls: list[str], state: str, feedback: str | None, ingest: bool, text: str | None
) -> list[str]:
    """检查表单 / 命令行参数；返回去重后的 URL 列表，不合法时抛 OpsError。"""
    from secondbrain.sources.homepage import safe_url

    cleaned = []
    for raw in urls:
        url = raw.strip()
        if not url:
            continue
        if safe_url(url) is None:
            raise OpsError(f"不是 http(s) 链接：{url}")
        if url not in cleaned:
            cleaned.append(url)
    if not cleaned:
        raise OpsError("请至少给出一个 URL")
    if state not in MANUAL_STATES:
        raise OpsError(f"状态应为 read 或 later：{state}")
    if feedback and (state != "read" or feedback not in READ_FEEDBACK):
        raise OpsError("读后反馈只能在「已读」时选择：gained / too_easy / too_hard / worthless")
    if ingest and not (state == "read" and feedback == "gained"):
        raise OpsError("编入 wiki 只适用于「已读 + 有收获」（wiki 队列只收当前状态为有收获的条目）")
    if text and len(cleaned) != 1:
        raise OpsError("正文粘贴只能用于单个 URL")
    return cleaned


def _fetch_manual(url: str, client, fs, text: str | None):
    """抓取单个 URL：arXiv 链接走 API 元数据 + arxiv_html / pdf；其他走网页正文提取，
    页面元数据补全标题 / 作者 / 日期。拿不到正文时用粘贴的正文；都没有时返回 None。"""
    from secondbrain.ingest import FAILED_METHODS, extract_content
    from secondbrain.models import RawEntry
    from secondbrain.net import FetchError
    from secondbrain.sources import arxiv

    m = _ARXIV_URL.search(url)
    entry = None
    if m:
        try:
            resp = client.get(arxiv.API_URL, params={"id_list": m.group(1)})
            found = arxiv.parse_api_response(resp.content)
            entry = found[0] if found else None
        except FetchError:
            entry = None
        entry = entry or RawEntry(url=arxiv.abs_url(m.group(1)), title=url, arxiv_id=m.group(1))
    else:
        entry = RawEntry(url=url, title=url, page_meta=True)
    content, method = extract_content(entry, client, fs)
    if method in FAILED_METHODS or not content:
        if not text:
            return None
        content, method = text.strip(), "manual_text"
        if entry.title == url:
            first = next((ln.strip() for ln in content.splitlines() if ln.strip()), url)
            entry.title = first[:120]
    return entry, content[: fs.content_max_chars], method


def add_manual(
    urls: list[str],
    echo: Echo,
    *,
    state: str,
    feedback: str | None = None,
    ingest: bool = False,
    text: str | None = None,
    client=None,
) -> dict:
    """手动添加文章（逐个处理，单个失败不影响其他）：归到虚拟信源 manual、状态 manual，
    不进 L1 / L2 / 分级 / 预算。已存在（规范化 URL 或正文哈希相同）的只更新状态与反馈。
    准备读 = later 反馈；已读 = 所选读后反馈，未选时只设置 read_at（已读、无评价）。"""
    from secondbrain.sources.arxiv import abs_url
    from secondbrain.urls import canonicalize_url, content_hash

    urls = validate_manual(urls, state, feedback, ingest, text)
    _, fs, fetch_fs = _settings()
    own_client = client is None
    client = client or HttpClient(fetch_fs)
    results: list[AddResult] = []
    conn = db.connect()
    try:
        db.ensure_manual_source(conn)
        for url in urls:
            m = _ARXIV_URL.search(url)
            canonical = canonicalize_url(abs_url(m.group(1)) if m else url)
            item_id = db.item_by_canonical(conn, canonical)
            result = AddResult(url, "已存在", item_id)
            if item_id is None:
                try:
                    fetched = _fetch_manual(url, client, fetch_fs, text)
                except Exception as exc:  # 单个 URL 的任何错误都不影响其他
                    fetched, reason = None, f"{type(exc).__name__}: {exc}"
                else:
                    reason = "无法获取正文（页面抓取失败、需要登录或不是文章页）；可粘贴正文后重试"
                if fetched is None:
                    result.status, result.message = "失败", reason
                    results.append(result)
                    echo(f"✗ {url}：{reason}")
                    continue
                entry, content, method = fetched
                digest = content_hash(entry.title, content)
                new = db.NewItem(
                    source_id=db.MANUAL_SOURCE,
                    url=entry.url,
                    url_canonical=canonical,
                    content_hash=digest,
                    title=entry.title,
                    author=entry.author,
                    summary=entry.summary,
                    content=content,
                    extract_method=method,
                    published_at=entry.published_at,
                )
                if db.insert_item(conn, new, datetime.now(UTC)):
                    item_id = db.item_by_canonical(conn, canonical)
                    db.set_status(conn, [item_id], "manual")
                    result.status = "新增"
                else:  # 正文与已有条目相同
                    item_id = db.item_by_hash(conn, digest) or db.item_by_canonical(conn, canonical)
                    result.message = "正文与已有条目相同"
                result.item_id = item_id
                conn.commit()
            now = datetime.now(UTC)
            if state == "later":
                db.add_feedback(conn, item_id, "later", None, now)
            elif feedback:
                db.add_feedback(conn, item_id, feedback, None, now)
            else:
                db.mark_read(conn, item_id, now)
            result.title = conn.execute(
                "SELECT title FROM items WHERE id = ?", (item_id,)
            ).fetchone()[0]
            results.append(result)
            echo(f"✓ {url}：{result.status} #{item_id} {result.title}")
    finally:
        conn.close()
        if own_client:
            client.close()
    summary = None
    ids = [r.item_id for r in results if r.item_id is not None]
    if ingest and ids:
        conn = db.connect()
        try:
            done = db.ingested_ids(conn)
        finally:
            conn.close()
        todo = [i for i in ids if i not in done]
        echo(f"编入 wiki：{len(todo)} 条")
        summary = ingest_items(todo, echo) if todo else IngestSummary()
    return {
        "results": [r.__dict__ for r in results],
        "ingest": summary.__dict__ if summary else None,
    }
