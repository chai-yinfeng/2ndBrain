"""CLI 与 Web 共用的运维流程：完整运行（带进程间锁）、编入 wiki、应用信源 trust 建议（ADR 0013）。

每个流程通过 echo 回调输出进度文字：CLI 传 typer.echo，Web 后台任务传任务日志。
"""

from __future__ import annotations

import fcntl
import os
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


def run_once(echo: Echo, *, limit: int | None = None, no_fetch: bool = False) -> RunReport:
    """抓取 → L0 → L1 → L2 → 分级，写入当天 digest。持有 run 锁期间执行。"""
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

        conn = db.connect()
        try:
            if not no_fetch:
                try:
                    sources = config.load_sources()
                except config.ConfigError as exc:
                    raise OpsError(str(exc)) from exc
                with HttpClient(fetch_fs) as client:
                    results = ingest.fetch_sources(
                        conn, sources, client, fetch_fs, limit=fs.run.fetch_limit
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
    target = config.CONFIG_DIR / "sources.yaml"
    if target.exists():
        return target, False
    example = config.CONFIG_DIR / "sources.example.yaml"
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return target, True


def apply_trust(changes: dict[str, int], echo: Echo) -> int:
    """把 trust 建议行级写入 config/sources.yaml（保留注释）；返回成功修改的信源数。"""
    from secondbrain.profile.tune import apply_trust_changes

    target, created = ensure_sources_file()
    if created:
        echo(f"{WARN} 已从 sources.example.yaml 生成 {target}（个人配置，已在 .gitignore 中）")
    new_text, missing = apply_trust_changes(target.read_text(encoding="utf-8"), changes)
    target.write_text(new_text, encoding="utf-8")
    for source_id in missing:
        echo(f"{FAIL} {source_id}：在 {target.name} 中找不到 trust 行，未修改")
    done = len(changes) - len(missing)
    echo(f"{OK} 已更新 {target}：{done} 个信源")
    return done
