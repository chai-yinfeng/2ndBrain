"""项目路径、.env 与 settings 的读取。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from secondbrain.models import FetchSettings, FilterSettings, SourceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_LLM_BACKEND = "deepseek"
KNOWN_LLM_BACKENDS = ("deepseek", "fake")


class ConfigError(Exception):
    """配置文件缺失或不合法。"""


def data_dir() -> Path:
    """数据目录：优先 SB_DATA_DIR，否则 <项目根>/data。"""
    override = os.environ.get("SB_DATA_DIR")
    return Path(override).expanduser() if override else PROJECT_ROOT / "data"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析简单的 KEY=VALUE 格式；忽略空行、# 注释，去掉成对引号。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> None:
    """把 .env 中的值写入 os.environ；已存在的环境变量优先，不覆盖。"""
    for key, value in parse_env_file(path or ENV_FILE).items():
        os.environ.setdefault(key, value)


def settings_path() -> tuple[Path, bool]:
    """返回 (settings 文件路径, 是否为 example)。正式文件不存在时退回 example。"""
    real = CONFIG_DIR / "settings.yaml"
    if real.is_file():
        return real, False
    return CONFIG_DIR / "settings.example.yaml", True


def load_settings() -> dict[str, Any]:
    path, _ = settings_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 mapping")
    return data


def llm_backend(settings: dict[str, Any] | None = None) -> str:
    """当前 LLM 后端：SB_LLM_BACKEND > settings.llm.backend > 默认 deepseek。"""
    env = os.environ.get("SB_LLM_BACKEND")
    if env:
        return env
    llm = (settings or {}).get("llm") or {}
    return llm.get("backend") or DEFAULT_LLM_BACKEND


def sources_path() -> tuple[Path, bool]:
    """返回 (sources 文件路径, 是否为 example)。正式文件不存在时退回 example。"""
    real = CONFIG_DIR / "sources.yaml"
    if real.is_file():
        return real, False
    return CONFIG_DIR / "sources.example.yaml", True


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    """读取并校验信源清单；id 必须唯一。"""
    path = path or sources_path()[0]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 {path}：{exc}") from exc
    raw = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ConfigError(f"{path} 需要顶层 `sources:` 列表")
    sources: list[SourceConfig] = []
    for index, entry in enumerate(raw):
        try:
            sources.append(SourceConfig.model_validate(entry))
        except ValidationError as exc:
            label = entry.get("id") if isinstance(entry, dict) else None
            raise ConfigError(f"{path} 第 {index + 1} 个信源（{label}）不合法：{exc}") from exc
    seen: set[str] = set()
    for s in sources:
        if s.id in seen:
            raise ConfigError(f"{path} 中信源 id 重复：{s.id}")
        seen.add(s.id)
    return sources


SOURCES_HEADER = (
    "# 个人信源配置（gitignore）。由 Web /sources 写回时注释不保留；\n"
    "# 各字段说明见 config/sources.example.yaml。\n"
)
# 写回时省略取默认值的这些字段，让文件保持简短
_OMIT_DEFAULTS = ("backfill", "needs_scraper", "use_lastmod")


def source_to_dict(source: SourceConfig) -> dict[str, Any]:
    data = source.model_dump(exclude_none=True)
    for key in _OMIT_DEFAULTS:
        if data.get(key) is False:
            del data[key]
    return data


def save_sources(sources: list[SourceConfig], path: Path) -> None:
    """校验 id 唯一后原子写入（临时文件 + rename）。PyYAML 写回不保留注释（ADR 0013）。"""
    ids = [s.id for s in sources]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"信源 id 重复：{', '.join(duplicates)}")
    body = yaml.safe_dump(
        {"sources": [source_to_dict(s) for s in sources]}, allow_unicode=True, sort_keys=False
    )
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(SOURCES_HEADER + body, encoding="utf-8")
    os.replace(tmp, path)


def fetch_settings(settings: dict[str, Any]) -> FetchSettings:
    try:
        return FetchSettings.model_validate(settings.get("fetch") or {})
    except ValidationError as exc:
        raise ConfigError(f"settings 中 fetch 段不合法：{exc}") from exc


def pipeline_settings(settings: dict[str, Any]) -> FilterSettings:
    """把 settings 顶层的 llm / pricing / 预算 / lanes 与 `filter:` 段组装成 FilterSettings。"""
    keys = (
        "llm",
        "pricing",
        "pricing_schedule",
        "daily_budget_minutes",
        "lanes",
        "lane_budget_reserve",
        "deep_read_ratio",
        "deep_summary_minutes",
        "backlog_max_days",
        "run",
        "personalize",
        "backfill",
        "wiki",
    )
    data = {k: settings[k] for k in keys if settings.get(k) is not None}
    data.update(settings.get("filter") or {})
    try:
        return FilterSettings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"settings 不合法：{exc}") from exc


def profile_path() -> Path:
    return data_dir() / "profile.md"


def ensure_profile() -> tuple[Path, bool]:
    """返回 (profile 路径, 是否刚从 example 复制)。已存在时绝不覆盖。"""
    path = profile_path()
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (CONFIG_DIR / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return path, True


def wiki_dir() -> Path:
    return data_dir() / "wiki"


def raw_dir() -> Path:
    """原文目录 data/wiki/raw：用 Obsidian 打开 data/wiki 时链接可点（ADR 0013）。"""
    return wiki_dir() / "raw"
