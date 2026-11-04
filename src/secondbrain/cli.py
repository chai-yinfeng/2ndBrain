"""`sb` 命令行入口。"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta

import typer

from secondbrain import __version__, config, db, ingest
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import summarize
from secondbrain.net import HttpClient
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.loader import load_profile

app = typer.Typer(help="Second Brain：知识过滤 + 分级推荐 + 知识 wiki。", no_args_is_help=True)
sources_app = typer.Typer(help="信源管理。", no_args_is_help=True)
items_app = typer.Typer(help="已采集条目。", no_args_is_help=True)
digest_app = typer.Typer(help="每日分级推荐。", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(items_app, name="items")
app.add_typer(digest_app, name="digest")

OK, FAIL, WARN = "✓", "✗", "!"


@app.callback()
def main() -> None:
    """Second Brain 命令行工具。"""


@app.command()
def doctor() -> None:
    """检查运行环境与配置。真正的错误返回非 0；API key 未设置只警告。"""
    config.load_env()
    errors = 0

    def report(symbol: str, message: str) -> None:
        nonlocal errors
        if symbol == FAIL:
            errors += 1
        typer.echo(f"{symbol} {message}")

    typer.echo(f"secondbrain {__version__}")

    py = sys.version_info
    py_str = f"{py.major}.{py.minor}.{py.micro}"
    if py >= (3, 12):
        report(OK, f"Python {py_str}")
    else:
        report(FAIL, f"Python {py_str}（需要 >= 3.12）")

    settings: dict = {}
    path, is_example = config.settings_path()
    kind = "example" if is_example else "正式"
    try:
        settings = config.load_settings()
        report(OK, f"settings 可读（{kind}）：{path}")
    except Exception as exc:  # 汇报任何读取/解析错误
        report(FAIL, f"settings 不可读（{kind}）：{path}（{exc}）")

    for name in ("sources.example.yaml", "profile.example.md"):
        example = config.CONFIG_DIR / name
        if example.is_file():
            report(OK, f"{name} 存在")
        else:
            report(FAIL, f"{name} 缺失：{example}")

    data = config.data_dir()
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / f".sb-write-test-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report(OK, f"data 目录可写：{data}")
    except OSError as exc:
        report(FAIL, f"data 目录不可写：{data}（{exc}）")

    backend = config.llm_backend(settings)
    if backend in config.KNOWN_LLM_BACKENDS:
        report(OK, f"SB_LLM_BACKEND = {backend}")
    else:
        known = ", ".join(config.KNOWN_LLM_BACKENDS)
        report(FAIL, f"SB_LLM_BACKEND = {backend}（未知后端，可选：{known}）")

    # 只报告是否设置，绝不打印值。
    if os.environ.get("DEEPSEEK_API_KEY"):
        report(OK, "DEEPSEEK_API_KEY 已设置")
    elif backend == "deepseek":
        report(WARN, "DEEPSEEK_API_KEY 未设置（在 .env 中填写；离线可用 SB_LLM_BACKEND=fake）")
    else:
        report(OK, f"DEEPSEEK_API_KEY 未设置（当前后端 {backend} 不需要）")

    if errors:
        typer.echo(f"\n{errors} 项检查失败")
        raise typer.Exit(code=1)
    typer.echo("\n检查通过")


def _load_sources_or_exit() -> list:
    try:
        return config.load_sources()
    except config.ConfigError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@sources_app.command("list")
def sources_list() -> None:
    """列出配置中的信源。"""
    path, is_example = config.sources_path()
    sources = _load_sources_or_exit()
    typer.echo(f"{len(sources)} 个信源（{'example' if is_example else '正式'}：{path.name}）")
    width = max(len(s.id) for s in sources) if sources else 0
    for s in sources:
        status = ingest.skip_reason(s) or "启用"
        location = s.query if s.type == "arxiv" else s.url
        typer.echo(
            f"{s.id.ljust(width)}  {s.type:<9}  {s.role:<9}  trust={s.trust}  "
            f"{status:<20}  {_truncate(location or '', 70)}"
        )


@app.command()
def fetch(
    source: str | None = typer.Option(None, "--source", help="只抓取该 id 的信源"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="每个信源最多新增的条目数"),
) -> None:
    """抓取信源并入库。单个信源失败不影响其他信源，也不改变返回码。"""
    sources = _load_sources_or_exit()
    if source:
        sources = [s for s in sources if s.id == source]
        if not sources:
            typer.echo(f"{FAIL} 未找到信源：{source}", err=True)
            raise typer.Exit(code=1)
    try:
        fs = config.fetch_settings(config.load_settings())
    except (config.ConfigError, OSError, ValueError) as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc

    width = max(len(s.id) for s in sources)

    def show(result: ingest.SourceResult) -> None:
        name = result.source_id.ljust(width)
        if result.status == "ok":
            line = f"{OK} {name}  新增 {result.added} / 跳过重复 {result.duplicates}"
            if result.extract_failed:
                line += f"（正文提取失败 {result.extract_failed}，已用摘要代替）"
        elif result.status == "failed":
            line = f"{FAIL} {name}  失败：{result.message}"
        else:
            line = f"- {name}  跳过：{result.message}"
        typer.echo(line)

    conn = db.connect()
    try:
        with HttpClient(fs) as client:
            results = ingest.fetch_sources(conn, sources, client, fs, limit=limit, on_result=show)
    finally:
        conn.close()
    added = sum(r.added for r in results)
    failed = sum(r.status == "failed" for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    typer.echo(f"\n合计新增 {added} 条；失败 {failed} 个信源，跳过 {skipped} 个信源")


_SINCE_RE = re.compile(r"^(\d+)([hdw])$")
_SINCE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str) -> timedelta:
    """`12h` / `2d` / `1w` → timedelta。"""
    m = _SINCE_RE.match(value.strip())
    if not m:
        raise typer.BadParameter("格式应为 <数字><h|d|w>，如 2d")
    return timedelta(**{_SINCE_UNITS[m.group(2)]: int(m.group(1))})


@items_app.command("list")
def items_list(
    since: str | None = typer.Option(None, "--since", help="只看该时间段内的条目，如 2d、12h、1w"),
    source: str | None = typer.Option(None, "--source", help="只看该 id 的信源"),
) -> None:
    """列出已采集条目：时间、信源、标题、正文长度。"""
    cutoff = datetime.now(UTC) - parse_since(since) if since else None
    conn = db.connect()
    try:
        rows = db.list_items(conn, since=cutoff, source_id=source)
    finally:
        conn.close()
    for row in rows:
        ts = row["ts"].replace("T", " ")[:16]
        typer.echo(
            f"{ts}  {row['source_id']:<22}  {_truncate(row['title'], 60)}  "
            f"[{row['content_len']} 字符]"
        )
    if not rows:
        typer.echo("（没有条目）")


LANE_TITLES = {
    "must_read": "必读 must_read",
    "worth_it": "值得读 worth_it",
    "refresher": "温故 refresher",
    "serendipity": "意外 serendipity",
    "deferred": "顺延的深读 deferred",
    "fyi": "速览 fyi",
    "archived": "归档 archived",
}
DETAIL_LANES = ("must_read", "worth_it", "refresher", "serendipity", "deferred")
TOP_LANES = ("must_read", "worth_it", "refresher", "serendipity")
FYI_SHOWN = 15


def _load_pipeline_settings():
    try:
        settings = config.load_settings()
        return settings, config.pipeline_settings(settings), config.fetch_settings(settings)
    except (config.ConfigError, OSError, ValueError) as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def run(
    limit: int | None = typer.Option(None, "--limit", min=1, help="进入 L1 的候选条目上限"),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="跳过抓取，只处理库中已有条目"),
) -> None:
    """抓取 → L0 规则 → L1 粗筛 → L2 精读 → 分级，写入当天 digest。"""
    config.load_env()
    settings, fs, fetch_fs = _load_pipeline_settings()
    backend_name = config.llm_backend(settings)
    try:
        backend = make_backend(backend_name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
    except LLMError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc

    profile_file, created = config.ensure_profile()
    if created:
        typer.echo(f"{WARN} 已从 example 生成 {profile_file}，请按自己的情况修改")
    profile = load_profile(profile_file)
    for warning in profile.warnings:
        typer.echo(f"{WARN} profile：{warning}")
    typer.echo(f"LLM 后端：{backend_name}；profile 解析到 {len(profile.domains)} 个领域")

    conn = db.connect()
    try:
        if not no_fetch:
            sources = _load_sources_or_exit()
            with HttpClient(fetch_fs) as client:
                results = ingest.fetch_sources(
                    conn, sources, client, fetch_fs, limit=fs.run.fetch_limit
                )
            added = sum(r.added for r in results)
            failed = [r for r in results if r.status == "failed"]
            typer.echo(f"抓取：新增 {added} 条，失败 {len(failed)} 个信源")
            for r in failed:
                typer.echo(f"  {FAIL} {r.source_id}：{r.message}")
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
        )
    finally:
        conn.close()

    rejected = "，".join(f"{k} {v}" for k, v in report.l0_rejected.most_common()) or "无"
    typer.echo(
        f"L0：检查 {report.l0_checked} 条，排除 {sum(report.l0_rejected.values())} 条（{rejected}）"
    )
    r = report
    typer.echo(f"L1：送入 {r.l1_selected} 条，完成 {r.l1_assessed} 条，失败 {r.l1_failed} 条")
    typer.echo(f"L2：选中 {r.l2_selected} 条，完成 {r.l2_assessed} 条，失败 {r.l2_failed} 条")
    for error in report.errors:
        typer.echo(f"  {FAIL} {error}")
    typer.echo(f"本次 LLM 调用 {report.calls} 次，估算费用 ${report.cost_usd:.4f}")
    if report.aborted:
        typer.echo(f"{FAIL} {report.aborted}")
        raise typer.Exit(code=1)
    lanes = "，".join(
        f"{LANE_TITLES[k].split()[0]} {report.lanes[k]}" for k in LANE_TITLES if report.lanes[k]
    )
    typer.echo(f"分级：{lanes or '无新条目'}")
    typer.echo(
        f"今日已排 {report.minutes_used} / {fs.daily_budget_minutes} 分钟；"
        f"积压必读 {report.backlog_kept} 条，降级 {report.backlog_demoted} 条"
    )


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _lane_line(position: int, row, l1: dict, l2: dict | None) -> list[str]:
    deep = " · 深读" if row["is_deep_read"] else ""
    deferred = f" · 顺延自 {row['deferred_from']}" if row["deferred_from"] else ""
    lines = [
        f"{position}. {_flat(row['title'])}",
        f"   {row['source_id']} · {row['est_minutes']} 分钟{deep}{deferred}",
    ]
    if l2:
        lines.append(f"   TL;DR：{_flat(l2['tldr'])}")
        lines.append(f"   理由：{_flat(l2['why_for_you'])}")
        if l2.get("prerequisites"):
            lines.append(f"   前置知识：{'、'.join(l2['prerequisites'])}")
    else:
        lines.append(f"   理由：{_flat(l1.get('one_line_reason', ''))}")
    lines.append(f"   {row['url']}")
    return lines


@digest_app.command("show")
def digest_show(
    day: str | None = typer.Option(None, "--date", help="日期 YYYY-MM-DD，默认最近一次 digest"),
) -> None:
    """按 lane 展示某天的分级推荐。"""
    _, fs, _ = _load_pipeline_settings()
    conn = db.connect()
    try:
        day = day or db.latest_digest_date(conn)
        if not day:
            typer.echo("（还没有 digest，先运行 sb run）")
            return
        rows = db.digest_rows(conn, day)
        l1 = db.latest_assessments(conn, "L1")
        l2 = db.latest_assessments(conn, "L2")
        backlog = [d for _, d in db.past_must_reads(conn, day)]
    finally:
        conn.close()
    if not rows:
        typer.echo(f"（{day} 没有 digest）")
        return
    by_lane: dict[str, list] = {}
    for row in rows:
        by_lane.setdefault(row["lane"], []).append(row)
    typer.echo(f"# Digest {day}\n")
    for lane in DETAIL_LANES:
        entries = by_lane.get(lane, [])
        if lane == "deferred" and not entries:
            continue
        typer.echo(f"## {LANE_TITLES[lane]}（{len(entries)}）")
        if not entries:
            typer.echo("（空）")
        for position, row in enumerate(entries, start=1):
            item = row["item_id"]
            for line in _lane_line(position, row, l1.get(item, {}), l2.get(item)):
                typer.echo(line)
        typer.echo("")
    fyi = by_lane.get("fyi", [])
    typer.echo(f"## {LANE_TITLES['fyi']}（{len(fyi)}）")
    for row in fyi[:FYI_SHOWN]:
        reason = l1.get(row["item_id"], {}).get("one_line_reason", "")
        typer.echo(f"- {_truncate(_flat(row['title']), 70)}（{row['source_id']}）{_flat(reason)}")
    if len(fyi) > FYI_SHOWN:
        typer.echo(f"- …… 另有 {len(fyi) - FYI_SHOWN} 条")
    typer.echo(f"\n归档 {len(by_lane.get('archived', []))} 条")
    used = sum(r["est_minutes"] for r in rows if r["lane"] in TOP_LANES)
    typer.echo(
        f"总耗时 {used} / 预算 {fs.daily_budget_minutes} 分钟；此前未读的必读积压 {len(backlog)} 条"
    )


@app.command()
def usage(
    since: str | None = typer.Option(None, "--since", help="统计该时间段，如 1d、7d；默认全部"),
) -> None:
    """按阶段与模型汇总 LLM token 用量与估算费用。"""
    cutoff = datetime.now(UTC) - parse_since(since) if since else None
    conn = db.connect()
    try:
        rows = summarize(conn, cutoff)
    finally:
        conn.close()
    if not rows:
        typer.echo("（没有 LLM 调用记录）")
        return
    typer.echo(
        f"{'stage':<6} {'model':<18} {'calls':>5} {'prompt':>10} {'cache_hit':>10} "
        f"{'completion':>10} {'cost_usd':>10}"
    )
    total = 0.0
    for r in rows:
        total += r["cost_usd"]
        typer.echo(
            f"{r['stage']:<6} {r['model']:<18} {r['calls']:>5} {r['prompt_tokens']:>10} "
            f"{r['cache_hit_tokens']:>10} {r['completion_tokens']:>10} {r['cost_usd']:>10.4f}"
        )
    typer.echo(f"合计估算费用：${total:.4f}")
