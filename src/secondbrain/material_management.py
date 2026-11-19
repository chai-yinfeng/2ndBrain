"""材料列表与可恢复生命周期；不改阅读、来源、个人笔记或 wiki。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from secondbrain import db
from secondbrain.ops import OpsError


def list_materials(
    query: str = "",
    reading: str = "",
    processing: str = "",
    completeness: str = "",
    view: str = "active",
    offset: int = 0,
) -> list[sqlite3.Row]:
    """每页返回最多 51 行，调用方用第 51 行判断下一页。"""
    if reading not in ("", "unread", "reading", "read"):
        raise OpsError("未知阅读状态")
    if processing not in ("", "pending", "processed"):
        raise OpsError("未知整理状态")
    if completeness not in ("", "missing", "abstract", "partial", "full"):
        raise OpsError("未知正文完整度")
    if view not in ("active", "hidden", "trash", "all"):
        raise OpsError("未知材料视图")
    if not isinstance(offset, int) or offset < 0:
        raise OpsError("分页位置必须为非负整数")
    clauses, params = ["i.id NOT IN (SELECT alias_id FROM material_aliases)"], []
    query = query.strip()
    if query:
        clauses.append("(i.title LIKE ? OR i.author LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    for column, value in (
        ("reading_state", reading),
        ("processing_status", processing),
        ("content_completeness", completeness),
    ):
        if value:
            clauses.append(f"i.{column} = ?")
            params.append(value)
    if view == "active":
        clauses.extend(["COALESCE(m.hidden, 0) = 0", "COALESCE(m.trashed, 0) = 0"])
    elif view == "hidden":
        clauses.extend(["m.hidden = 1", "m.trashed = 0"])
    elif view == "trash":
        clauses.append("m.trashed = 1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT i.*, COALESCE(m.hidden, 0) AS is_hidden,"
            " COALESCE(m.trashed, 0) AS is_trashed FROM items i"
            " LEFT JOIN material_lifecycle m ON m.item_id = i.id" + where
            + " ORDER BY i.id DESC LIMIT 51 OFFSET ?",
            [*params, offset],
        ).fetchall()
    finally:
        conn.close()


def _manage(conn: sqlite3.Connection, ids: list[int], action: str) -> dict:
    if action not in ("hide", "unhide", "trash", "restore"):
        raise OpsError("未知材料操作")
    if any(type(item_id) is not int or item_id <= 0 for item_id in ids):
        raise OpsError("材料 ID 必须为正整数")
    ids = list(dict.fromkeys(ids))
    rows = {}
    # 先验证整个批次，再写入，任何不存在的 ID 均不会导致部分修改。
    for item_id in ids:
        try:
            db.require_canonical(conn, item_id)
        except ValueError as exc:
            raise OpsError(str(exc)) from exc
        row = conn.execute(
            """SELECT COALESCE(m.hidden, 0) AS hidden, COALESCE(m.trashed, 0) AS trashed,
                      COALESCE(m.revision, 0) AS revision
               FROM items i LEFT JOIN material_lifecycle m ON m.item_id = i.id
               WHERE i.id = ?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise OpsError(f"材料不存在：{item_id}")
        rows[item_id] = row
    result = {"changed": [], "unchanged": []}
    now = db.format_time(datetime.now(UTC))
    for item_id, row in rows.items():
        hidden, trashed = row["hidden"], row["trashed"]
        if action == "restore":
            hidden, trashed = 0, 0
        elif action == "trash":
            trashed = 1
        elif not trashed:
            hidden = int(action == "hide")
        if (hidden, trashed) == (row["hidden"], row["trashed"]):
            result["unchanged"].append(item_id)
            continue
        revision = row["revision"] + int(trashed != row["trashed"])
        conn.execute(
            """INSERT INTO material_lifecycle (item_id, hidden, trashed, revision, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET hidden = excluded.hidden,
                   trashed = excluded.trashed, revision = excluded.revision,
                   updated_at = excluded.updated_at""",
            (item_id, hidden, trashed, revision, now),
        )
        result["changed"].append(item_id)
    return result


def manage_materials(ids: list[int], action: str = "hide") -> dict:
    """原子批量操作；仅回收/恢复的真实切换产生待同步的 wiki revision。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _manage(conn, ids, action)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_to_hide(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.*, 0 AS is_hidden, 0 AS is_trashed
           FROM items i LEFT JOIN material_lifecycle m ON m.item_id = i.id
           WHERE i.reading_state = 'read'
             AND i.id NOT IN (SELECT alias_id FROM material_aliases)
             AND COALESCE(m.hidden, 0) = 0 AND COALESCE(m.trashed, 0) = 0
           ORDER BY i.id"""
    ).fetchall()


def read_to_hide() -> list[sqlite3.Row]:
    """只读预览默认列表中将被隐藏的全部已读材料，不限一页。"""
    conn = db.connect()
    try:
        return _read_to_hide(conn)
    finally:
        conn.close()


def hide_read() -> dict:
    """隐藏默认列表中的全部已读材料；已读与知识状态不变。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = [row["id"] for row in _read_to_hide(conn)]
        result = _manage(conn, ids, "hide")
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
