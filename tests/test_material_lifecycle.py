"""材料隐藏、回收、恢复与消费者回归；所有数据和原件均位于临时目录。"""

import sqlite3
from datetime import UTC, datetime

import pytest

from secondbrain import config, db, ops
from secondbrain import material_management as management
from secondbrain.models import SourceConfig
from secondbrain.pipeline.run import deep_one
from secondbrain.urls import content_hash

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    return tmp_path


def material(number=1, *, source_id=None, completeness="full", title=None, text=None):
    text = text or f"Original material body {number}"
    title = title or f"Material {number}"
    url = f"https://example.org/material/{number}"
    return db.NewItem(
        source_id=source_id,
        url=url,
        url_canonical=url,
        content_hash=content_hash(title, text),
        title=title,
        author="Example author",
        summary=None,
        content=text,
        extract_method="manual_text",
        published_at=None,
        completeness=completeness,
    )


def add(conn, number=1, **kwargs):
    item_id, created, _ = db.persist_material(conn, material(number, **kwargs), NOW)
    assert created
    conn.commit()
    return item_id


def ids(rows):
    return [row["id"] for row in rows]


def snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in (
            "items", "item_sources", "feedback", "assessments", "reading_list", "wiki_ingests"
        )
    }


def test_hide_unhide_only_change_default_material_list(env):
    conn = db.connect()
    try:
        item_id = add(conn)
        db.set_reading_state(conn, item_id, "reading")
        db.add_to_reading_list(conn, item_id, NOW)
        db.add_assessment(conn, item_id, "L1", {"topics": ["knowledge"]}, NOW)
        db.record_ingest(conn, item_id, ["material-1", "shared-concept"], NOW)
        conn.commit()
        ops.material_notes(item_id, "Personal observations must stay unchanged.")
        before = snapshot(conn)
        original = config.wiki_dir() / db.material_origins(conn, item_id)[0]["text_path"]
        original_bytes = original.read_bytes()

        assert management.manage_materials([item_id, item_id], "hide") == {
            "changed": [item_id], "unchanged": []
        }
        assert management.list_materials() == []
        assert ids(management.list_materials(view="hidden")) == [item_id]
        row, = management.list_materials(view="all")
        assert row["is_hidden"] == 1 and row["is_trashed"] == 0
        assert ids(db.reading_list_items(conn)) == [item_id]
        assert snapshot(conn) == before
        assert original.read_bytes() == original_bytes
        assert ops.material_notes(item_id) == "Personal observations must stay unchanged."
        assert db.lifecycle_states(conn)[item_id] == {
            "hidden": True, "trashed": False, "revision": 0, "synced_revision": 0
        }
        assert management.manage_materials([item_id], "hide") == {
            "changed": [], "unchanged": [item_id]
        }
        assert management.manage_materials([item_id], "unhide")["changed"] == [item_id]
        assert ids(management.list_materials()) == [item_id]
        assert db.lifecycle_states(conn)[item_id]["revision"] == 0
    finally:
        conn.close()


def test_trash_restore_preserve_identity_reading_membership_and_history(env):
    conn = db.connect()
    try:
        item_id = add(conn)
        db.add_feedback(conn, item_id, "gained", "A useful observation", NOW)
        db.add_to_reading_list(conn, item_id, NOW)
        db.add_assessment(conn, item_id, "L2", {"tldr": "Keep this history"}, NOW)
        db.record_ingest(conn, item_id, [f"material-{item_id}", "shared-concept"], NOW)
        conn.commit()
        ops.material_notes(item_id, "Keep personal notes.")
        before = snapshot(conn)
        management.manage_materials([item_id], "hide")
        assert management.manage_materials([item_id], "trash")["changed"] == [item_id]
        assert management.list_materials() == []
        assert management.list_materials(view="hidden") == []
        assert ids(management.list_materials(view="trash")) == [item_id]
        assert db.reading_list_items(conn) == []
        assert db.gained_item_ids(conn) == []
        assert db.is_trashed(conn, item_id)
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
        assert snapshot(conn) == before
        assert management.manage_materials([item_id], "trash")["unchanged"] == [item_id]
        assert db.lifecycle_states(conn)[item_id]["revision"] == 1
        assert management.manage_materials([item_id], "unhide")["unchanged"] == [item_id]

        assert management.manage_materials([item_id], "restore")["changed"] == [item_id]
        assert ids(management.list_materials()) == [item_id]
        assert ids(db.reading_list_items(conn)) == [item_id]
        assert db.gained_item_ids(conn) == [item_id]
        assert db.lifecycle_states(conn)[item_id] == {
            "hidden": False, "trashed": False, "revision": 2, "synced_revision": 0
        }
        assert management.manage_materials([item_id], "restore")["unchanged"] == [item_id]
        assert snapshot(conn) == before
        assert ops.material_notes(item_id) == "Keep personal notes."
    finally:
        conn.close()


