"""项目路径、.env 与 settings 的读取。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_LLM_BACKEND = "deepseek"
KNOWN_LLM_BACKENDS = ("deepseek", "fake")


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
