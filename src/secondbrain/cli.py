"""`sb` 命令行入口。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from secondbrain import __version__, config, db, ingest, schedule
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import summarize
from secondbrain.net import HttpClient
from secondbrain.profile.loader import load_profile
from secondbrain.wiki.store import open_wiki

app = typer.Typer(help="Second Brain：信源、稍后阅读列表与 AI 知识库。", no_args_is_help=True)
sources_app = typer.Typer(help="信源管理。", no_args_is_help=True)
items_app = typer.Typer(help="已采集条目。", no_args_is_help=True)
digest_app = typer.Typer(help="每日分级推荐。", no_args_is_help=True)
schedule_app = typer.Typer(help="macOS launchd 定时运行 sb run。", no_args_is_help=True)
profile_app = typer.Typer(help="用户画像（profile.md）。", no_args_is_help=True)
wiki_app = typer.Typer(help="知识 wiki（data/wiki，Obsidian 兼容）。", no_args_is_help=True)
paper_app = typer.Typer(help="按论文名检索、消歧并导入材料。", no_args_is_help=True)
reading_app = typer.Typer(help="自己维护的稍后清单与外部阅读登记。", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(items_app, name="items")
app.add_typer(digest_app, name="digest")
app.add_typer(schedule_app, name="schedule")
app.add_typer(profile_app, name="profile")
app.add_typer(wiki_app, name="wiki")
app.add_typer(paper_app, name="paper")
app.add_typer(reading_app, name="reading")

OK, FAIL, WARN = "✓", "✗", "!"


@app.callback()
def main() -> None:
    """Second Brain 命令行工具。"""


def _show_acquisition(acquisition: dict) -> None:
    if not acquisition:
        return
    status = {
        "full": "全文", "partial": "正文不完整", "abstract": "仅摘要", "missing": "未取得正文"
    }.get(acquisition.get("status"), "未知")
    typer.echo(
        f"取得内容：{status} · 提取方式：{acquisition.get('method') or '无'}"
        f" · 实际版本：{acquisition.get('version') or 'unknown'}"
    )
    typer.echo(f"实际内容来源：{acquisition.get('actual_url') or '无可核对的正文响应地址'}")
    for attempt in acquisition.get("attempts") or []:
        typer.echo(
            f"  [{attempt.get('status', 'unknown')}] {attempt.get('url', '')}"
            f" · {attempt.get('reason') or '未提供原因'}"
        )


def _paper_result(result: dict, ingest: bool, *, read: bool = False) -> None:
    typer.echo(f"{result['status']}：{result['message']}")
    _show_acquisition(result.get("acquisition") or {})
    for error in result.get("errors", []):
        typer.echo(f"! {error}", err=True)
    for error in (result.get("ingest") or {}).get("errors", []):
        typer.echo(f"wiki 整理失败：{error}", err=True)
    candidates = (
        result.get("candidates", []) if result["status"] == "ambiguous"
        else [result["selected"]] if result.get("selected") else []
    )
    for candidate in candidates:
        typer.echo(
            f"{candidate['id']}  {candidate['title']}\n"
            f"  {', '.join(candidate.get('authors', []))} · {candidate.get('year') or '?'}"
            f" · {candidate.get('venue', '')} · {candidate.get('version', '')}\n"
            f"  DOI={candidate.get('doi') or '-'} arXiv={candidate.get('arxiv_id') or '-'}"
            f" · {candidate.get('provider', '')}"
        )
        if result["status"] == "ambiguous":
            typer.echo(
                f"  sb paper choose {result['search_id']} {candidate['id']}"
                + (" --read" if read else " --ingest" if ingest else "")
            )
    if result.get("material_id"):
        item_id = result["material_id"]
        typer.echo(f"材料：/item/{item_id}")
        page = open_wiki(config.wiki_dir(), config.raw_dir()).find(f"material-{item_id}")
        if page:
            label = "机器草稿" if page.knowledge_status == "draft" else "知识页"
            typer.echo(f"{label}：/wiki/page/{page.slug}")
            if page.knowledge_status != "draft":
                typer.echo(f"局部网络：/wiki/graph?focus={page.slug}")
    if (
        result["status"] in {"failed", "not_found"}
        or (result.get("ingest") or {}).get("failures")
        or (result.get("ingest") or {}).get("errors")
    ):
        raise typer.Exit(code=1)


@paper_app.command("add")
def paper_add(
    query: str = typer.Argument(..., help="论文标题或名称"),
    ingest: bool = typer.Option(False, "--ingest", help="独立整理入 wiki，不标记已读"),
    read: bool = typer.Option(False, "--read", help="已在外部读过，登记并整理知识"),
) -> None:
    """检索论文；身份不明确时只列出候选，不自动导入。"""
    from secondbrain import ops, papers

    try:
        result = papers.import_by_name(query, typer.echo, ingest=ingest and not read)
        if read and result.get("material_id"):
            result["ingest"] = ops.register_read(result["material_id"], typer.echo)["ingest"]
    except (ValueError, ops.OpsError) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _paper_result(result, ingest, read=read)


@paper_app.command("choose")
def paper_choose(
    search_id: str,
    candidate_id: str,
    ingest: bool = typer.Option(False, "--ingest", help="独立整理入 wiki，不标记已读"),
    read: bool = typer.Option(False, "--read", help="已在外部读过，登记并整理知识"),
) -> None:
    """从已保存的检索结果中选择；不会重新搜索或接收候选 URL。"""
    from secondbrain import ops, papers

    try:
        result = papers.choose_candidate(
            search_id, candidate_id, typer.echo, ingest=ingest and not read
        )
        if read and result.get("material_id"):
            result["ingest"] = ops.register_read(result["material_id"], typer.echo)["ingest"]
    except (ValueError, ops.OpsError) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _paper_result(result, ingest, read=read)


@sources_app.command("follow")
def source_follow(
    url: str, apply: bool = typer.Option(False, "--apply", help="确认关注；默认只识别预览"),
    source_id: str | None = typer.Option(None, "--id"),
    name: str | None = typer.Option(None, "--name", help="来源显示名称"),
    candidate: str | None = typer.Option(None, "--candidate", help="预览列出的候选范围 key"),
    manual_only: bool = typer.Option(
        False, "--manual-only", help="明确仅保存主页，不表示自动识别成功"
    ),
) -> None:
    """输入普通主页，发现 RSS/HTML 列表；不可枚举时明确保存为手动浏览入口。"""
    from secondbrain.net import FetchError
    from secondbrain.source_discovery import discover_source, follow_source

    settings = config.fetch_settings(config.load_settings())
    try:
        with HttpClient(settings) as client:
            result = (
                follow_source(url, client, settings, source_id=source_id, name=name,
                              candidate_key=candidate, manual_only=manual_only)
                if apply else discover_source(url, client, settings)
            )
    except (ValueError, FetchError, config.ConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{result['title']} · {result['method']} · {result['homepage']}")
    for warning in result["warnings"]:
        typer.echo(f"! {warning}")
    for entry in result["entries"]:
        typer.echo(f"- {entry.title} · {entry.url}")
    for choice in result.get("candidates", []):
        typer.echo(
            f"候选 {choice['key']} · {choice['source'].url} · "
            f"分类：{', '.join(choice['source'].rss_categories) or '综合RSS'}"
        )
        typer.echo(choice["note"])
    if result["source"] is None or result["status"] in ("failed", "rejected", "incomplete"):
        raise typer.Exit(code=1)
    typer.echo(
        f"已关注：{result['saved_id']}" if apply
        else "仅预览；加 --apply 才保存，不自动加入阅读材料"
    )


@sources_app.command("edit")
def source_edit(
    source_id: str,
    fields: Annotated[list[str] | None, typer.Option(
        "--set", help='字段=值，可重复；列表/布尔值用 JSON，如 tags=["AI","systems"]',
    )] = None,
) -> None:
    """原位编辑来源；改 ID 同步迁移历史引用，不删除材料。"""
    from secondbrain.source_management import update_source

    changes = {}
    for field in fields or []:
        key, separator, value = field.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("--set 需要 字段=值")
        try:
            changes[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            changes[key.strip()] = value
    if not changes:
        raise typer.BadParameter("至少提供一个 --set 字段=值")
    try:
        source = update_source(source_id, changes)
    except (ValueError, config.ConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"已更新：{source.name or source.id} · {source.id}")


@sources_app.command("repair")
def source_repair(
    source_id: str | None = typer.Argument(None, help="省略则检查所有关注来源"),
    apply: bool = typer.Option(False, "--apply", help="仅修复已验证的采集字段，保留关注与人工配置"),
) -> None:
    """复用已有适配重新识别；默认只预览，不调用模型。"""
    from secondbrain.source_management import repair_source

    ids = [source_id] if source_id else [s.id for s in _load_sources_or_exit()]
    failed = False
    for source in ids:
        try:
            result = repair_source(source, typer.echo, apply=apply)
        except (ValueError, config.ConfigError) as exc:
            typer.echo(f"{source}：{exc}")
            failed = True
            continue
        typer.echo(
            f"{source} · {result['status']} · "
            f"{'已更新' if result['applied'] else '未改配置'} · "
            f"字段：{', '.join(result['changes']) or '无需更改'}"
        )
        for warning in result["warnings"]:
            typer.echo(f"! {warning}")
        failed |= result["status"] in ("failed", "rejected", "incomplete")
    if failed:
        raise typer.Exit(code=1)


@sources_app.command("analyze")
def source_analyze(
    source_id: str, force: bool = typer.Option(False, "--force", help="不复用同输入的已有分类"),
) -> None:
    """显式分析来源领域和内容性质；可能调用 cheap 模型，不覆盖人工标签。"""
    from secondbrain.source_analysis import analyze_source

    try:
        result = analyze_source(source_id, typer.echo, force=force)
    except (LLMError, ValueError, config.ConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    profile = result["profile"]
    typer.echo(f"领域：{', '.join(profile['domains']) or '证据不足'}")
    typer.echo(f"性质：{', '.join(profile['content_kinds']) or '证据不足'}")
    typer.echo(profile["summary"])
    typer.echo(f"{'复用' if result['cached'] else '新分析'} · 估算费用 ${result['cost']:.4f}")
    for evidence in profile["evidence"]:
        typer.echo(f"- {evidence['title']} · {evidence['url']}")


@reading_app.command("list")
def reading_list() -> None:
    conn = db.connect()
    try:
        rows = db.reading_list_items(conn)
        for row in rows:
            typer.echo(f"#{row['id']} {row['title']} · {row['url'] or '本地材料'}")
        if not rows:
            typer.echo("阅读列表为空；信源发现不会自动加入。")
    finally:
        conn.close()


@reading_app.command("add")
def reading_add(item_id: int) -> None:
    conn = db.connect()
    try:
        if not db.item_exists(conn, item_id):
            raise typer.BadParameter("材料不存在")
        db.add_to_reading_list(conn, item_id)
        conn.commit()
    finally:
        conn.close()
    typer.echo(f"#{item_id} 已加入阅读列表")


@reading_app.command("remove")
def reading_remove(item_id: int) -> None:
    conn = db.connect()
    try:
        db.remove_from_reading_list(conn, item_id)
        conn.commit()
    finally:
        conn.close()
    typer.echo(f"#{item_id} 已移出清单，材料和知识保留")


@reading_app.command("read")
def reading_read(
    item_id: int, notes_file: str | None = typer.Option(None, "--notes-file"),
    discussion_url: str | None = typer.Option(None, "--discussion-url"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """登记外部已读；增量整理，正文和笔记未变时不重复付费。"""
    from pathlib import Path

    from secondbrain import ops

    try:
        notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else None
        result = ops.register_read(
            item_id, typer.echo, notes=notes, discussion_url=discussion_url, force=force
        )
    except (OSError, ops.OpsError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    message = "；复用已有整理，无新增模型调用" if result["cached"] else ""
    typer.echo(f"#{item_id} 已登记阅读{message}")
    if result["ingest"]["failures"]:
        raise typer.Exit(code=1)


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
        example = config.config_dir() / name
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
    from secondbrain.source_analysis import source_contexts

    path, is_example = config.sources_path()
    sources = _load_sources_or_exit()
    label = "尚未建立关注列表，示例仅供参考" if is_example else f"正式：{path.name}"
    typer.echo(f"{len(sources)} 个已关注信源（{label}）")
    width = max(len(s.id) for s in sources) if sources else 0
    contexts = source_contexts() if sources else {}
    for s in sources:
        status = "停用" if not s.enabled else "手动浏览主页" if s.needs_scraper else "启用"
        location = s.query if s.type == "arxiv" else s.url
        typer.echo(
            f"{s.id.ljust(width)}  {'homepage' if s.needs_scraper else s.type:<9}  "
            f"{s.role:<9}  trust={s.trust}  "
            f"{status:<20}  {_truncate(location or '', 70)}"
        )
        if s.name or s.tags:
            typer.echo(f"  {s.name or s.id} · 标签：{', '.join(s.tags) or '未设置'}")
        hints = contexts.get(s.id, {})
        if hints.get("existing_topics"):
            typer.echo("  历史文章主题：" + "、".join(hints["existing_topics"]))
        elif hints.get("catalog_tags"):
            typer.echo("  已知规则标签：" + "、".join(hints["catalog_tags"]))


@app.command()
def fetch(
    source: str | None = typer.Option(None, "--source", help="只抓取该 id 的信源"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="每个信源最多新增的条目数"),
) -> None:
    """抓取信源并入库。单个信源失败不影响其他信源，也不改变返回码。"""
    from secondbrain import ops

    sources = _load_sources_or_exit()
    if source:
        sources = [s for s in sources if s.id == source]
        if not sources:
            typer.echo(f"{FAIL} 未找到信源：{source}", err=True)
            raise typer.Exit(code=1)
    elif not ops.active_sources(sources):
        typer.echo(ops.NO_SOURCES)
        return
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


@items_app.command("incomplete")
def items_incomplete() -> None:
    """列出"正文不完整"的条目：只有摘要，或手动添加的论文类正文短于 1500 字符（ADR 0020）。"""
    conn = db.connect()
    try:
        rows = db.incomplete_items(conn)
    finally:
        conn.close()
    if not rows:
        typer.echo("没有正文不完整的条目")
        return
    for r in rows:
        ids = [
            f"doi:{r['doi']}" if r["doi"] else "",
            f"arXiv:{r['arxiv_id']}" if r["arxiv_id"] else "",
        ]
        tail = "  " + " ".join(x for x in ids if x) if any(ids) else ""
        kind = f"{r['extract_method']} · {r['content_len']} 字符"
        typer.echo(f"#{r['id']}  {_truncate(r['title'], 60)}  [{kind}]{tail}")
    typer.echo(
        f"\n共 {len(rows)} 条；补全：sb add <url> --pdf FILE / --text-file FILE，"
        "或在条目详情页粘贴正文 / 上传 PDF"
    )


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


@items_app.command("show")
def item_show(item_id: int) -> None:
    """阅读材料原文，不要求推荐或机器评估。"""
    from secondbrain import ops

    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        if row is None:
            raise typer.BadParameter(f"材料不存在：{item_id}")
        typer.echo(f"# {row['title']}\n")
        typer.echo(
            f"阅读：{row['reading_state']} · 处理：{row['processing_status']}"
            f" · 完整性：{row['content_completeness']}\n"
        )
        for origin in db.material_origins(conn, item_id):
            typer.echo(f"来源：{origin['kind']} · {origin['locator']}")
            metadata = json.loads(origin["metadata_json"])
            if metadata:
                typer.echo(
                    f"  请求版本：{metadata.get('requested_version') or '未指定'}"
                    f"；实际版本：{metadata.get('actual_version') or '未知'}"
                    f"；实际地址：{metadata.get('actual_url') or '未获得正文'}"
                )
        typer.echo("\n" + (row["original_text"] or row["content"]))
        notes = ops.material_notes(item_id)
        if notes:
            typer.echo("\n## 个人笔记\n\n" + notes)
    finally:
        conn.close()


@items_app.command("read-state")
def item_read_state(item_id: int, state: str) -> None:
    """设置 unread / reading / read；不改变推荐或机器处理状态。"""
    if state not in {"unread", "reading", "read"}:
        raise typer.BadParameter("阅读状态必须为 unread / reading / read")
    if state == "read":
        reading_read(item_id, None, None, False)
        return
    conn = db.connect()
    try:
        if not db.item_exists(conn, item_id):
            raise typer.BadParameter(f"材料不存在：{item_id}")
        db.set_reading_state(conn, item_id, state)
    finally:
        conn.close()
    typer.echo(f"#{item_id} 阅读状态：{state}")


@items_app.command("notes")
def item_notes(item_id: int, file: str | None = typer.Option(None, "--file")) -> None:
    """查看个人笔记；--file 从文件保存笔记到 vault。"""
    from pathlib import Path

    from secondbrain import ops

    try:
        text = Path(file).read_text(encoding="utf-8") if file else None
        typer.echo(ops.material_notes(item_id, text))
    except (OSError, ops.OpsError) as exc:
        raise typer.BadParameter(str(exc)) from exc


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
    all_time: bool = typer.Option(
        False, "--all-time", help="不限时间窗，只去重（每个信源仍只取最新 fetch_limit 条）"
    ),
) -> None:
    """抓取 → L0 规则 → L1 粗筛 → L2 精读 → 分级，写入当天 digest。

    与 Web /ops 的"立即运行"、定时任务共用一把锁（data/run.lock），已在运行时直接退出。"""
    from secondbrain import ops

    try:
        report = ops.run_once(typer.echo, limit=limit, no_fetch=no_fetch, all_time=all_time)
    except ops.OpsError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if report.aborted:
        raise typer.Exit(code=1)


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _lane_line(position: int, row, l1: dict, l2: dict | None) -> list[str]:
    deep = " · 深读" if row["is_deep_read"] else ""
    minutes = f"{row['est_minutes']} 分钟"
    if row["budget"] < row["est_minutes"]:
        minutes = f"速读约 {row['budget']} 分钟 · 全文约 {row['est_minutes']} 分钟"
    deferred = f" · 顺延自 {row['deferred_from']}" if row["deferred_from"] else ""
    if row["extract_method"] == "no_transcript":
        deep += " · 无字幕"
    if row["is_backfill"]:
        deep += " · 经典回补"
    lines = [
        f"{position}. {_flat(row['title'])}",
        f"   {row['source_id']} · {minutes}{deep}{deferred}",
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
    used = sum(r["budget"] for r in rows if r["lane"] in TOP_LANES)
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
    uvicorn.run(create_app(extra_hosts=(host,)), host=host, port=port, log_level="info")


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
    schedule.install(local, config.PROJECT_ROOT, log_dir, runner=subprocess.run)
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
    if not schedule.uninstall(runner=subprocess.run):
        typer.echo(f"{WARN} 未安装：{target} 不存在")
        return
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
def sources_tune() -> None:
    """只读的信源统计：反馈分布、近期产出与低产出停用建议（ADR 0018）。
    反馈不再用来调整 trust；trust 请在 config/sources.yaml 或 Web /sources 中手动设置。"""
    from secondbrain.profile.tune import DISTRIBUTION_KINDS, feedback_distribution, source_yields

    _, fs, _ = _load_pipeline_settings()
    sources = _load_sources_or_exit()
    pers = fs.personalize
    conn = db.connect()
    try:
        dist = feedback_distribution(conn)
        yields = source_yields(conn, sources, datetime.now(UTC), pers)
    finally:
        conn.close()
    if not sources:
        typer.echo("还没有信源，去 /sources 添加")
        return
    labels = "  ".join(db.FEEDBACK_KINDS[k] for k in DISTRIBUTION_KINDS)
    width = max(len(s.id) for s in sources)
    typer.echo(
        f"反馈分布（各条目当前状态：{labels}）与近 {pers.yield_window_days} 天产出；只供参考"
    )
    for s in sources:
        counts = dist.get(s.id, {})
        shown = " ".join(f"{counts.get(k, 0):>3}" for k in DISTRIBUTION_KINDS)
        y = yields[s.id]
        typer.echo(
            f"{s.id.ljust(width)}  trust {s.trust}  反馈 {shown}  "
            f"入库 {y.items:>3} 精读 {y.l2:>3} 进入推荐 {y.front:>3}"
        )
    low = [y for y in yields.values() if y.low_yield]
    typer.echo(
        f"\n低产出信源（近 {pers.yield_window_days} 天入库 ≥ {pers.low_yield_min_items} 条、"
        f"进入推荐 0 条）：{len(low)} 个"
    )
    for y in low:
        typer.echo(
            f"{WARN} {y.source_id}：入库 {y.items}，精读 {y.l2}，进入推荐 0 —— 建议停用"
            "（在 Web /sources 一键停用，或改 config/sources.yaml）"
        )


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


@app.command()
def add(
    urls: Annotated[list[str] | None, typer.Argument(help="文章 URL；本地 PDF/文本可省略")] = None,
    read: bool = typer.Option(False, "--read/--later", help="登记已读并整理；默认加入稍后清单"),
    title: str | None = typer.Option(None, "--title", help="本地材料标题"),
    feedback: str | None = typer.Option(
        None, "--feedback", help="读后反馈（仅 --read）：gained | too_easy | too_hard | worthless"
    ),
    ingest: bool = typer.Option(False, "--ingest", help="导入后整理入 wiki，不改变阅读状态"),
    text_file: str | None = typer.Option(None, "--text-file", help="独立文本或单个 URL 的正文"),
    pdf_file: str | None = typer.Option(None, "--pdf", help="独立 PDF 或单个 URL 的原文件，≤30MB"),
    reference_only: bool = typer.Option(False, "--reference", help="仅保存网页引用，不抓取正文"),
    notes_file: str | None = typer.Option(None, "--notes-file", help="已读材料的外部学习笔记"),
    discussion_url: str | None = typer.Option(None, "--discussion-url"),
    refetch: bool = typer.Option(
        False, "--refetch", help="已存在的条目也重新抓取（仅在拿到更完整正文时替换）"
    ),
) -> None:
    """导入 URL、本地 PDF 或文本到材料库；不要求信源、推荐或读后反馈。"""
    from pathlib import Path

    from secondbrain import ops

    try:
        text = Path(text_file).read_text(encoding="utf-8") if text_file else None
        pdf = Path(pdf_file).read_bytes() if pdf_file else None
        learning_notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else None
    except OSError as exc:
        raise typer.BadParameter(f"无法读取材料文件：{exc}") from exc
    try:
        out = ops.add_manual(
            urls or [],
            typer.echo,
            state="read" if read else "later",
            title=title,
            feedback=feedback,
            ingest=ingest,
            text=text,
            pdf=pdf,
            refetch=refetch,
            reference_only=reference_only, learning_notes=learning_notes,
            discussion_url=discussion_url,
        )
    except ops.OpsError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc
    results = out["results"]
    for result in results:
        _show_acquisition(result.get("acquisition") or {})
    counts = {
        k: sum(r["status"].startswith(k) for r in results) for k in ("新增", "已存在", "失败")
    }
    typer.echo("合计：" + "，".join(f"{k} {v}" for k, v in counts.items()))
    if out["ingest"]:
        typer.echo(f"编入 wiki 费用 ${out['ingest']['cost']:.4f}")
    if counts["失败"] or (out["ingest"] and out["ingest"]["failures"]):
        raise typer.Exit(code=1)


@app.command()
def summary(
    item_id: int = typer.Argument(..., help="条目 id"),
    force: bool = typer.Option(False, "--force", help="已有深度速读时重新生成"),
) -> None:
    """深度速读：读全文（长内容分块提要后汇总），输出方法、结果、局限、前置知识等。"""
    from secondbrain import ops
    from secondbrain.extract.transcript import key_point_link

    try:
        out = ops.deep_summary(item_id, typer.echo, force=force)
    except ops.OpsError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc
    conn = db.connect()
    try:
        url = db.item_detail(conn, item_id)["url"]
    finally:
        conn.close()
    d = out.data
    typer.echo(f"\n## 问题与动机\n{_flat(d['problem'])}")
    for title, key in (
        ("核心方法", "method"),
        ("关键结果", "results"),
        ("局限与争议", "limitations"),
    ):
        typer.echo(f"\n## {title}")
        for point in d[key]:
            text, link = key_point_link(_flat(point), url)
            typer.echo(f"- {text}" + (f"  {link}" if link else ""))
    typer.echo(f"\n## 与已有知识的关系\n{_flat(d['relation_to_known'])}")
    typer.echo("\n## 值得精读的章节")
    for s in d["sections_to_read"] or [{"name": "（无）", "reason": ""}]:
        typer.echo(f"- {s['name']}" + (f"：{_flat(s['reason'])}" if s["reason"] else ""))
    typer.echo("\n## 需要补的前置知识")
    for p in d["prerequisites"] or [{"name": "（无）", "why": ""}]:
        typer.echo(f"- {p['name']}" + (f"：{_flat(p['why'])}" if p["why"] else ""))
    note = "（缓存）" if out.cached else f"（本次费用 ${out.cost:.4f}）"
    typer.echo(f"\n生成于 {d['_created_at']} · {d['_model']}{note}")


@app.command()
def prereq(
    item_id: int = typer.Argument(..., help="条目 id"),
    force: bool = typer.Option(False, "--force", help="重新生成 AI 讲解"),
) -> None:
    """补前置：列出该条目的前置知识；wiki 已有的给出页面与掌握度，没有的生成简短讲解。"""
    from secondbrain import ops

    try:
        report = ops.prerequisites(item_id, typer.echo, force=force)
    except ops.OpsError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for e in report.entries:
        typer.echo(f"\n## {e.name}" + (f"（{_flat(e.why)}）" if e.why else ""))
        if e.page_slug:
            typer.echo(
                f"{OK} wiki 已有：[[{e.page_slug}]] {e.page_title}（掌握度 {e.mastery or '—'}）"
            )
        elif e.explanation:
            typer.echo(f"AI 讲解（AI 生成）：{_flat(e.explanation)}")
        else:
            typer.echo(f"{WARN} 没有生成讲解")
        for x in e.explainers:
            typer.echo(f"  讲解类条目 #{x['id']} {x['title']}  {x['url']}")
    typer.echo(
        f"\n共 {len(report.entries)} 个前置知识；"
        f"本次新生成 {report.generated} 条讲解，费用 ${report.cost:.4f}"
    )


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


@wiki_app.command("reindex")
def wiki_reindex() -> None:
    """从同一 vault 的 Markdown 重建目录；图谱与反向链接实时派生。"""
    from secondbrain.wiki.graph import build_graph

    wiki = _open_wiki()
    wiki.rebuild_index()
    graph = build_graph(wiki, datetime.now().date())
    typer.echo(f"已重新索引：{len(graph['nodes'])} 页，{len(graph['edges'])} 条页面链接")


@wiki_app.command("show")
def wiki_show(name: str) -> None:
    """打开知识页正文、关联与反向链接；同名时要求使用稳定 ID。"""
    from secondbrain.wiki.graph import page_connections

    wiki = _open_wiki()
    candidates = wiki.resolve(name)
    if len(candidates) != 1:
        choices = "、".join(page.slug for page in candidates) or "无匹配页面"
        raise typer.BadParameter(f"无法唯一定位 {name}：{choices}")
    page = candidates[0]
    typer.echo(page.body)
    connections = page_connections(wiki, page)
    for key, label in (("outgoing", "关联"), ("backlinks", "反向链接")):
        typer.echo(f"\n{label}：" + "、".join(p.slug for p in connections[key]))
    for key, label in (("missing", "缺失"), ("ambiguous", "歧义")):
        if connections[key]:
            typer.echo(f"{label}：" + "、".join(connections[key]))


@wiki_app.command("graph")
def wiki_graph(
    focus: str | None = typer.Option(None, "--focus", help="以名称或稳定 ID 为中心"),
    depth: int = typer.Option(1, "--depth", min=1, help="局部网络的直接邻居展开层数"),
    drafts: bool = typer.Option(False, "--drafts", help="额外显示未登记阅读的机器草稿"),
) -> None:
    """列出全局或局部网络；普通连线仅表示 Markdown 页面链接。"""
    from secondbrain.wiki.graph import build_graph

    wiki = _open_wiki()
    graph = build_graph(wiki, datetime.now().date(), include_drafts=drafts)
    selected = {node["id"] for node in graph["nodes"]}
    if focus:
        candidates = wiki.resolve(focus)
        if len(candidates) != 1:
            choices = "、".join(page.slug for page in candidates) or "无匹配页面"
            raise typer.BadParameter(f"无法唯一定位 {focus}：{choices}")
        if candidates[0].slug not in selected:
            raise typer.BadParameter("这是机器草稿；先登记阅读，或显式加 --drafts 查看")
        selected = {candidates[0].slug}
        adjacent = {node["id"]: set() for node in graph["nodes"]}
        for edge in graph["edges"]:
            adjacent[edge["source"]].add(edge["target"])
            adjacent[edge["target"]].add(edge["source"])
        frontier = selected.copy()
        for _ in range(depth):
            frontier = {neighbor for node in frontier for neighbor in adjacent[node]} - selected
            selected.update(frontier)
            if not frontier:
                break
    for node in graph["nodes"]:
        if node["id"] in selected:
            typer.echo(f"{node['id']} · {node['label']} · {node['url']}")
    for edge in graph["edges"]:
        if edge["source"] in selected and edge["target"] in selected:
            typer.echo(f"[[{edge['source']}]] — [[{edge['target']}]]")
    if not selected:
        typer.echo("知识网络为空；可先导入材料并整理入 wiki。")


@wiki_app.command("ingest")
def wiki_ingest(
    item_id: int | None = typer.Argument(None, help="要编入 wiki 的条目 id"),
    pending: bool = typer.Option(
        False, "--pending", help='编入所有当前标为"有收获"但尚未编入的条目'
    ),
    force: bool = typer.Option(False, "--force", help="已编入过的条目也重新编入"),
) -> None:
    """把条目编进 wiki：strong 模型输出受限的页面操作，由代码校验后写入 data/wiki。"""
    from secondbrain import ops
    from secondbrain.wiki.ingest import pending_items

    if (item_id is None) == (not pending):
        raise typer.BadParameter("请给出 item_id，或使用 --pending（二选一）")
    wiki = _open_wiki()
    conn = db.connect()
    try:
        if pending:
            ids = pending_items(conn)
        else:
            done = db.ingested_ids(conn)
            row = db.item_detail(conn, item_id)
            if item_id in done and row["processing_status"] == "processed" and not force:
                typer.echo(f"{WARN} #{item_id} 已编入 wiki（如需重新编入加 --force）")
                return
            ids = [item_id]
        typer.echo(f"wiki：{wiki.root}（{len(wiki.pages)} 个页面）")
        if not ids:
            typer.echo('0 条待 ingest（没有标为"有收获"且尚未编入的条目）')
            return
    finally:
        conn.close()
    try:
        summary = ops.ingest_items(ids, typer.echo, force=force)
    except ops.OpsError as exc:
        typer.echo(f"{FAIL} {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"\n合计估算费用 ${summary.cost:.4f}；wiki 现有 {summary.pages} 个页面")
    if summary.failures:
        raise typer.Exit(code=1)


@wiki_app.command("lint")
def wiki_lint(
    use_llm: bool = typer.Option(False, "--llm", help="另用 cheap 模型检查可能矛盾的说法"),
) -> None:
    """检查断链 / 缺失页面、孤立页、frontmatter 缺字段、index 不一致。只报告，不修改。"""
    from secondbrain.wiki.lint import lint, llm_contradictions, report_sections

    wiki = _open_wiki()
    report = lint(wiki)
    typer.echo(f"wiki：{wiki.root}（{len(wiki.pages)} 个页面）")
    sections = report_sections(report)
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
    if page is None or page.type != "concept" or page.knowledge_status == "draft":
        typer.echo(f"{FAIL} 找不到概念页：{slug}", err=True)
        raise typer.Exit(code=1)
    out = review(wiki, page, grade, datetime.now().date(), fs.wiki.mastered_interval_days)
    interval = f"间隔 {out.interval_before} → {out.interval_after} 天"
    mastery = f"掌握度 {out.mastery_before} → {out.mastery_after}"
    typer.echo(
        f"{OK} [[{out.slug}]] {GRADES[grade]}：{interval}，ease {out.ease_after}，"
        f"{mastery}，下次 {out.next_due}"
    )


@wiki_app.command("relation")
def wiki_relation(
    source: str, target: str, mode: str,
    reason: str = typer.Option("", "--reason", help="人工关联依据或禁止原因"),
) -> None:
    """pin / block / reset 人工关联；不调用模型，后续 AI 必须遵守。"""
    from secondbrain.wiki.relations import set_relation
    from secondbrain.wiki.store import WikiError

    try:
        result = set_relation(_open_wiki(), source, target, mode, reason)
    except WikiError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{result['source']} ↔ {result['target']}：{result['mode']} · {result['reason']}")