def test_bulk_validation_is_atomic_and_actions_are_idempotent(env):
    conn = db.connect()
    try:
        first, second = add(conn, 1), add(conn, 2)
        with pytest.raises(ops.OpsError, match="不存在"):
            management.manage_materials([first, second + 100], "trash")
        with pytest.raises(ops.OpsError, match="正整数"):
            management.manage_materials([first, "2"], "hide")
        with pytest.raises(ops.OpsError, match="未知材料操作"):
            management.manage_materials([first], "delete")
        assert conn.execute("SELECT count(*) FROM material_lifecycle").fetchone()[0] == 0
        management.manage_materials([first], "hide")
        assert management.manage_materials([first, second, first], "hide") == {
            "changed": [second], "unchanged": [first]
        }
        assert management.manage_materials([], "hide") == {"changed": [], "unchanged": []}
        assert management.manage_materials([first], "restore")["changed"] == [first]
        assert db.lifecycle_states(conn)[first]["revision"] == 0
    finally:
        conn.close()


def test_hide_read_does_not_unlearn_or_touch_queue(env, monkeypatch):
    def no_backend(*args):
        pytest.fail("Lifecycle operations must not initialize a model")

    monkeypatch.setattr(ops, "_backend", no_backend)
    conn = db.connect()
    try:
        read, unread, trashed = add(conn, 1), add(conn, 2), add(conn, 3)
        db.add_feedback(conn, read, "gained", None, NOW)
        db.mark_read(conn, trashed, NOW)
        db.add_to_reading_list(conn, read, NOW)
        management.manage_materials([trashed], "trash")
        before = snapshot(conn)
        assert ids(management.read_to_hide()) == [read]
        assert management.hide_read() == {"changed": [read], "unchanged": []}
        assert management.hide_read() == {"changed": [], "unchanged": []}
        assert ids(management.list_materials()) == [unread]
        assert ids(db.reading_list_items(conn)) == [read]
        assert db.gained_item_ids(conn) == [read]
        assert snapshot(conn) == before
        assert db.lifecycle_states(conn)[read]["revision"] == 0
    finally:
        conn.close()


def test_material_filters_and_pagination_apply_before_limit(env):
    conn = db.connect()
    try:
        expected = [add(conn, number) for number in range(1, 54)]
        assert ids(management.list_materials()) == list(reversed(expected))[:51]
        assert ids(management.list_materials(offset=50)) == list(reversed(expected))[50:]
        target = expected[10]
        db.set_reading_state(conn, target, "reading")
        conn.execute(
            "UPDATE items SET processing_status = 'processed',"
            " content_completeness = 'partial' WHERE id = ?", (target,)
        )
        conn.commit()
        management.manage_materials([target], "hide")
        assert ids(management.list_materials(
            query="Example author", reading="reading", processing="processed",
            completeness="partial", view="hidden"
        )) == [target]
        assert management.list_materials(
            query="No matching title", view="all"
        ) == []
        assert management.list_materials(
            reading="reading", processing="processed", completeness="partial"
        ) == []
    finally:
        conn.close()


@pytest.mark.parametrize("kwargs", [
    {"reading": "later"}, {"processing": "invalid"}, {"completeness": "invalid"},
    {"view": "deleted"}, {"offset": -1}, {"offset": "1"},
])
def test_invalid_material_filters_are_rejected(env, kwargs):
    with pytest.raises(ops.OpsError):
        management.list_materials(**kwargs)


def test_lifecycle_acknowledges_only_current_revision(env):
    conn = db.connect()
    try:
        item_id = add(conn)
        assert db.acknowledge_lifecycle(conn, item_id, 0)
        conn.commit()
        management.manage_materials([item_id], "trash")
        assert db.lifecycle_states(conn)[item_id]["revision"] == 1
        management.manage_materials([item_id], "restore")
        assert not db.acknowledge_lifecycle(conn, item_id, 1)
        conn.commit()
        assert db.lifecycle_states(conn)[item_id]["synced_revision"] == 0
        assert db.acknowledge_lifecycle(conn, item_id, 2)
        conn.commit()
        management.manage_materials([item_id], "hide")
        assert db.lifecycle_states(conn)[item_id]["synced_revision"] == 2
        management.manage_materials([item_id], "trash")
        assert not db.acknowledge_lifecycle(conn, item_id, 2)
        conn.commit()
        state = db.lifecycle_states(conn)[item_id]
        assert state["revision"] == 3 and state["synced_revision"] == 2
    finally:
        conn.close()


def test_legacy_migration_defaults_active_and_readonly_preview_does_not_migrate(env):
    path = env / "legacy.db"
    conn = db.connect(path)
    item_id = add(conn)
    db.add_to_reading_list(conn, item_id, NOW)
    before = snapshot(conn)
    # 构造生命周期引入前的 v13 库；不用当前写入口操作缺少新表的旧 schema。
    for table in ("material_merges", "material_aliases", "material_lifecycle"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    readonly = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    try:
        assert not db.is_trashed(readonly, item_id)
        assert db.lifecycle_states(readonly)[item_id] == {
            "hidden": False, "trashed": False, "revision": 0, "synced_revision": 0
        }
        assert readonly.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'material_lifecycle'"
        ).fetchone() is None
    finally:
        readonly.close()
    conn = db.connect(path)
    try:
        assert snapshot(conn) == before
        assert not db.is_trashed(conn, item_id)
        assert db.lifecycle_states(conn)[item_id]["revision"] == 0
        assert conn.execute("SELECT count(*) FROM material_lifecycle").fetchone()[0] == 0
    finally:
        conn.close()


