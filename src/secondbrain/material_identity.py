"""Evidence-based local identities and checkpointed, recoverable duplicate merges.

History remains on its original IDs. Redirects are append-only; no original,
notes, knowledge body or human relation is deleted or rewritten.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from secondbrain import config, db
from secondbrain.bibliography import compare_bibliography


def resolve_existing(conn, bibliography: dict) -> int | None:
    matches = set()
    for row in conn.execute("SELECT id FROM items WHERE id NOT IN "
                            "(SELECT alias_id FROM material_aliases)"):
        compared = compare_bibliography(bibliography, db.material_bibliography(conn, row[0]))
        if compared["match"]:
            if db.is_trashed(conn, row[0]):
                raise ValueError(f"材料 #{row[0]} 已回收，请先恢复后再导入或补全")
            matches.add(row[0])
        elif compared["conflicts"] and {"doi", "arxiv_id"} & set(compared["evidence"]):
            raise ValueError("材料身份冲突：" + "、".join(compared["conflicts"]))
    if len(matches) > 1:
        raise ValueError("材料身份待确认：书目匹配不唯一")
    return next(iter(matches), None)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".merge-tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _journal() -> Path:
    return config.data_dir() / "material-merge-journal.json"


def _recover(conn) -> dict | None:
    path = _journal()
    if not path.exists():
        return None
    plan = json.loads(path.read_text())
    committed = conn.execute("SELECT 1 FROM material_merges WHERE token = ?",
                             (plan["token"],)).fetchone()
    if not committed:
        # External edits during a crashed merge are never overwritten. Keep the
        # durable journal, stop, and let a person resolve the conflicting file.
        for change in plan["files"]:
            target = Path(change["path"])
            current = target.read_bytes() if target.exists() else None
            before = base64.b64decode(change["before"]) if change["before"] is not None else None
            after = base64.b64decode(change["after"])
            if current not in (before, after):
                raise ValueError(f"归并恢复遇到外部编辑：{target}；保留journal，请人工处理")
        for change in reversed(plan["files"]):
            target = Path(change["path"])
            if change["before"] is None:
                target.unlink(missing_ok=True)
            else:
                _atomic(target, base64.b64decode(change["before"]))
    path.unlink()
    return {"token": plan["token"], "committed": bool(committed),
            "action": "completed" if committed else "rolled_back"}


def _file_change(path: Path, after: bytes) -> dict:
    before = path.read_bytes() if path.exists() else None
    return {
        "path": str(path),
        "before": base64.b64encode(before).decode() if before is not None else None,
        "after": base64.b64encode(after).decode(),
    }


def _bibliography(conn, item_id: int) -> dict:
    facts = db.material_bibliography(conn, item_id)
    # Legacy URL-placeholder PDFs have no persisted bibliography. Read their
    # existing original without network or modifying the original/source row.
    if not facts.get("title") or not facts.get("authors"):
        from secondbrain.extract.pdf import extract_pdf_text

        paths = [row["original_path"] for row in db.material_origins(conn, item_id)]
        for relative in paths:
            if not relative or not relative.endswith(".pdf"):
                continue
            path = (config.wiki_dir() / relative).resolve()
            if not path.is_relative_to(config.wiki_dir().resolve()) or not path.is_file():
                continue
            metadata = {}
            extract_pdf_text(path.read_bytes(), 1, metadata=metadata)
            extracted = metadata.get("bibliography", {})
            if extracted:
                facts = {**facts, **{k: v for k, v in extracted.items() if v}}
                break
    return facts


def _plan(conn, canonical_id: int, duplicate_id: int) -> dict:
    from secondbrain.wiki.store import open_wiki

    if canonical_id == duplicate_id:
        raise ValueError("主材料与重复材料必须不同")
    for item_id in (canonical_id, duplicate_id):
        db.require_canonical(conn, item_id)
        if not db.item_exists(conn, item_id) or db.is_trashed(conn, item_id):
            raise ValueError(f"材料 #{item_id} 不存在或已回收")
    if conn.execute("SELECT 1 FROM material_aliases WHERE canonical_id = ?",
                    (duplicate_id,)).fetchone():
        raise ValueError("重复材料已有归并历史，请先人工审阅；不改写已有ID映射")
    facts = [_bibliography(conn, iid) for iid in (canonical_id, duplicate_id)]
    # Explicit stored IDs cannot be replaced by a PDF's different identifier.
    for iid, extracted in zip((canonical_id, duplicate_id), facts, strict=True):
        stored = db.material_bibliography(conn, iid)
        conflict = compare_bibliography(stored, extracted)["conflicts"]
        if conflict:
            raise ValueError("材料书目冲突：" + "、".join(conflict))
    compared = compare_bibliography(*facts)
    origins = [db.material_origins(conn, iid) for iid in (canonical_id, duplicate_id)]
    for key in ("pdf_hash", "text_hash", "canonical"):
        left = {row[key] for row in origins[0] if row[key]}
        right = {row[key] for row in origins[1] if row[key]}
        if left & right:
            if key == "text_hash":
                # Partial extraction fingerprints alone cannot prove identity.
                full_left = {
                    r["text_hash"] for r in origins[0] if r["text_hash"] and not r["truncated"]
                }
                full_right = {
                    r["text_hash"] for r in origins[1] if r["text_hash"] and not r["truncated"]
                }
                if not full_left & full_right:
                    continue
            compared["evidence"].append(key)
            if not compared["conflicts"]:
                compared["match"] = True
    if not compared["match"]:
        reason = "、".join(compared["conflicts"]) or "缺少唯一身份依据"
        raise ValueError("材料不能自动归并：" + reason)
    # A bibliographic match must also be unique across the active local corpus.
    if not {"doi", "arxiv_id", "pdf_hash", "text_hash", "canonical"} & set(compared["evidence"]):
        for row in conn.execute("SELECT id FROM items WHERE id NOT IN "
                                "(SELECT alias_id FROM material_aliases)"):
            if row[0] not in (canonical_id, duplicate_id) and not db.is_trashed(conn, row[0]):
                if compare_bibliography(facts[1], _bibliography(conn, row[0]))["match"]:
                    raise ValueError("材料不能自动归并：书目身份不唯一")
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    files, checkpoints = [], {}
    notes = [wiki.root / "notes" / f"material-{iid}-notes.md"
             for iid in (canonical_id, duplicate_id)]
    contents = [path.read_bytes() if path.exists() else b"" for path in notes]
    for records in origins:
        for origin in records:
            for key in ("original_path", "text_path", "raw_path"):
                if origin[key]:
                    path = (wiki.root / origin[key]).resolve()
                    if not path.is_relative_to(wiki.root.resolve()):
                        raise ValueError("材料原件路径不在知识库中")
                    checkpoints[str(path)] = _hash(path.read_bytes()) if path.is_file() else None
    for path, content in zip(notes, contents, strict=True):
        checkpoints[str(path)] = _hash(content) if path.exists() else None
    if all(content.strip() for content in contents) and contents[0] != contents[1]:
        raise ValueError("两份材料均有不同的非空个人笔记，请人工处理后再归并")
    if contents[1].strip() and not contents[0].strip():
        files.append(_file_change(notes[0], contents[1]))
    for page in wiki.pages.values():
        path = page.path
        if path is None:
            continue
        checkpoints[str(path)] = _hash(path.read_bytes())
        if page.meta.get("generated_by") != "wiki_ingest":
            continue
        sources = page.meta.get("sources") or []
        old_material = page.meta.get("material_id") == duplicate_id
        if duplicate_id not in sources and not old_material:
            continue
        meta = dict(page.meta)
        meta["sources"] = list(dict.fromkeys(
            canonical_id if value == duplicate_id else value for value in sources
        ))
        if old_material:
            meta["merged_into"] = canonical_id
            meta["material_sync"] = {"status": "withdrawn", "merged_into": canonical_id}
        original = path.read_bytes()
        front = re.match(br"\A---\r?\n.*?\r?\n---(?=\r?\n|\Z)", original, re.S)
        if front is None:
            raise ValueError(f"机器页缺少frontmatter：{path}")
        # Byte-for-byte body suffix retention, including human additions.
        header = (
            "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip() + "\n---"
        )
        files.append(_file_change(path, header.encode() + original[front.end():]))
    snapshot = {}
    for table in ("items", "item_sources", "feedback", "assessments", "digests", "wiki_ingests",
                  "item_ai", "reading_list", "material_lifecycle"):
        column = "id" if table == "items" else "item_id"
        snapshot[table] = [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE {column} IN (?, ?) ORDER BY rowid",
            (canonical_id, duplicate_id))]
    # Global identity/redirect changes invalidate the uniqueness proof.
    snapshot["identities"] = [dict(row) for row in conn.execute(
        "SELECT id, title, author, doi, arxiv_id, published_at FROM items ORDER BY id")]
    snapshot["aliases"] = [
        dict(row) for row in conn.execute("SELECT * FROM material_aliases ORDER BY alias_id")
    ]
    plan = {"canonical_id": canonical_id, "duplicate_id": duplicate_id,
            "evidence": compared["evidence"], "bibliography": facts,
            "snapshot": snapshot, "files": files, "checkpoints": checkpoints}
    plan["token"] = _hash(_json(plan).encode())
    return plan


def preview_merge(canonical_id: int, duplicate_id: int) -> dict:
    from secondbrain.ops import run_lock

    with run_lock():
        conn = db.connect()
        try:
            recovery = _recover(conn)
            conn.execute("BEGIN")
            plan = _plan(conn, canonical_id, duplicate_id)
            if recovery:
                plan["recovery"] = recovery
            return {key: value for key, value in plan.items() if key not in ("snapshot", "files")}
        finally:
            conn.close()


def apply_merge(canonical_id: int, duplicate_id: int, token: str, echo=lambda _: None) -> dict:
    from secondbrain.ops import run_lock
    from secondbrain.wiki.store import WikiError, open_wiki

    with run_lock():
        conn = db.connect()
        try:
            _recover(conn)
            conn.execute("BEGIN IMMEDIATE")
            plan = _plan(conn, canonical_id, duplicate_id)
            if token != plan["token"]:
                raise ValueError("归并预览已过期，请重新预览")
            _atomic(_journal(), _json(plan).encode())
            now = db.format_time(datetime.now(UTC))
            rows = plan["snapshot"]["items"]
            primary = next(row for row in rows if row["id"] == canonical_id)
            duplicate = next(row for row in rows if row["id"] == duplicate_id)
            ranks = {"missing": 0, "abstract": 1, "partial": 2, "full": 3}
            improved = (
                ranks[duplicate["content_completeness"]] > ranks[primary["content_completeness"]]
            )
            if improved:
                fields = ("content", "original_text", "extract_method", "content_hash", "text_hash",
                          "content_completeness", "content_truncated", "resource_path")
                conn.execute("UPDATE items SET " + ", ".join(f"{key} = ?" for key in fields)
                             + ", processing_status = 'pending' WHERE id = ?",
                             [*(duplicate[key] for key in fields), canonical_id])
            facts = plan["bibliography"][1]
            primary_facts = plan["bibliography"][0]
            published = primary["published_at"]
            if not published or (
                primary_facts.get("preprint_year") and not primary_facts.get("year")
                and facts.get("year")
            ):
                published = (
                    db.format_time(datetime(int(facts["year"]), 1, 1, tzinfo=UTC))
                    if facts.get("year") else duplicate["published_at"]
                )
            if (not primary["title"] or primary["title"] == primary["url"]
                    or primary["title"].startswith(("http://", "https://", "urn:"))):
                custom = any(json.loads(row["metadata_json"]).get("display_title_custom")
                             for row in db.material_origins(conn, canonical_id))
                if not custom and facts.get("title"):
                    conn.execute(
                        "UPDATE items SET title = ? WHERE id = ?", (facts["title"], canonical_id)
                    )
            conn.execute(
                "UPDATE items SET author = COALESCE(NULLIF(author, ''), ?), "
                "doi = COALESCE(doi, ?), arxiv_id = COALESCE(arxiv_id, ?), "
                "published_at = ?, processing_status = 'pending' "
                "WHERE id = ?",
                (duplicate["author"] or ", ".join(facts.get("authors", [])) or None,
                 duplicate["doi"] or facts.get("doi"),
                 duplicate["arxiv_id"] or facts.get("arxiv_id"),
                 published, canonical_id),
            )
            conn.execute(
                """INSERT INTO material_lifecycle (item_id, revision, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       revision = material_lifecycle.revision + 1,
                       updated_at = excluded.updated_at""",
                (canonical_id, now),
            )
            if duplicate["reading_state"] == "read" or primary["reading_state"] == "read":
                conn.execute(
                    "UPDATE items SET reading_state = 'read', "
                    "read_at = COALESCE(read_at, ?) WHERE id = ?",
                    (duplicate["read_at"] or now, canonical_id),
                )
            elif duplicate["reading_state"] == "reading" and primary["reading_state"] == "unread":
                conn.execute(
                    "UPDATE items SET reading_state = 'reading' WHERE id = ?", (canonical_id,)
                )
            if conn.execute(
                "SELECT 1 FROM reading_list WHERE item_id = ?", (duplicate_id,)
            ).fetchone():
                conn.execute("INSERT OR IGNORE INTO reading_list (item_id, added_at) VALUES (?, ?)",
                             (canonical_id, now))
            for origin in db.material_origins(conn, duplicate_id):
                metadata = json.loads(origin["metadata_json"])
                metadata["original_item_id"] = duplicate_id
                if not metadata.get("bibliography"):
                    metadata["bibliography"] = facts
                conn.execute("UPDATE item_sources SET item_id = ?, metadata_json = ? WHERE id = ?",
                             (canonical_id, _json(metadata), origin["id"]))
            conn.execute(
                "INSERT INTO material_aliases VALUES (?, ?, ?)", (duplicate_id, canonical_id, now)
            )
            conn.execute("INSERT INTO material_merges VALUES (?, ?, ?, ?, ?, ?)",
                         (token, canonical_id, duplicate_id, _json(plan["evidence"]),
                          _json(plan["snapshot"]), now))
            for change in plan["files"]:
                path = Path(change["path"])
                current = path.read_bytes() if path.exists() else None
                expected = (
                    base64.b64decode(change["before"]) if change["before"] is not None else None
                )
                if current != expected:
                    raise ValueError(f"归并文件已变化：{path}")
                _atomic(path, base64.b64decode(change["after"]))
            conn.commit()
            _journal().unlink()
            index_warning = None
            try:
                open_wiki(config.wiki_dir(), config.raw_dir()).reindex()
            except (OSError, ValueError, WikiError) as exc:
                index_warning = f"归并已提交，但知识目录重建失败：{exc}；请运行 sb wiki reindex"
                echo(index_warning)
            echo(f"材料 #{duplicate_id} 已归并至 #{canonical_id}；原件、历史与旧页面均保留")
            return {"canonical_id": canonical_id, "duplicate_id": duplicate_id,
                    "token": token, "improved": improved, "evidence": plan["evidence"],
                    "index_updated": index_warning is None, "index_warning": index_warning}
        except Exception:
            conn.rollback()
            _recover(conn)
            raise
        finally:
            conn.close()
