"""知识同步预览、回收/恢复与来源边界；全部数据放在临时目录。"""

from datetime import UTC, date, datetime

import pytest

from secondbrain import db
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings
from secondbrain.ops import IngestSummary
from secondbrain.urls import content_hash
from secondbrain.wiki import sync
from secondbrain.wiki.graph import build_graph
from secondbrain.wiki.ingest import ingest_item, material_input_hash, pending_items
from secondbrain.wiki.lint import lint
from secondbrain.wiki.loop import related_knowledge
from secondbrain.wiki.store import Page, WikiError, new_meta, open_wiki

TODAY = date(2026, 11, 18)
NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    conn = db.connect()
    wiki = open_wiki(tmp_path / "wiki", tmp_path / "wiki" / "raw")
    yield conn, wiki
    conn.close()


def material(conn, key, *, read=True):
    body = f"{key} material content"
    iid, _, _ = db.persist_material(conn, db.NewItem(
        source_id=None, url="", url_canonical=f"text:{key}",
        content_hash=content_hash(key, body), title=key, author=None, summary=None,
        content=body, original_text=body, extract_method="manual_text", published_at=None,
        completeness="full", origin_kind="text", origin_locator=key,
    ), NOW)
    if read:
        conn.execute("UPDATE items SET reading_state = 'read' WHERE id = ?", (iid,))
    conn.commit()
    return iid


def compiled(wiki, conn, iid):
    meta = new_meta(f"Material {iid}", "material", TODAY)
    meta.update(generated_by="wiki_ingest", material_id=iid, sources=[iid],
                knowledge_status="recorded",
                input_hash=material_input_hash(db.item_detail(conn, iid), ""))
    page = Page(f"material-{iid}", meta, "# Material\n\n## 个人笔记\n\n我的判断。\n")
    wiki.save_page(page)
    conn.execute("UPDATE items SET processing_status = 'processed' WHERE id = ?", (iid,))
    db.record_ingest(conn, iid, [page.slug], NOW)
    conn.commit()
    return page


def concept(wiki, sources, *, slug="shared-concept", generated=True):
    meta = new_meta("Shared concept", "concept", TODAY)
    meta["mastery"] = "想回炉"
    meta.update(sources=sources, knowledge_status="recorded", last_reviewed="2020-01-01",
                human_relations={"blocked": ["do-not-remove"]})
    if generated:
        meta["generated_by"] = "wiki_ingest"
    page = Page(slug, meta, "# Shared concept\n\n## 个人笔记\n\n亲自推导，必须保留。\n")
    wiki.save_page(page)
    return page


def lifecycle(conn, iid, trashed, *, hidden=False):
    conn.execute(
        """INSERT INTO material_lifecycle
           (item_id, hidden, trashed, revision, synced_revision, updated_at)
           VALUES (?, ?, ?, 1, 0, ?)
           ON CONFLICT(item_id) DO UPDATE SET hidden = excluded.hidden,
               trashed = excluded.trashed, revision = material_lifecycle.revision + 1""",
        (iid, int(hidden), int(trashed), NOW.isoformat()),
    )
    conn.commit()


def apply():
    return sync.apply_sync(sync.preview_sync()["token"], lambda message: None)