def test_trashed_duplicates_are_rejected_before_originals_or_sources_are_written(env):
    conn = db.connect()
    try:
        item_id = add(conn)
        management.manage_materials([item_id], "trash")
        before = snapshot(conn)
        originals = sorted((config.wiki_dir() / "resources").iterdir())
        with pytest.raises(ValueError, match="先恢复"):
            db.persist_material(conn, material(1, text="An improved replacement body"), NOW)
        assert snapshot(conn) == before
        assert sorted((config.wiki_dir() / "resources").iterdir()) == originals
        assert db.item_by_canonical(conn, material(1).url_canonical) == item_id
        management.manage_materials([item_id], "restore")
        same_id, created, _ = db.persist_material(conn, material(1), NOW)
        conn.commit()
        assert same_id == item_id and not created
        # 相同标题从来不是材料身份。
        other = add(conn, 2, title=material(1).title)
        assert other != item_id
    finally:
        conn.close()


def test_trashed_manual_reimport_returns_restore_error_without_network(env, monkeypatch):
    conn = db.connect()
    item_id = add(conn)
    conn.close()
    management.manage_materials([item_id], "trash")

    def no_fetch(*args, **kwargs):
        pytest.fail("A known trashed identity must be rejected before fetching")

    monkeypatch.setattr(ops, "_fetch_manual", no_fetch)
    result = ops.add_manual([material(1).url], lambda _: None)["results"][0]
    assert result["status"] == "失败" and "先恢复" in result["message"]
    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert len(db.material_origins(conn, item_id)) == 1
        assert db.is_trashed(conn, item_id)
    finally:
        conn.close()


def test_explicit_processing_rejects_trash_before_models_or_read_changes(env, monkeypatch):
    conn = db.connect()
    item_id = add(conn)
    db.add_to_reading_list(conn, item_id, NOW)
    conn.close()
    management.manage_materials([item_id], "trash")

    def no_backend(*args):
        pytest.fail("Trashed materials must not initialize models")

    monkeypatch.setattr(ops, "_backend", no_backend)
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.register_read(item_id, lambda _: None, notes="Must not overwrite notes")
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.deep_summary(item_id, lambda _: None)
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.prerequisites(item_id, lambda _: None)
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.save_prereq_page(item_id, "A concept")
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.complete_item(item_id, text="Replacement body")
    with pytest.raises(ops.OpsError, match="先恢复"):
        ops.refetch_item(item_id)
    summary = ops.ingest_items([item_id], lambda _: None)
    assert summary.failures == 1 and "先恢复" in summary.errors[0]
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        outcome = deep_one(conn, None, None, None, row, NOW)
        assert outcome.response is None and "先恢复" in outcome.error
        assert row["reading_state"] == "unread" and row["read_at"] is None
        assert conn.execute(
            "SELECT item_id FROM reading_list WHERE item_id = ?", (item_id,)
        ).fetchone() is not None
        assert ops.material_notes(item_id) == ""
        assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0
    finally:
        conn.close()


def test_automatic_selection_excludes_trash_but_not_hidden_materials(env):
    conn = db.connect()
    try:
        db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="https://example.org")])
        hidden = add(conn, 1, source_id="blog", completeness="partial")
        trashed = add(conn, 2, source_id="blog", completeness="partial")
        management.manage_materials([hidden], "hide")
        management.manage_materials([trashed], "trash")
        assert ids(db.items_by_status(conn, ("new",))) == [hidden]
        assert ids(db.incomplete_items(conn)) == [hidden]
        assert db.incomplete_ids(conn) == {hidden}
        for item_id in (hidden, trashed):
            conn.execute(
                """INSERT INTO digests
                   (date, item_id, lane, rank, est_minutes, is_deep_read, created_at,
                    budget_minutes)
                   VALUES ('2026-11-17', ?, 'must_read', 1, 5, 0, ?, 5)""",
                (item_id, db.format_time(NOW)),
            )
        conn.commit()
        assert [row["item_id"] for row in db.digest_rows(conn, "2026-11-17")] == [hidden]
        assert db.past_must_reads(conn, "2026-11-18") == [(hidden, "2026-11-17")]
        assert db.day_usage(conn, "2026-11-17", frozenset({"must_read"}), set()) == (
            {"must_read": 1}, 5
        )
        management.manage_materials([trashed], "restore")
        assert set(ids(db.items_by_status(conn, ("new",)))) == {hidden, trashed}
        assert db.incomplete_ids(conn) == {hidden, trashed}
        assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 2
    finally:
        conn.close()
