"""macOS launchd 定时运行：生成 / 安装 / 卸载 `sb run` 的 LaunchAgent。

时间按本机本地时区解释；是否落在 DeepSeek 高峰时段按 UTC 判断（冬令时与夏令时都检查）。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from secondbrain.models import PricingSchedule

LABEL = "com.secondbrain.run"
# 默认本地 07:00：美东冬令时为 UTC 12:00、夏令时为 UTC 11:00，都不在高峰时段
DEFAULT_TIME = "07:00"


def launch_agents_dir() -> Path:
    """SB_LAUNCH_AGENTS_DIR 可覆盖（测试与验收用，避免碰真实的 ~/Library/LaunchAgents）。"""
    override = os.environ.get("SB_LAUNCH_AGENTS_DIR")
    return Path(override) if override else Path.home() / "Library" / "LaunchAgents"


def plist_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir or launch_agents_dir()) / f"{LABEL}.plist"


def parse_time(value: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except ValueError as exc:
        raise ValueError(f"时间格式应为 HH:MM：{value!r}") from exc


@dataclass(frozen=True)
class UtcCheck:
    label: str  # 冬令时 / 夏令时
    utc: datetime
    peak: bool


def utc_checks(local: time, schedule: PricingSchedule, year: int | None = None) -> list[UtcCheck]:
    """把本地时间分别放在 1 月与 7 月的一个周三，换算成 UTC 并判断是否高峰。
    （按工作日检查，周末本就不是高峰。）"""
    year = year or datetime.now().year
    checks = []
    seen: set[int] = set()
    for label, month, day in (("冬令时", 1, 14), ("夏令时", 7, 15)):
        moment = datetime(year, month, day, local.hour, local.minute)
        while moment.weekday() != 2:  # 周三
            moment = moment.replace(day=moment.day + 1)
        utc = moment.astimezone().astimezone(UTC)  # naive → 本机时区规则（含夏令时）
        offset = int(moment.astimezone().utcoffset().total_seconds())
        if offset in seen:  # 本机时区无夏令时：只报一次
            continue
        seen.add(offset)
        hour_ok = utc.weekday() in schedule.peak_weekdays_utc
        peak = hour_ok and any(s <= utc.hour < e for s, e in schedule.peak_hours_utc)
        checks.append(UtcCheck(label, utc, peak))
    return checks


def build_plist(local: time, project_root: Path, log_dir: Path, uv: str) -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "sb", "run"],
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": {"Hour": local.hour, "Minute": local.minute},
        "StandardOutPath": str(log_dir / "run.log"),
        "StandardErrorPath": str(log_dir / "run.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": False,
    }
    return plistlib.dumps(payload)


def find_uv() -> str:
    return shutil.which("uv") or "uv"


# ---------- 运行方式（ADR 0016）：装了 LaunchAgent = 每日自动，没装 = 手动 ----------

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    path: Path
    at: time | None = None  # 每天运行的本地时间
    loaded: bool | None = None  # launchctl 是否已加载（未安装时为 None）


def status(runner: Runner | None = None) -> ScheduleStatus:
    """读 plist 得到时间；只有 plist 存在时才调用 `launchctl list` 判断是否已加载。"""
    runner = runner or subprocess.run
    path = plist_path()
    if not path.exists():
        return ScheduleStatus(False, path)
    try:
        interval = plistlib.loads(path.read_bytes()).get("StartCalendarInterval") or {}
        at = time(int(interval["Hour"]), int(interval["Minute"]))
    except (plistlib.InvalidFileException, KeyError, ValueError, TypeError):
        at = None
    result = runner(["launchctl", "list", LABEL], capture_output=True, check=False)
    return ScheduleStatus(True, path, at, result.returncode == 0)


def install(local: time, project_root: Path, log_dir: Path, runner: Runner | None = None) -> Path:
    """写入 plist 并 `launchctl load -w`（与 `sb schedule install` 相同）。"""
    runner = runner or subprocess.run
    target = plist_path()
    log_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_plist(local, project_root, log_dir, find_uv()))
    runner(["launchctl", "load", "-w", str(target)], check=True)
    return target


def uninstall(runner: Runner | None = None) -> bool:
    """`launchctl unload -w` 并删除 plist；未安装时返回 False。
    注意：unload 会终止该 LaunchAgent 正在进行的运行。"""
    runner = runner or subprocess.run
    target = plist_path()
    if not target.exists():
        return False
    runner(["launchctl", "unload", "-w", str(target)], check=False)
    target.unlink()
    return True
