"""线程后台任务；可选 SQLite 持久记录，重启不自动重试中断的工作。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LINES = 200


def _result_status(result: dict) -> str:
    if result.get("status") == "ambiguous":
        return "waiting"
    if result.get("status") in {"failed", "not_found"}:
        return result["status"]
    outcomes = result.get("results") or []
    failures = sum(r.get("status") in {"失败", "failed"} for r in outcomes)
    incomplete = result.get("failures") or (result.get("ingest") or {}).get("failures")
    if failures and failures == len(outcomes):
        return "failed"
    return "partial" if failures or incomplete else "done"


class TaskRunning(Exception):
    """同一类任务已在运行。"""


@dataclass
class Task:
    kind: str
    title: str
    started: datetime
    status: str = "running"  # running | waiting | done | partial | failed | interrupted | resolved
    finished: datetime | None = None
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    thread: threading.Thread | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dismissed: bool = False
    _persist: Callable[[Task], None] | None = field(default=None, repr=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished or datetime.now(UTC)
        return int((end - self.started).total_seconds())

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            del self.lines[:-MAX_LINES]
            self.save()

    def save(self) -> None:
        """显式保存中间结果，避免后续知识整理失败时丢失已导入材料。"""
        if self._persist is not None:
            self._persist(self)


class TaskRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._records: dict[str, Task] = {}
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
                )
                rows = conn.execute("SELECT record FROM tasks").fetchall()
            for (record,) in rows:
                data = json.loads(record)
                data["started"] = datetime.fromisoformat(data["started"])
                if data["finished"]:
                    data["finished"] = datetime.fromisoformat(data["finished"])
                task = Task(**data, _persist=self._save)
                if task.running:
                    task.status = "interrupted"
                    task.finished = datetime.now(UTC)
                    task.error = "服务重启，任务已中断；未自动重试，请检查已保存结果后重新提交。"
                    task.save()
                self._remember(task)

    def _remember(self, task: Task) -> None:
        self._records[task.id] = task
        current = self._tasks.get(task.kind)
        if current is None or task.started >= current.started:
            self._tasks[task.kind] = task

    def _save(self, task: Task) -> None:
        if self.path is None:
            return
        with task._lock:
            record = {
                "id": task.id, "kind": task.kind, "title": task.title,
                "started": task.started.isoformat(), "status": task.status,
                "finished": task.finished.isoformat() if task.finished else None,
                "lines": task.lines, "result": task.result, "error": task.error,
                "dismissed": task.dismissed,
            }
            payload = json.dumps(record, ensure_ascii=False)
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (task.id, payload))

    def get(self, kind: str) -> Task | None:
        with self._lock:
            return self._tasks.get(kind) or self._records.get(kind)

    def records(self, *, include_dismissed: bool = False) -> list[Task]:
        with self._lock:
            records = [
                task for task in self._records.values()
                if include_dismissed or not task.dismissed
            ]
        return sorted(records, key=lambda task: task.started, reverse=True)

    def dismiss(self, task_id: str) -> bool:
        with self._lock:
            task = self._records.get(task_id)
        if task is None or task.running or task.status == "waiting":
            return False
        with task._lock:
            task.dismissed = True
            task.save()
        return True

    def recover_papers(self, path: Path) -> None:
        """只读旧论文会话与尝试；原表不写、不删，候选仍交给原选择入口。"""
        if not path.is_file():
            return
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"paper_searches", "paper_import_attempts"} <= tables:
                return
            attempts = conn.execute("SELECT * FROM paper_import_attempts").fetchall()
            searches = conn.execute(
                "SELECT * FROM paper_searches WHERE id NOT IN"
                " (SELECT search_id FROM paper_import_attempts)"
            ).fetchall()
        known_searches = {
            t.result.get("search_id") for t in self._records.values()
            if not t.id.startswith("legacy-")
        }

        def paper_key(result: dict) -> tuple:
            return (
                result.get("search_id"), (result.get("selected") or {}).get("id"),
                result.get("status"), result.get("material_id"),
            )

        known_results = {
            paper_key(task.result) for task in self._records.values()
            if not task.id.startswith("legacy-")
        }
        for row in attempts:
            result = json.loads(row["result_json"])
            if paper_key(result) not in known_results:
                self._recover_paper(f"legacy-attempt-{row['id']}", row["created_at"], result)
        for row in searches:
            if row["id"] in known_searches:
                continue
            candidates = json.loads(row["candidates_json"])
            self._recover_paper(f"legacy-search-{row['id']}", row["created_at"], {
                "search_id": row["id"], "query": row["query"], "candidates": candidates,
                "errors": json.loads(row["errors_json"]), "material_id": None,
                "selected": None, "status": "ambiguous" if candidates else "not_found",
                "message": (
                    "已恢复历史检索；请选择论文与版本。" if candidates else "历史检索无候选。"
                ),
            })
        for task in self.records(include_dismissed=True):
            self._resolve_paper_waiters(task)

    def _recover_paper(self, task_id: str, stamp: str, result: dict) -> None:
        if task_id in self._records:
            return
        started = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        task = Task(
            kind=f"paper-{task_id}", id=task_id, title=f"历史论文：{result.get('query', '')}",
            started=started, finished=started, result=result,
            status=_result_status(result),
            _persist=self._save,
        )
        self._remember(task)
        task.save()

    def _resolve_paper_waiters(self, completed: Task) -> None:
        """成功导入后旧检索只留作历史；失败选择仍保留候选供用户重试。"""
        search_id = completed.result.get("search_id")
        if not search_id or not completed.result.get("material_id"):
            return
        for task in self.records(include_dismissed=True):
            if (
                task.id != completed.id and task.status == "waiting"
                and task.result.get("search_id") == search_id
            ):
                with task._lock:
                    task.status = "resolved"
                    task.result = dict(task.result, resolved_by=completed.id)
                    task.save()

    def start(
        self, kind: str, title: str, fn: Callable[[Task], dict | None],
        *, initial_result: dict | None = None,
    ) -> Task:
        """在后台线程执行 fn(task)；fn 的返回值存为 task.result，异常存为 task.error。"""
        with self._lock:
            current = self._tasks.get(kind)
            if current is not None and current.running:
                raise TaskRunning(f"「{current.title}」正在运行（已 {current.elapsed_seconds} 秒）")
            task = Task(
                kind=kind, title=title, started=datetime.now(UTC), _persist=self._save,
                result=dict(initial_result or {}),
            )
            task.save()
            self._remember(task)

        def target() -> None:
            try:
                result = fn(task) or {}
                with task._lock:
                    task.result = result
                    task.status = _result_status(result)
            except Exception as exc:  # 任务中的任何异常都显示在页面上，不影响服务
                with task._lock:
                    task.error = str(exc) or type(exc).__name__
                    task.status = "failed"
            finally:
                with task._lock:
                    task.finished = datetime.now(UTC)
                    task.save()
            self._resolve_paper_waiters(task)

        task.thread = threading.Thread(target=target, name=f"sb-task-{kind}", daemon=True)
        task.thread.start()
        return task
