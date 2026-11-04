"""`sb` 命令行入口。"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta

import typer

from secondbrain import __version__, config, db, ingest
from secondbrain.net import HttpClient

app = typer.Typer(help="Second Brain：知识过滤 + 分级推荐 + 知识 wiki。", no_args_is_help=True)
sources_app = typer.Typer(help="信源管理。", no_args_is_help=True)
items_app = typer.Typer(help="已采集条目。", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(items_app, name="items")

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
