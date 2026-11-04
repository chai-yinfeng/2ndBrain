"""Web 后台任务：进程内的简单注册表（线程），同一类任务同时只跑一个（ADR 0013）。

页面用 HTMX 每 2 秒轮询任务状态片段；任务只保存在内存里，服务重启后清空。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_LINES = 200


class TaskRunning(Exception):
    """同一类任务已在运行。"""


@dataclass
class Task:
    kind: str
    title: str
    started: datetime
    status: str = "running"  # running | done | failed
    finished: datetime | None = None
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished or datetime.now(UTC)
        return int((end - self.started).total_seconds())

    def log(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-MAX_LINES]


class TaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    def get(self, kind: str) -> Task | None:
        return self._tasks.get(kind)

    def start(self, kind: str, title: str, fn: Callable[[Task], dict | None]) -> Task:
        """在后台线程执行 fn(task)；fn 的返回值存为 task.result，异常存为 task.error。"""
        with self._lock:
            current = self._tasks.get(kind)
            if current is not None and current.running:
                raise TaskRunning(f"「{current.title}」正在运行（已 {current.elapsed_seconds} 秒）")
            task = Task(kind=kind, title=title, started=datetime.now(UTC))
            self._tasks[kind] = task

        def target() -> None:
            try:
                task.result = fn(task) or {}
                task.status = "done"
            except Exception as exc:  # 任务中的任何异常都显示在页面上，不影响服务
                task.error = str(exc) or type(exc).__name__
                task.status = "failed"
            finally:
                task.finished = datetime.now(UTC)

        task.thread = threading.Thread(target=target, name=f"sb-task-{kind}", daemon=True)
        task.thread.start()
        return task