def no_model(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("metadata synchronization must not invoke ingestion")
    monkeypatch.setattr(sync, "ingest_items", forbidden)


def test_preview_is_read_only_and_unread_is_not_registered(env, monkeypatch):
    conn, wiki = env
    material(conn, "unread", read=False)
    before_files = {p: p.read_bytes() for p in wiki.root.rglob("*") if p.is_file()}
    before_changes = conn.total_changes
    no_model(monkeypatch)
    preview = sync.preview_sync()
    assert preview["changes"] == []
    assert conn.total_changes == before_changes
    assert {p: p.read_bytes() for p in wiki.root.rglob("*") if p.is_file()} == before_files


def test_preview_does_not_initialize_missing_database(tmp_path, monkeypatch):
    root = tmp_path / "not-created"
    monkeypatch.setenv("SB_DATA_DIR", str(root))
    assert sync.preview_sync()["changes"] == []
    assert not root.exists()


@pytest.mark.parametrize("change", ["content", "notes", "page", "lifecycle"])
def test_stale_preview_refuses_all_mutations(env, change):
    conn, wiki = env
    iid = material(conn, "stale")
    page = compiled(wiki, conn, iid)
    lifecycle(conn, iid, True)
    token = sync.preview_sync()["token"]
    if change == "content":
        conn.execute("UPDATE items SET original_text = 'changed' WHERE id = ?", (iid,))
        conn.commit()
    elif change == "notes":
        notes = wiki.root / "notes" / f"material-{iid}-notes.md"
        notes.parent.mkdir(exist_ok=True)
        notes.write_text("新的学习笔记", encoding="utf-8")
    elif change == "page":
        page.path.write_text(page.path.read_text() + "\n人工补充\n", encoding="utf-8")
    else:
        lifecycle(conn, iid, False)
    with pytest.raises(sync.SyncConflict):
        sync.apply_sync(token, lambda message: None)
    wiki.reload_pages()
    assert "material_sync" not in wiki.pages[page.slug].meta
    assert db.lifecycle_states(conn)[iid]["synced_revision"] == 0


def test_trash_preserves_personal_content_and_restore_clears_markers(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "single")
    page = compiled(wiki, conn, iid)
    shared = concept(wiki, [iid])
    original_body = page.body
    personal = shared.body
    mastery = shared.meta["mastery"]
    relations = shared.meta["human_relations"].copy()
    raw = wiki.write_raw(iid, "single", "", "original raw")
    raw_bytes = raw.read_bytes()
    notes = wiki.root / "notes" / f"material-{iid}-notes.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text("独立个人笔记\n", encoding="utf-8")
    # 笔记已被整理，回收与恢复只需要元数据同步。
    page.meta["input_hash"] = material_input_hash(db.item_detail(conn, iid), notes.read_text())
    wiki.save_page(page)
    no_model(monkeypatch)
    lifecycle(conn, iid, True)
    result = apply()
    assert page.slug in result["withdrawn"]
    assert shared.slug in result["review"]
    wiki.reload_pages()
    assert wiki.pages[page.slug].material_sync_status == "withdrawn"
    assert wiki.pages[shared.slug].material_sync_status == "unsupported"
    assert wiki.pages[page.slug].body == original_body
    assert wiki.pages[shared.slug].body == personal
    assert wiki.pages[shared.slug].meta["mastery"] == mastery
    assert wiki.pages[shared.slug].meta["human_relations"] == relations
    assert raw.read_bytes() == raw_bytes and notes.read_text() == "独立个人笔记\n"
    assert sync.preview_sync()["changes"] == []
    assert build_graph(wiki)["nodes"] == []
    assert len(build_graph(wiki, include_drafts=True)["nodes"]) == 2
    assert not wiki.find("Shared concept").is_visible
    assert related_knowledge(wiki, [db.item_detail(conn, iid)], 500, 2000) == ""
    assert not lint(wiki).index_missing
    lifecycle(conn, iid, False)
    assert sync.preview_sync()["changes"][0]["action"] == "restore"
    result = apply()
    assert set(result["restored"]) == {page.slug, shared.slug}
    wiki.reload_pages()
    assert all("material_sync" not in p.meta for p in wiki.pages.values())
    assert wiki.pages[shared.slug].body == personal
    assert sync.preview_sync()["changes"] == []


def test_multi_source_concept_is_review_and_manual_page_is_untouched(env, monkeypatch):
    conn, wiki = env
    first, second = material(conn, "first"), material(conn, "second")
    compiled(wiki, conn, first)
    compiled(wiki, conn, second)
    shared = concept(wiki, [first, second])
    manual = concept(wiki, [first], slug="manual", generated=False)
    original = manual.path.read_bytes()
    no_model(monkeypatch)
    lifecycle(conn, first, True)
    apply()
    wiki.reload_pages()
    shared = wiki.pages[shared.slug]
    assert shared.material_sync_status == "review"
    assert shared.meta["material_sync"]["removed_sources"] == [first]
    assert manual.path.read_bytes() == original
    assert shared.slug in {node["id"] for node in build_graph(wiki)["nodes"]}
    assert "待复核" in wiki.index_path.read_text()
    assert "〔已登记〕" not in wiki.index_path.read_text()
    lifecycle(conn, second, True)
    apply()
    wiki.reload_pages()
    assert wiki.pages[shared.slug].material_sync_status == "unsupported"
    lifecycle(conn, first, False)
    apply()
    wiki.reload_pages()
    assert wiki.pages[shared.slug].material_sync_status == "review"
    assert wiki.pages[shared.slug].meta["material_sync"]["removed_sources"] == [second]


def test_hidden_read_material_does_not_change_knowledge(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "hidden")
    page = compiled(wiki, conn, iid)
    conn.execute(
        "INSERT INTO material_lifecycle VALUES (?, 1, 0, 0, 0, ?)", (iid, NOW.isoformat()),
    )
    conn.commit()
    before = page.path.read_bytes()
    no_model(monkeypatch)
    assert sync.preview_sync()["changes"] == []
    assert apply()["failures"] == 0
    assert page.path.read_bytes() == before


def test_changed_notes_are_pending_without_processing_status_change(env):
    conn, wiki = env
    iid = material(conn, "notes")
    compiled(wiki, conn, iid)
    notes = wiki.root / "notes" / f"material-{iid}-notes.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text("updated learning record", encoding="utf-8")
    change = sync.preview_sync()["changes"][0]
    assert change["item_id"] == iid and change["needs_ingest"]
    assert change["action"] == "ingest"


def test_restore_failure_keeps_revision_pending(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "failed-restore")
    compiled(wiki, conn, iid)
    lifecycle(conn, iid, True)
    no_model(monkeypatch)
    apply()
    lifecycle(conn, iid, False)
    conn.execute("UPDATE items SET original_text = 'new content' WHERE id = ?", (iid,))
    conn.commit()
    monkeypatch.setattr(sync, "ingest_items", lambda ids, echo: IngestSummary(
        failures=1, errors=["model unavailable"],
    ))
    result = apply()
    assert result["failures"] == 1
    state = db.lifecycle_states(conn)[iid]
    assert state["revision"] != state["synced_revision"]
    assert sync.preview_sync()["changes"][0]["needs_ingest"]


def test_revision_changed_during_sync_is_not_acknowledged(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "racing")
    compiled(wiki, conn, iid)
    lifecycle(conn, iid, True)
    original_save = type(wiki).save_metadata

    def racing_save(self, page, **kwargs):
        original_save(self, page, **kwargs)
        lifecycle(conn, iid, False)

    monkeypatch.setattr(type(wiki), "save_metadata", racing_save)
    result = apply()
    assert result["failures"] == 1
    assert db.lifecycle_states(conn)[iid]["synced_revision"] == 0
    assert sync.preview_sync()["changes"][0]["action"] == "restore"


def test_trashed_material_is_not_pending_or_sent_to_model(env):
    conn, wiki = env
    iid = material(conn, "trashed")
    lifecycle(conn, iid, True)
    assert iid not in pending_items(conn, wiki)
    with pytest.raises(WikiError, match="回收站"):
        ingest_item(conn, wiki, FakeBackend(), None, db.item_detail(conn, iid),
                    FilterSettings(), TODAY)


def test_apply_uses_existing_ingestion_for_new_registered_material(env):
    conn, wiki = env
    iid = material(conn, "registered")
    result = apply()
    assert result["failures"] == 0
    assert f"material-{iid}" in result["created"]
    assert sync.preview_sync()["changes"] == []


def test_metadata_write_failure_remains_previewable_after_restart(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "metadata-failure")
    page = compiled(wiki, conn, iid)
    lifecycle(conn, iid, True)
    original_save = type(wiki).save_metadata

    def broken_save(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(type(wiki), "save_metadata", broken_save)
    assert apply()["failures"] == 1
    assert sync.preview_sync()["changes"][0]["item_id"] == iid
    assert db.lifecycle_states(conn)[iid]["synced_revision"] == 0
    monkeypatch.setattr(type(wiki), "save_metadata", original_save)
    assert apply()["failures"] == 0
    wiki.reload_pages()
    assert wiki.pages[page.slug].material_sync_status == "withdrawn"
    assert sync.preview_sync()["changes"] == []


def test_body_update_uses_real_incremental_ingestion(env):
    conn, wiki = env
    iid = material(conn, "body-update")
    page = compiled(wiki, conn, iid)
    original_body = page.body
    conn.execute("UPDATE items SET original_text = 'new full content' WHERE id = ?", (iid,))
    conn.commit()
    assert sync.preview_sync()["changes"][0]["needs_ingest"]
    result = apply()
    assert result["failures"] == 0 and page.slug in result["updated"]
    wiki.reload_pages()
    assert original_body.strip() in wiki.pages[page.slug].body
    assert "new full content" in wiki.raw_path(iid).read_text()
    assert sync.preview_sync()["changes"] == []


def test_sync_metadata_preserves_crlf_body_bytes(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "crlf")
    page = compiled(wiki, conn, iid)
    page.path.write_bytes(page.path.read_bytes().replace(b"\n", b"\r\n"))
    body = page.path.read_bytes().split(b"\r\n---", 1)[1]
    lifecycle(conn, iid, True)
    no_model(monkeypatch)
    assert apply()["failures"] == 0
    assert page.path.read_bytes().split(b"\n---", 1)[1] == body


def test_registered_existing_draft_promotes_without_model(env, monkeypatch):
    conn, wiki = env
    iid = material(conn, "promote-draft")
    page = compiled(wiki, conn, iid)
    page.meta["knowledge_status"] = "draft"
    wiki.save_page(page)
    from secondbrain import ops

    def forbidden(*args, **kwargs):
        pytest.fail("unchanged draft promotion must not initialize a backend")

    monkeypatch.setattr(ops, "_backend", forbidden)
    assert sync.preview_sync()["changes"][0]["needs_ingest"]
    assert apply()["failures"] == 0
    wiki.reload_pages()
    assert wiki.pages[page.slug].knowledge_status == "recorded"
    assert sync.preview_sync()["changes"] == []


def test_source_added_during_ingestion_keeps_shared_concept_visible(env, monkeypatch):
    conn, wiki = env
    old_id = material(conn, "old-source")
    updating_id = material(conn, "updating-source")
    compiled(wiki, conn, old_id)
    compiled(wiki, conn, updating_id)
    shared = concept(wiki, [old_id, updating_id])
    lifecycle(conn, old_id, True)
    apply()
    conn.execute(
        "UPDATE items SET original_text = 'updated body' WHERE id = ?", (updating_id,),
    )
    conn.commit()
    original_ingest = sync.ingest_items
    added = []

    def ingestion_with_concurrent_source(ids, echo):
        summary = original_ingest(ids, echo)
        new_id = material(conn, "concurrent-source", read=False)
        added.append(new_id)
        # 模拟整理服务返回前另一请求回收旧来源、为共享页补入新的有效来源。
        lifecycle(conn, updating_id, True)
        wiki.reload_pages()
        page = wiki.pages[shared.slug]
        page.meta["sources"].append(new_id)
        wiki.save_page(page)
        return summary

    monkeypatch.setattr(sync, "ingest_items", ingestion_with_concurrent_source)
    result = apply()
    assert added
    assert result["failures"] == 1  # 生命周期在同步期间变化，不能确认旧版本。
    wiki.reload_pages()
    page = wiki.pages[shared.slug]
    assert page.material_sync_status == "review"
    assert page.meta["material_sync"]["removed_sources"] == [old_id, updating_id]
    node = next(node for node in build_graph(wiki)["nodes"] if node["id"] == page.slug)
    assert node["material_sync_status"] == "review" and added[0] in node["sources"]
    assert page.is_visible


def test_sync_never_recompiles_or_restores_a_merged_historical_material(env, monkeypatch):
    conn, wiki = env
    canonical, duplicate = material(conn, "main"), material(conn, "old")
    compiled(wiki, conn, canonical)
    historical = compiled(wiki, conn, duplicate)
    historical.meta.update(
        merged_into=canonical, sources=[canonical],
        material_sync={"status": "withdrawn", "merged_into": canonical},
    )
    wiki.save_metadata(historical)
    before = historical.path.read_bytes()
    conn.execute("INSERT INTO material_aliases VALUES (?, ?, ?)",
                 (duplicate, canonical, NOW.isoformat()))
    conn.commit()
    no_model(monkeypatch)
    assert sync.preview_sync()["changes"] == []
    lifecycle(conn, canonical, True)
    assert {change["item_id"] for change in sync.preview_sync()["changes"]} == {canonical}
    apply()
    lifecycle(conn, canonical, False)
    apply()
    wiki.reload_pages()
    assert historical.path.read_bytes() == before
    assert wiki.pages[historical.slug].material_sync_status == "withdrawn"
    assert not wiki.pages[historical.slug].is_visible
    assert sync.preview_sync()["changes"] == []
