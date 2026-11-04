"""`sb` 命令行入口。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import typer

from secondbrain import __version__, config, db, ingest, schedule
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import summarize
from secondbrain.net import HttpClient
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.loader import load_profile
from secondbrain.wiki.store import open_wiki

app = typer.Typer(help="Second Brain：知识过滤 + 分级推荐 + 知识 wiki。", no_args_is_help=True)
sources_app = typer.Typer(help="信源管理。", no_args_is_help=True)
items_app = typer.Typer(help="已采集条目。", no_args_is_help=True)
digest_app = typer.Typer(help="每日分级推荐。", no_args_is_help=True)
schedule_app = typer.Typer(help="macOS launchd 定时运行 sb run。", no_args_is_help=True)
profile_app = typer.Typer(help="用户画像（profile.md）。", no_args_is_help=True)
wiki_app = typer.Typer(help="知识 wiki（data/wiki，Obsidian 兼容）。", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(items_app, name="items")
app.add_typer(digest_app, name="digest")
app.add_typer(schedule_app, name="schedule")
app.add_typer(profile_app, name="profile")
app.add_typer(wiki_app, name="wiki")

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
            if result.backfilled:
                line += f"（其中回补池 {result.backfilled}）"
            if result.extract_failed:
                line += f"（正文 / 字幕提取失败 {result.extract_failed}）"
        elif result.status == "failed":
            line = f"{FAIL} {name}  失败：{result.message}"
        else:
            line = f"- {name}  跳过：{result.message}"
        typer.echo(line)
        for note in result.notes:
            typer.echo(f"    {WARN} {note}")

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
            wiki=open_wiki(config.wiki_dir(), config.raw_dir()),
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
    if r.concepts_placed:
        typer.echo(f"wiki：到期概念 {r.concepts_placed} 个放入温故栏")
    if r.backfill_screened or r.backfill_picked:
        typer.echo(
            f"回补：粗筛 {r.backfill_screened} 条，选中 {r.backfill_picked} 条，"
            f"放入温故 {r.backfill_placed} 条"
        )
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
    if row["extract_method"] == "no_transcript":
        deep += " · 无字幕"
    if row["is_backfill"]:
        deep += " · 经典回补"
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
        concept_rows = db.digest_concepts(conn, day)
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
        if lane == "refresher":
            for c in concept_rows:
                mastery = c["mastery"] or "—"
                typer.echo(f"* 概念复习：{c['title']}（掌握度 {mastery}）· {c['est_minutes']} 分钟")
                typer.echo(f"   wiki：[[{c['slug']}]]；复习：sb wiki review {c['slug']} <结果>")
                if c["related_title"]:
                    typer.echo(f"   相关条目：{c['related_title']}  {c['related_url']}")
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
    used += sum(c["est_minutes"] for c in concept_rows)
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


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（默认只监听本机）"),
    port: int = typer.Option(8000, "--port", help="端口"),
) -> None:
    """启动本地 Web UI。"""
    import uvicorn

    from secondbrain.web.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo(f"{WARN} 监听 {host}：局域网内其他设备也能访问，且没有登录保护")
    typer.echo(f"Second Brain：http://{host}:{port}/  （Ctrl+C 退出）")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


def _peak_note(local, fs) -> list[str]:
    lines = []
    for check in schedule.utc_checks(local, fs.pricing_schedule):
        status = "高峰时段（全价）" if check.peak else "非高峰（半价）"
        lines.append(f"  {check.label}：本地 {local:%H:%M} = UTC {check.utc:%H:%M}，{status}")
    return lines


@schedule_app.command("install")
def schedule_install(
    at: str = typer.Option(schedule.DEFAULT_TIME, "--time", help="每天运行的本地时间 HH:MM"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印将写入的 plist 与命令，不做改动"),
) -> None:
    """生成 LaunchAgent，每天在指定本地时间运行 `uv run sb run`。"""
    try:
        local = schedule.parse_time(at)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _, fs, _ = _load_pipeline_settings()
    log_dir = config.data_dir() / "logs"
    plist = schedule.build_plist(local, config.PROJECT_ROOT, log_dir, schedule.find_uv())
    target = schedule.plist_path()
    notes = _peak_note(local, fs)
    tz = datetime.now().astimezone().tzname()
    typer.echo(f"每天本地 {local:%H:%M}（本机时区 {tz}）运行 `uv run sb run`；高峰按 UTC 判断：")
    for line in notes:
        typer.echo(line)
    if any("高峰时段" in line for line in notes):
        typer.echo(f"{WARN} 该时间落在 DeepSeek 高峰时段（UTC 周一至周五 01–04、06–10），费用翻倍")
    typer.echo(f"日志：{log_dir}/run.log、run.err.log")
    command = ["launchctl", "load", "-w", str(target)]
    if dry_run:
        typer.echo(f"\n[dry-run] 将写入 {target}：\n")
        typer.echo(plist.decode())
        typer.echo(f"[dry-run] 将执行：{' '.join(command)}")
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plist)
    subprocess.run(command, check=True)
    typer.echo(f"{OK} 已安装 {target}")


@schedule_app.command("uninstall")
def schedule_uninstall(
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印将执行的操作，不做改动"),
) -> None:
    """卸载 LaunchAgent。"""
    target = schedule.plist_path()
    command = ["launchctl", "unload", "-w", str(target)]
    if dry_run:
        exists = "存在" if target.exists() else "不存在"
        typer.echo(f"[dry-run] 将执行：{' '.join(command)}")
        typer.echo(f"[dry-run] 将删除：{target}（当前{exists}）")
        return
    if not target.exists():
        typer.echo(f"{WARN} 未安装：{target} 不存在")
        return
    subprocess.run(command, check=False)
    target.unlink()
    typer.echo(f"{OK} 已卸载 {target}")


@profile_app.command("update")
def profile_update(
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply", help="默认只打印 diff；--apply 备份旧版后写入"
    ),
) -> None:
    """根据近期反馈，让 strong 模型提出 profile.md 修改提案，打印 unified diff。"""
    from secondbrain.llm.usage import record_call
    from secondbrain.profile.feedback import recent_feedback
    from secondbrain.profile.rewrite import generate_update, save_profile

    config.load_env()
    settings, fs, _ = _load_pipeline_settings()
    pers = fs.personalize
    now = datetime.now(UTC)
    conn = db.connect()
    try:
        feedback = recent_feedback(conn, now, pers.feedback_window_days, pers.summary_max_items)
        if len(feedback) < pers.min_feedback_for_update:
            typer.echo(
                f"无足够反馈，不提议修改（近 {pers.feedback_window_days} 天内 "
                f"不感兴趣 / 太浅 / 有收获 / 太难 / 没价值 共 {len(feedback)} 条，"
                f"至少需要 {pers.min_feedback_for_update} 条）"
            )
            return
        backend_name = config.llm_backend(settings)
        try:
            backend = make_backend(backend_name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
        except LLMError as exc:
            typer.echo(f"{FAIL} {exc}", err=True)
            raise typer.Exit(code=1) from exc
        path, created = config.ensure_profile()
        if created:
            typer.echo(f"{WARN} 已从 example 生成 {path}")
        typer.echo(f"LLM 后端：{backend_name}；依据 {len(feedback)} 条反馈生成提案")
        try:
            result = generate_update(backend, path.read_text(encoding="utf-8"), feedback)
        except LLMError as exc:
            if exc.usage:
                record_call(
                    conn, backend.name, "PROFILE", exc.usage, now, fs.pricing, fs.pricing_schedule
                )
                conn.commit()
            typer.echo(f"{FAIL} {exc}", err=True)
            raise typer.Exit(code=1) from exc
        cost = record_call(
            conn, backend.name, "PROFILE", result.usage, now, fs.pricing, fs.pricing_schedule
        )
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"本次估算费用 ${cost:.4f}")
    if result.summary:
        typer.echo(f"提案摘要：{result.summary}")
    for line in result.applied:
        typer.echo(f"{OK} {line}")
    for line in result.rejected:
        typer.echo(f"{FAIL} 已拒绝：{line}")
    if not result.changed:
        typer.echo("提案没有可应用的修改，profile 不变")
        return
    typer.echo("")
    typer.echo(result.diff(path.name), nl=False)
    if dry_run:
        typer.echo("\n（dry-run：未写入；确认后用 --apply 应用）")
        return
    backup = save_profile(path, result.new_text, config.data_dir() / "profile_history", now)
    typer.echo(f"\n{OK} 已写入 {path}；旧版本备份为 {backup}")


@sources_app.command("tune")
def sources_tune(
    apply: bool = typer.Option(False, "--apply", help="把建议写入 config/sources.yaml"),
) -> None:
    """按信源的 有收获 / 不感兴趣 / 没价值 比例给出 trust 调整建议（±1，限 1–5）。"""
    from secondbrain.profile.tune import apply_trust_changes, source_stats

    _, fs, _ = _load_pipeline_settings()
    sources = _load_sources_or_exit()
    pers = fs.personalize
    conn = db.connect()
    try:
        stats = source_stats(conn, sources, datetime.now(UTC), pers)
    finally:
        conn.close()
    typer.echo(
        f"近 {pers.tune_window_days} 天；每个条目取当前反馈（有收获为正，不感兴趣 / 没价值为负）；"
        f"样本 ≥ {pers.tune_min_samples} 才给建议"
    )
    width = max(len(st.source_id) for st in stats) if stats else 0
    shown = [st for st in stats if st.samples or st.suggested]
    for st in shown:
        change = f"trust {st.trust} → {st.suggested}" if st.suggested else f"trust {st.trust}"
        typer.echo(
            f"{st.source_id.ljust(width)}  正 {st.positive:>3}  负 {st.negative:>3}  "
            f"{change:<14}  {st.reason}"
        )
    silent = len(stats) - len(shown)
    if silent:
        typer.echo(f"另有 {silent} 个信源没有反馈：样本不足，不给建议")
    changes = {st.source_id: st.suggested for st in stats if st.suggested}
    if not changes:
        typer.echo("没有 trust 调整建议")
        return
    if not apply:
        typer.echo(f"\n共 {len(changes)} 条建议；用 --apply 写入 config/sources.yaml")
        return
    target = config.CONFIG_DIR / "sources.yaml"
    if not target.exists():
        example = config.CONFIG_DIR / "sources.example.yaml"
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"{WARN} 已从 {example.name} 生成 {target}（个人配置，已在 .gitignore 中）")
    new_text, missing = apply_trust_changes(target.read_text(encoding="utf-8"), changes)
    target.write_text(new_text, encoding="utf-8")
    for source_id in missing:
        typer.echo(f"{FAIL} {source_id}：在 {target.name} 中找不到 trust 行，未修改")
    typer.echo(f"{OK} 已更新 {target}：{len(changes) - len(missing)} 个信源")


@app.command()
def deep(
    item_id: int = typer.Argument(..., help="条目 id（见 sb items list 或 Web 详情页）"),
    force: bool = typer.Option(False, "--force", help="已有精读结果时仍重新精读"),
) -> None:
    """按需精读单条：长内容自动分块 map-reduce，视频要点带时间戳链接。"""
    from secondbrain.extract.transcript import key_point_link
    from secondbrain.pipeline.run import deep_one

    config.load_env()
    settings, fs, _ = _load_pipeline_settings()
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            typer.echo(f"{FAIL} 条目不存在：{item_id}", err=True)
            raise typer.Exit(code=1)
        existing = db.item_assessment(conn, item_id, "L2")
        typer.echo(f"#{item_id} {row['title']}（{row['source_id']}）")
        if existing and not force:
            typer.echo(f"{WARN} 已有精读结果（如需重新精读加 --force）：\n")
            _print_l2(existing, row["url"], key_point_link)
            return
        backend_name = config.llm_backend(settings)
        try:
            backend = make_backend(backend_name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
        except LLMError as exc:
            typer.echo(f"{FAIL} {exc}", err=True)
            raise typer.Exit(code=1) from exc
        profile_file, _ = config.ensure_profile()
        outcome = deep_one(conn, fs, backend, load_profile(profile_file), row, datetime.now(UTC))
    finally:
        conn.close()
    typer.echo(f"预估 ${outcome.estimate:.4f}；实际估算费用 ${outcome.cost:.4f}")
    if outcome.response is None:
        typer.echo(f"{FAIL} {outcome.error}", err=True)
        raise typer.Exit(code=1)
    typer.echo("")
    _print_l2(outcome.response.model_dump(), row["url"], key_point_link)


def _print_l2(l2: dict, url: str, key_point_link) -> None:
    typer.echo(f"TL;DR：{_flat(l2['tldr'])}")
    typer.echo("要点：")
    for point in l2["key_points"]:
        text, link = key_point_link(_flat(point), url)
        typer.echo(f"  - {text}" + (f"\n    {link}" if link else ""))
    typer.echo(f"理由：{_flat(l2['why_for_you'])}")
    typer.echo(f"预计 {l2['est_minutes']} 分钟")
    if l2.get("prerequisites"):
        typer.echo(f"前置知识：{'、'.join(l2['prerequisites'])}")


def _open_wiki():
    return open_wiki(config.wiki_dir(), config.raw_dir())


@wiki_app.command("ingest")
def wiki_ingest(
    item_id: int | None = typer.Argument(None, help="要编入 wiki 的条目 id"),
    pending: bool = typer.Option(
        False, "--pending", help='编入所有当前标为"有收获"但尚未编入的条目'
    ),
    force: bool = typer.Option(False, "--force", help="已编入过的条目也重新编入"),
) -> None:
    """把条目编进 wiki：strong 模型输出受限的页面操作，由代码校验后写入 data/wiki。"""
    from secondbrain.llm.usage import record_call
    from secondbrain.wiki.ingest import ingest_item, pending_items

    if (item_id is None) == (not pending):
        raise typer.BadParameter("请给出 item_id，或使用 --pending（二选一）")
    config.load_env()
    settings, fs, _ = _load_pipeline_settings()
    wiki = _open_wiki()
    conn = db.connect()
    try:
        if pending:
            ids = pending_items(conn)
        else:
            done = db.ingested_ids(conn)
            if item_id in done and not force:
                typer.echo(f"{WARN} #{item_id} 已编入 wiki（如需重新编入加 --force）")
                return
            ids = [item_id]
        typer.echo(f"wiki：{wiki.root}（{len(wiki.pages)} 个页面）")
        if not ids:
            typer.echo('0 条待 ingest（没有标为"有收获"且尚未编入的条目）')
            return
        backend_name = config.llm_backend(settings)
        try:
            backend = make_backend(backend_name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
        except LLMError as exc:
            typer.echo(f"{FAIL} {exc}", err=True)
            raise typer.Exit(code=1) from exc
        profile_file, _ = config.ensure_profile()
        profile = load_profile(profile_file)
        total = 0.0
        failures = 0
        for iid in ids:
            row = db.item_detail(conn, iid)
            if row is None:
                typer.echo(f"{FAIL} 条目不存在：{iid}")
                failures += 1
                continue
            today = datetime.now().date()
            _, estimate, _ = ingest_item(
                conn, wiki, backend, profile, row, fs, today, estimate_only=True
            )
            typer.echo(f"#{iid} {row['title']}：预估 ${estimate:.4f}")
            if total + estimate > fs.run.max_cost_usd:
                typer.echo(f"{FAIL} 预估费用超过上限 run.max_cost_usd=${fs.run.max_cost_usd}，停止")
                failures += 1
                break
            now = datetime.now(UTC)
            try:
                result, _, _ = ingest_item(conn, wiki, backend, profile, row, fs, today)
            except LLMError as exc:
                if exc.usage:
                    total += record_call(
                        conn, backend.name, "WIKI", exc.usage, now, fs.pricing, fs.pricing_schedule
                    )
                    conn.commit()
                typer.echo(f"{FAIL} #{iid}：{exc}")
                failures += 1
                continue
            cost = record_call(
                conn, backend.name, "WIKI", result.usage, now, fs.pricing, fs.pricing_schedule
            )
            conn.commit()
            total += cost
            typer.echo(f"  新建：{', '.join(result.created) or '无'}")
            typer.echo(f"  更新：{', '.join(result.updated) or '无'}")
            for line in result.rejected:
                typer.echo(f"  {FAIL} 已拒绝：{line}")
            typer.echo(f"  费用 ${cost:.4f}")
        typer.echo(f"\n合计估算费用 ${total:.4f}；wiki 现有 {len(wiki.pages)} 个页面")
    finally:
        conn.close()
    if failures:
        raise typer.Exit(code=1)


@wiki_app.command("lint")
def wiki_lint(
    use_llm: bool = typer.Option(False, "--llm", help="另用 cheap 模型检查可能矛盾的说法"),
) -> None:
    """检查断链 / 缺失页面、孤立页、frontmatter 缺字段、index 不一致。只报告，不修改。"""
    from secondbrain.wiki.lint import lint, llm_contradictions

    wiki = _open_wiki()
    report = lint(wiki)
    typer.echo(f"wiki：{wiki.root}（{len(wiki.pages)} 个页面）")
    sections = [
        (
            "断链（指向不存在的页面）",
            [f"[[{s}]] → {', '.join(t)}" for s, t in report.broken_links.items()],
        ),
        (
            "缺失页面（被链接但不存在）",
            [f"[[{s}]]（{n} 处）" for s, n in report.missing_pages.items()],
        ),
        ("孤立页（没有其他页面链接到它）", [f"[[{s}]]" for s in report.orphans]),
        (
            "frontmatter 缺字段",
            [f"[[{s}]]：{', '.join(f)}" for s, f in report.missing_fields.items()],
        ),
        ("不在 index 中的页面", [f"[[{s}]]" for s in report.index_missing]),
        ("index 中指向不存在页面的条目", [f"[[{s}]]" for s in report.index_dangling]),
    ]
    if use_llm and wiki.pages:
        config.load_env()
        settings, fs, _ = _load_pipeline_settings()
        try:
            backend = make_backend(
                config.llm_backend(settings), fs.llm, os.environ.get("DEEPSEEK_API_KEY")
            )
            findings = llm_contradictions(wiki, backend)
            sections.append(
                (
                    "可能矛盾（LLM）",
                    [f"{', '.join(i.pages)}：{i.description}" for i in findings.issues],
                )
            )
        except LLMError as exc:
            typer.echo(f"{FAIL} LLM 检查失败：{exc}")
    for title, lines in sections:
        typer.echo(f"\n## {title}（{len(lines)}）")
        for line in lines:
            typer.echo(f"- {line}")
    typer.echo(f"\n共 {sum(len(lines) for _, lines in sections)} 项")


@wiki_app.command("due")
def wiki_due(
    day: str | None = typer.Option(None, "--date", help="日期 YYYY-MM-DD，默认今天"),
) -> None:
    """列出到期复习的概念。"""
    from datetime import date as date_cls

    from secondbrain.wiki.review import due_concepts, due_date

    try:
        target = date_cls.fromisoformat(day) if day else datetime.now().date()
    except ValueError as exc:
        raise typer.BadParameter("日期格式应为 YYYY-MM-DD") from exc
    wiki = _open_wiki()
    due = due_concepts(wiki, target)
    typer.echo(f"{target} 到期的概念：{len(due)} 个（共 {len(wiki.concepts())} 个概念页）")
    for page in due:
        typer.echo(
            f"- [[{page.slug}]] {page.title}（掌握度 {page.meta.get('mastery') or '—'}；"
            f"间隔 {page.meta.get('interval_days')} 天；到期 {due_date(page)}）"
        )
    if due:
        typer.echo(
            "复习后记录：sb wiki review <slug> remembered|fuzzy|forgot|needs_refresh|mastered"
            "（或在 Web /wiki 点按钮）"
        )


@wiki_app.command("review")
def wiki_review(
    slug: str = typer.Argument(..., help="概念页 slug"),
    grade: str = typer.Argument(
        ...,
        help="remembered（还记得）| fuzzy（模糊）| forgot（忘了）"
        "| needs_refresh（需要回炉）| mastered（已扎实）",
    ),
) -> None:
    """记录一次概念复习，按 SM-2 简化版更新间隔、ease 与掌握度。"""
    from secondbrain.wiki.review import GRADES, review

    if grade not in GRADES:
        raise typer.BadParameter("结果应为 " + " / ".join(GRADES))
    _, fs, _ = _load_pipeline_settings()
    wiki = _open_wiki()
    page = wiki.find(slug)
    if page is None or page.type != "concept":
        typer.echo(f"{FAIL} 找不到概念页：{slug}", err=True)
        raise typer.Exit(code=1)
    out = review(wiki, page, grade, datetime.now().date(), fs.wiki.mastered_interval_days)
    interval = f"间隔 {out.interval_before} → {out.interval_after} 天"
    mastery = f"掌握度 {out.mastery_before} → {out.mastery_after}"
    typer.echo(
        f"{OK} [[{out.slug}]] {GRADES[grade]}：{interval}，ease {out.ease_after}，"
        f"{mastery}，下次 {out.next_due}"
    )
