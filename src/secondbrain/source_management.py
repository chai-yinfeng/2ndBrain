"""来源安全编辑、采集配置修复与参考目录显示设置。"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from secondbrain import config, db, sources_file
from secondbrain.models import SourceConfig
from secondbrain.net import HttpClient
from secondbrain.source_discovery import discover_source

_thread_lock = threading.RLock()
_lock_state = threading.local()
_REPAIR_FIELDS = (
    "type", "url", "query", "url_pattern", "max_links", "use_lastmod", "needs_scraper",
    "rss_categories", "scope_note",
)


@contextmanager
def source_lock() -> Iterator[None]:
    """来源配置共用的可重入线程锁与跨进程 flock；异常或退出自动释放。"""
    with _thread_lock:
        if getattr(_lock_state, "held", False):
            yield
            return
        path = config.config_dir() / "sources.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _lock_state.held = True
            try:
                yield
            finally:
                _lock_state.held = False
                fcntl.flock(handle, fcntl.LOCK_UN)


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _rename_metadata(value, old: str, new: str):
    if isinstance(value, list):
        return [_rename_metadata(item, old, new) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"source_id", "origin_source_id"} and item == old:
            result[key] = new
        elif key == "source_ids" and isinstance(item, list):
            result[key] = [new if identity == old else identity for identity in item]
        else:
            result[key] = _rename_metadata(item, old, new)
    return result


def _rename_source(conn: sqlite3.Connection, old: str, new: str) -> None:
    if conn.execute("SELECT 1 FROM sources WHERE id = ?", (new,)).fetchone():
        raise config.ConfigError(f"信源 id 已存在（含历史来源）：{new}")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute("UPDATE sources SET id = ? WHERE id = ?", (new, old))
    # Older material tables use NO ACTION; newer analyses use ON UPDATE CASCADE.
    # Update every actual foreign key, including references added by later migrations.
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )]
    for table in tables:
        for reference in conn.execute(f"PRAGMA foreign_key_list({_identifier(table)})"):
            if reference[2] == "sources" and reference[4] == "id":
                column = _identifier(reference[3])
                conn.execute(
                    f"UPDATE {_identifier(table)} SET {column} = ? WHERE {column} = ?",
                    (new, old),
                )
    for row in conn.execute("SELECT id, metadata_json FROM item_sources"):
        metadata = json.loads(row["metadata_json"])
        updated = _rename_metadata(metadata, old, new)
        if updated != metadata:
            conn.execute(
                "UPDATE item_sources SET metadata_json = ? WHERE id = ?",
                (json.dumps(updated, ensure_ascii=False), row["id"]),
            )


def _store_source(conn, source: SourceConfig, fields: dict) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
    exists = conn.execute("SELECT 1 FROM sources WHERE id = ?", (source.id,)).fetchone()
    values = source.model_dump() if not exists else {
        key: getattr(source, key) for key in fields
    }
    values = {key: value for key, value in values.items() if key in columns and key != "id"}
    if "tags" in values:
        values["tags"] = json.dumps(values["tags"], ensure_ascii=False)
    values["updated_at"] = db.format_time(datetime.now(UTC))
    if exists:
        assignments = ", ".join(f"{_identifier(key)} = ?" for key in values)
        conn.execute(
            f"UPDATE sources SET {assignments} WHERE id = ?", (*values.values(), source.id)
        )
    else:
        values = {"id": source.id, **values}
        names = ", ".join(_identifier(key) for key in values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO sources ({names}) VALUES ({placeholders})", tuple(values.values())
        )


def update_source(source_id: str, changes: dict) -> SourceConfig:
    """仅修改显式字段；身份原位迁移，YAML 写入或数据库失败时恢复原配置。"""
    with source_lock():
        path = config.config_dir() / "sources.yaml"
        if not path.is_file():
            raise config.ConfigError(f"找不到信源：{source_id}")
        original = path.read_text(encoding="utf-8")
        updated_text = sources_file.update_entry(original, source_id, changes)
        identity = changes.get("id", source_id)
        updated = next(s for s in config.parse_sources_text(updated_text) if s.id == identity)
        if updated_text == original:
            return updated
        conn = db.connect()
        attempted_write = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            if updated.id != source_id:
                _rename_source(conn, source_id, updated.id)
            _store_source(conn, updated, changes)
            # Fail FK checks before exposing the new YAML; COMMIT still remains fallible.
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise config.ConfigError("信源编辑违反材料或历史记录的引用约束")
            attempted_write = True
            sources_file.write(updated_text)
            conn.commit()
        except Exception:
            conn.rollback()
            if attempted_write:
                config._atomic_write(path, original)
            raise
        finally:
            conn.close()
        return updated


def repair_source(
    source_id: str, echo: Callable[[str], None], *, apply: bool = False
) -> dict:
    """显式联网预览；apply 才修复采集字段，绝不迁移身份或覆盖个人偏好。"""
    with source_lock():
        source = next((s for s in config.load_sources() if s.id == source_id), None)
        if source is None:
            raise config.ConfigError(f"找不到信源：{source_id}")
    location = source.homepage or source.url
    if not location:
        message = "信源没有可用于发现的主页或采集地址"
        echo(message)
        return {
            "source_id": source_id, "status": "failed", "applied": False,
            "changes": {}, "warnings": [message], "discovery": None,
        }
    settings = config.fetch_settings(config.load_settings())
    with HttpClient(settings) as client:
        discovery = discover_source(location, client, settings)
    candidate = discovery["source"]
    status = discovery["status"]
    usable = status in {"ready", "empty"} and candidate is not None and not candidate.needs_scraper
    changes = {}
    applied = False
    if usable:
        # Re-read under the same lock used by all editors. Discovery must not overwrite
        # a name or preferences changed while its network request was in flight.
        with source_lock():
            current = next((s for s in config.load_sources() if s.id == source_id), None)
            if current is None:
                raise config.ConfigError(f"找不到信源：{source_id}")
            if any(
                getattr(current, key) != getattr(source, key)
                for key in (*_REPAIR_FIELDS, "homepage")
            ):
                raise config.ConfigError("检查期间采集配置已改变，请重新检查；未写入旧结果")
            changes = {
                key: getattr(candidate, key)
                for key in _REPAIR_FIELDS
                if getattr(candidate, key) != getattr(current, key)
            }
            discovered_name = candidate.name or discovery["title"].strip()
            if not current.name and discovered_name:
                changes["name"] = discovered_name
            if apply:
                update_source(source_id, changes)
                applied = True
        echo(f"{'已修复' if applied else '修复预览'}：{source_id}（{candidate.type}）")
    else:
        echo(f"未修复：{source_id}（{status}）")
    for warning in discovery["warnings"]:
        echo(warning)
    return {
        "source_id": source_id, "status": status, "applied": applied,
        "changes": changes, "warnings": discovery["warnings"], "discovery": discovery,
    }


def catalog_hidden_ids() -> set[str]:
    conn = db.connect()
    try:
        return {row[0] for row in conn.execute("SELECT source_id FROM source_catalog_hidden")}
    finally:
        conn.close()


def _catalog_ids() -> set[str]:
    path = config.config_dir() / "sources.example.yaml"
    return {source.id for source in config.load_sources(path)} if path.is_file() else set()


def hide_catalog(source_id: str) -> None:
    with source_lock():
        if source_id not in _catalog_ids():
            raise config.ConfigError(f"参考目录中找不到信源：{source_id}")
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO source_catalog_hidden(source_id) VALUES (?)",
                    (source_id,),
                )
        finally:
            conn.close()


def clear_catalog() -> None:
    """隐藏当前完整目录；不修改关注配置、参考文件或任何来源/材料记录。"""
    with source_lock():
        identities = _catalog_ids()
        conn = db.connect()
        try:
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO source_catalog_hidden(source_id) VALUES (?)",
                    [(identity,) for identity in identities],
                )
        finally:
            conn.close()


def restore_catalog() -> None:
    with source_lock():
        conn = db.connect()
        try:
            with conn:
                conn.execute("DELETE FROM source_catalog_hidden")
        finally:
            conn.close()
