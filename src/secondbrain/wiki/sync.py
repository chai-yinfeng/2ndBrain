"""材料变更先预览再同步；回收只管理来源元数据，不删除个人知识。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager

from secondbrain import config, db
from secondbrain.ops import OpsError, ingest_items, run_lock
from secondbrain.wiki.ingest import material_input_hash
from secondbrain.wiki.store import Page, Wiki, WikiError


class SyncConflict(OpsError):
    """预览后的材料或 wiki 已变化，必须重新预览。"""


@contextmanager
def _read_connection():
    path = db.db_path()
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _notes(wiki: Wiki, item_id: int) -> str:
    path = wiki.root / "notes" / f"material-{item_id}-notes.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _sources(page: Page) -> set[int]:
    sources = {value for value in page.meta.get("sources") or [] if type(value) is int}
    material_id = page.meta.get("material_id")
    if type(material_id) is int:
        sources.add(material_id)
    return sources


def _marker(page: Page, states: dict, item_ids: set[int]) -> dict | None:
    if page.meta.get("generated_by") != "wiki_ingest":
        return None
    if type(page.meta.get("merged_into")) is int:
        return {"status": "withdrawn", "merged_into": page.meta["merged_into"]}
    sources = _sources(page)
    removed = sorted(iid for iid in sources if states.get(iid, {}).get("trashed"))
    if not removed:
        return None
    if page.type == "material" and page.meta.get("material_id") in removed:
        status = "withdrawn"
    elif any(iid in item_ids and not states.get(iid, {}).get("trashed") for iid in sources):
        status = "review"
    else:
        status = "unsupported"
    return {"status": status, "removed_sources": removed}


def _snapshot(conn, wiki: Wiki) -> tuple[dict, dict]:
    states = db.lifecycle_states(conn) if conn is not None else {}
    aliases = {}
    if conn is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'material_aliases'"
    ).fetchone():
        aliases = dict(conn.execute("SELECT alias_id, canonical_id FROM material_aliases"))
    rows = {
        row["id"]: db.item_detail(conn, row["id"])
        for row in conn.execute("SELECT id FROM items ORDER BY id") if row["id"] not in aliases
    } if conn is not None else {}
    inputs = {}
    changes = []
    item_ids = set(rows)
    for iid, row in rows.items():
        notes = _notes(wiki, iid)
        digest = material_input_hash(row, notes)
        state = states.get(iid, {})
        material = wiki.material_page(iid)
        compiled = (
            material is not None and material.meta.get("input_hash") == digest
            and not (row["reading_state"] == "read" and material.knowledge_status == "draft")
            and row["processing_status"] == "processed"
        )
        needs_ingest = (
            row["reading_state"] == "read" and not compiled and not state.get("trashed")
            and not (material is not None and material.is_trashed)
        )
        lifecycle_pending = state.get("revision", 0) != state.get("synced_revision", 0)
        inputs[iid] = {
            "hash": digest, "reading_state": row["reading_state"],
            "processing_status": row["processing_status"],
            "origins": [dict(origin) for origin in db.material_origins(conn, iid)],
        }
        pages = []
        for page in sorted(wiki.pages.values(), key=lambda p: p.slug):
            if (page.is_trashed or iid not in _sources(page)
                    or page.meta.get("generated_by") != "wiki_ingest"):
                continue
            marker = _marker(page, states, item_ids)
            if marker != page.meta.get("material_sync"):
                pages.append({"slug": page.slug, "title": page.title,
                              "effect": marker["status"] if marker else "restored"})
        if not lifecycle_pending and not needs_ingest:
            # 元数据写失败也必须可重试；已有生命周期任务的共享页由该任务负责。
            pages = [
                entry for entry in pages
                if not any(
                    states.get(source, {}).get("revision", 0)
                    != states.get(source, {}).get("synced_revision", 0)
                    for source in _sources(wiki.pages[entry["slug"]])
                )
            ]
            if not pages:
                continue
        action = (
            "withdraw" if state.get("trashed") else
            "ingest" if needs_ingest else "restore"
        )
        changes.append({
            "item_id": iid, "title": row["title"], "action": action,
            "reason": (
                "材料已回收" if state.get("trashed") else
                "已登记材料的正文或学习笔记需要整理" if needs_ingest else
                "材料状态需要同步" if lifecycle_pending else "来源状态需要重新同步"
            ),
            "pages": pages, "needs_ingest": needs_ingest,
            "revision": state.get("revision", 0),
        })
    files = {
        path.relative_to(wiki.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(wiki.root.rglob("*.md")) if path.is_file()
    }
    snapshot = {
        "inputs": inputs, "states": states, "aliases": aliases, "files": files, "changes": changes,
    }
    token = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")).hexdigest()
    return {"token": token, "changes": changes}, snapshot


def preview_sync() -> dict:
    """只读预览；不初始化 vault、不迁移数据库、不调用模型或网络。"""
    wiki = Wiki(root=config.wiki_dir(), raw_root=config.raw_dir())
    wiki.reload_pages()
    with _read_connection() as conn:
        preview, _ = _snapshot(conn, wiki)
    return preview


def _same_revision(conn, iid: int, expected: dict) -> bool:
    return db.lifecycle_states(conn).get(iid, {}) == expected


def apply_sync(token: str, echo: Callable[[str], None]) -> dict:
    """验证预览后同步；付费整理共用既有服务的单次费用上限。"""
    result = {
        "created": [], "updated": [], "withdrawn": [], "review": [], "restored": [],
        "cost": 0.0, "failures": 0, "errors": [],
    }
    with run_lock():
        wiki = Wiki(root=config.wiki_dir(), raw_root=config.raw_dir())
        wiki.reload_pages()
        with _read_connection() as reader:
            preview, snapshot = _snapshot(reader, wiki)
        if not token or token != preview["token"]:
            raise SyncConflict("材料、学习笔记或 wiki 已变化，请重新预览知识同步")
        if not preview["changes"]:
            return result
        conn = db.connect()
        ready = []
        failed = set()

        def failure(iid, exc):
            message = f"#{iid}：{exc}"
            failed.add(iid)
            result["failures"] += 1
            result["errors"].append(message)
            echo(message)

        try:
            for change in preview["changes"]:
                iid = change["item_id"]
                expected = snapshot["states"].get(iid, {})
                try:
                    if not _same_revision(conn, iid, expected):
                        raise SyncConflict("材料状态已变化，请重新预览")
                    row = db.item_detail(conn, iid)
                    if row is None or (
                        material_input_hash(row, _notes(wiki, iid))
                        != snapshot["inputs"][iid]["hash"]
                    ):
                        raise SyncConflict("材料或学习笔记已变化，请重新预览")
                    for entry in change["pages"]:
                        page = wiki.pages[entry["slug"]]
                        marker = _marker(page, snapshot["states"], set(snapshot["inputs"]))
                        previous = page.meta.get("material_sync")
                        if marker == previous:
                            continue
                        if marker is None:
                            page.meta.pop("material_sync", None)
                        else:
                            page.meta["material_sync"] = marker
                        relative = page.path.relative_to(wiki.root).as_posix()
                        try:
                            wiki.save_metadata(page, expected_hash=snapshot["files"][relative])
                        except (OSError, WikiError):
                            if previous is None:
                                page.meta.pop("material_sync", None)
                            else:
                                page.meta["material_sync"] = previous
                            raise
                        category = "restored" if marker is None else (
                            "withdrawn" if marker["status"] == "withdrawn" else "review"
                        )
                        if page.slug not in result[category]:
                            result[category].append(page.slug)
                    ready.append(change)
                except (OSError, ValueError, OpsError, WikiError) as exc:
                    failure(iid, exc)
            if ready:
                wiki.rebuild_index()
            compile_ids = [change["item_id"] for change in ready if change["needs_ingest"]]
            if compile_ids:
                try:
                    summary = ingest_items(compile_ids, echo)
                except (OSError, ValueError, OpsError, WikiError) as exc:
                    for iid in compile_ids:
                        failure(iid, exc)
                else:
                    for key in ("created", "updated"):
                        result[key] = getattr(summary, key)
                    result["cost"] = summary.cost
                    result["failures"] += summary.failures
                    result["errors"].extend(summary.errors)
                wiki.reload_pages()
                # 新来源可能让既有 unsupported 机器页恢复为 review。
                states = db.lifecycle_states(conn)
                current_ids = {
                    row[0] for row in conn.execute(
                        "SELECT id FROM items WHERE id NOT IN "
                        "(SELECT alias_id FROM material_aliases)"
                    )
                }
                for page in wiki.pages.values():
                    affected = _sources(page).intersection(compile_ids)
                    if (page.is_trashed or page.meta.get("generated_by") != "wiki_ingest"
                            or not affected):
                        continue
                    marker = _marker(page, states, current_ids)
                    if marker == page.meta.get("material_sync"):
                        continue
                    if marker is None:
                        page.meta.pop("material_sync", None)
                    else:
                        page.meta["material_sync"] = marker
                    try:
                        wiki.save_metadata(page)
                    except (OSError, WikiError) as exc:
                        for iid in affected:
                            failure(iid, exc)
                wiki.reload_pages()
                wiki.rebuild_index()
            for change in ready:
                iid = change["item_id"]
                if iid in failed:
                    continue
                expected = snapshot["states"].get(iid, {})
                if not _same_revision(conn, iid, expected):
                    failure(iid, SyncConflict("同步期间材料状态变化，仍保留待同步"))
                    continue
                row = db.item_detail(conn, iid)
                digest = material_input_hash(row, _notes(wiki, iid)) if row is not None else None
                if digest != snapshot["inputs"][iid]["hash"]:
                    failure(iid, SyncConflict("同步期间材料或笔记变化，仍保留待同步"))
                    continue
                if change["needs_ingest"]:
                    material = wiki.material_page(iid)
                    if (material is None or material.meta.get("input_hash") != digest
                            or row["processing_status"] != "processed"):
                        continue
                if expected and not db.acknowledge_lifecycle(conn, iid, change["revision"]):
                    failure(iid, SyncConflict("材料状态版本已变化，仍保留待同步"))
                conn.commit()
        finally:
            conn.close()
    return result
