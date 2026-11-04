"""`sb` 命令行入口。"""

from __future__ import annotations

import os
import sys
import uuid

import typer

from secondbrain import __version__, config

app = typer.Typer(help="Second Brain：知识过滤 + 分级推荐 + 知识 wiki。", no_args_is_help=True)

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
