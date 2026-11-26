"""外部阅读登记、独立清单与增量整理；仅使用临时配置、SQLite/vault 和 fake。"""

import json
import shutil
from datetime import UTC, datetime

import httpx
import pytest
from conftest import fast_settings

from secondbrain import config, db, ingest, ops
from secondbrain.llm.base import LLMError, Usage
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import ModelPrice, RawEntry, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.urls import content_hash
from secondbrain.wiki.ingest import material_input_hash
from secondbrain.wiki.store import open_wiki

NOW = datetime(2026, 11, 16, 10, tzinfo=UTC)
BODY = "KV cache stores keys and values used by attention during inference."


class RecordingFake(FakeBackend):
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.failure_usage = None
        self.actual_tokens = None

    def model_name(self, tier):
        return "workflow-test"

    def complete_json(self, messages, schema, tier):
        self.calls.append((messages, schema, tier))
        if len(self.calls) == self.fail_at:
            raise LLMError("离线模型失败", self.failure_usage)
        response, usage = super().complete_json(messages, schema, tier)
        if self.actual_tokens is not None:
            usage = Usage(self.model_name(tier), self.actual_tokens, 0, self.actual_tokens, 0)
        return response, usage


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    settings, filters, fetch = ops._settings()
    filters = filters.model_copy(deep=True)
    filters.run.max_cost_usd = 100
    filters.pricing = {
        "workflow-test": ModelPrice(input_cache_hit=1, input_cache_miss=2, output=3)
    }
    backend = RecordingFake()
    monkeypatch.setattr(ops, "_settings", lambda: (settings, filters, fetch))
    monkeypatch.setattr(ops, "_backend", lambda *_: ("fake", backend))
    return tmp_path, backend, filters


def add_material(*, key="one", body=BODY, completeness="full", conn=None):
    own = conn is None
    conn = conn or db.connect()
    try:
        item_id, _, _ = db.persist_material(
            conn,
            db.NewItem(
                source_id=None, url="", url_canonical=f"text:{key}",
                content_hash=content_hash("KV cache reading", body), title="KV cache reading",
                author=None, summary=None, content=body, original_text=body,
                extract_method="manual_text", published_at=None,
                completeness=completeness, origin_kind="text", origin_locator=key,
            ),
            NOW,
        )
        conn.commit()
        return item_id
    finally:
        if own:
            conn.close()


def calls():
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    finally:
        conn.close()


def test_explicit_queue_is_independent_of_unrelated_preferences(workflow):
    item_id = add_material()
    conn = db.connect()
    try:
        db.add_to_reading_list(conn, item_id, NOW)
        db.add_to_reading_list(conn, item_id)
        assert db.feedback_for(conn, item_id) == []
        row, = db.reading_list_items(conn)
        assert dict(row) == {
            "id": item_id, "title": "KV cache reading", "url": "", "source_id": None,
            "added_at": db.format_time(NOW), "content_len": len(BODY),
            "reading_state": "unread", "processing_status": "pending",
        }
        db.add_feedback(conn, item_id, "dismiss", "偏好与清单分离", NOW)
        db.add_feedback(conn, item_id, "clear", None, NOW)
        assert [row["id"] for row in db.reading_list_items(conn)] == [item_id]
        db.remove_from_reading_list(conn, item_id)
        assert db.reading_list_items(conn) == []
        assert len(db.feedback_for(conn, item_id)) == 2
        assert db.item_detail(conn, item_id)["reading_state"] == "unread"
    finally:
        conn.close()


def test_legacy_feedback_shortcuts_and_read_remove_membership(workflow):
    item_id = add_material()
    conn = db.connect()
    try:
        db.add_feedback(conn, item_id, "later", None, NOW)
        assert len(db.reading_list_items(conn)) == 1
        db.toggle_feedback(conn, item_id, "later", None, NOW)
        assert db.reading_list_items(conn) == []
        db.add_to_reading_list(conn, item_id)
        db.add_feedback(conn, item_id, "too_easy", None, NOW)
        assert db.reading_list_items(conn) == []
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
        db.add_to_reading_list(conn, item_id)
        db.mark_read(conn, item_id, NOW)
        assert db.reading_list_items(conn) == []
        db.add_to_reading_list(conn, item_id)
        db.set_reading_state(conn, item_id, "read")
        assert db.reading_list_items(conn) == []
    finally:
        conn.close()


def test_subscription_fetch_and_default_manual_import_do_not_autoqueue(workflow, monkeypatch):
    source = SourceConfig(id="feed", type="rss", url="https://example.org/feed")
    monkeypatch.setitem(ingest.FETCHERS, "rss", lambda *_: [RawEntry(
        url="https://example.org/post", title="Subscription post",
        published_at=NOW, feed_content=f"<p>{BODY}</p>",
    )])
    conn = db.connect()
    client = HttpClient(
        fast_settings(min_feed_content_chars=1),
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    try:
        ingest.fetch_sources(conn, [source], client, client.settings, now=NOW)
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert db.reading_list_items(conn) == []
        out = ops.add_manual([], lambda _: None, text="A separate manual material")
        assert out["results"][0]["item_id"] is not None
        assert db.reading_list_items(conn) == []
    finally:
        client.close()
        conn.close()


def test_manual_read_is_local_unless_ingest_explicitly_requested(workflow):
    later = ops.add_manual([], lambda _: None, text=BODY, state="later")
    item_id = later["results"][0]["item_id"]
    conn = db.connect()
    try:
        assert [row["id"] for row in db.reading_list_items(conn)] == [item_id]
        assert db.feedback_for(conn, item_id) == []
    finally:
        conn.close()
    read = ops.add_manual([], lambda _: None, text=BODY, state="read")
    assert read["ingest"] is None
    assert calls() == []
    explicit = ops.add_manual([], lambda _: None, text=BODY, state="read", ingest=True)
    assert explicit["ingest"]["failures"] == 0 and calls()
    conn = db.connect()
    try:
        assert db.reading_list_items(conn) == []
        assert db.feedback_for(conn, item_id) == []
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
        assert db.item_detail(conn, item_id)["processing_status"] == "processed"
    finally:
        conn.close()


def test_register_read_notes_reference_and_unchanged_cache_need_no_key(workflow, monkeypatch):
    _, backend, _ = workflow
    item_id = add_material()
    notes = "我的结论：先比较 KV cache 与 activation cache。\n个人文字不要改。\n"
    reference = "https://chatgpt.com/c/external-discussion"
    monkeypatch.setattr(
        ops, "HttpClient",
        lambda *_a, **_kw: pytest.fail("登记外部讨论只保存链接，不抓取聊天"),
    )
    first = ops.register_read(item_id, lambda _: None, notes=notes, discussion_url=reference)
    assert first["ingest"] is None and calls() == []
    summary = ops.ingest_items([item_id], lambda _: None, include_notes=True, require_read=True)
    assert not summary.failures and summary.cost > 0
    assert any(
        "我的结论：先比较 KV cache 与 activation cache。" in messages[-1]["content"]
        for messages, _, _ in backend.calls
    )
    assert ops.material_notes(item_id) == notes
    before = calls()
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        read_at = row["read_at"]
        assert row["reading_state"] == "read"
        assert db.feedback_for(conn, item_id) == []
        assert db.get_ai(conn, item_id, "reading_record")["discussion_url"] == reference
        wiki = open_wiki(config.wiki_dir(), config.raw_dir())
        assert (
            wiki.pages[f"material-{item_id}"].meta["input_hash"]
            == material_input_hash(row, notes, include_notes=True)
        )
    finally:
        conn.close()

    def forbidden_backend(*_):
        raise AssertionError("缓存不应检查 API key 或创建后端")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(ops, "_backend", forbidden_backend)
    second = ops.register_read(item_id, lambda _: None, discussion_url=reference + "-updated")
    assert second["ingest"] is None
    reused = ops.ingest_items([item_id, item_id], lambda _: None, include_notes=True)
    assert reused.cached == [item_id]
    assert calls() == before
    assert ops.material_notes(item_id) == notes
    conn = db.connect()
    try:
        assert db.item_detail(conn, item_id)["read_at"] == read_at
    finally:
        conn.close()


def test_unread_compilation_is_promoted_without_new_calls_or_mastery_change(workflow, monkeypatch):
    item_id = add_material()
    assert ops.ingest_items([item_id], lambda _: None).failures == 0
    before = calls()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    assert wiki.pages[f"material-{item_id}"].meta["knowledge_status"] == "draft"
    mastery = {slug: page.meta.get("mastery") for slug, page in wiki.pages.items()}
    monkeypatch.setattr(ops, "_backend", lambda *_: pytest.fail("promote 不能调用后端"))
    ops.register_read(item_id)
    assert open_wiki(config.wiki_dir(), config.raw_dir()).pages[
        f"material-{item_id}"
    ].knowledge_status == "draft"
    result = ops.ingest_items([item_id], lambda _: None)
    assert result.cached == [item_id] and result.cost == 0
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    assert wiki.pages[f"material-{item_id}"].meta["knowledge_status"] == "recorded"
    assert {slug: page.meta.get("mastery") for slug, page in wiki.pages.items()} == mastery
    assert calls() == before


def test_changed_notes_body_and_force_recompile_but_stable_input_does_not(workflow):
    _, backend, _ = workflow
    item_id = add_material(completeness="partial")
    ops.register_read(item_id, notes="First personal conclusion.")

    def organize(force=False):
        return ops.ingest_items([item_id], lambda _: None, force=force, include_notes=True)

    organize()
    first_count = len(backend.calls)
    assert first_count > 0
    ops.material_notes(item_id, "An updated personal conclusion.")
    assert not organize().cached
    assert len(backend.calls) > first_count
    second_count = len(backend.calls)
    add_material(body=BODY + " More complete source body.", completeness="full")
    assert not organize().cached
    assert len(backend.calls) > second_count
    third_count = len(backend.calls)
    assert organize().cached
    assert len(backend.calls) == third_count
    assert not organize(force=True).cached
    assert len(backend.calls) > third_count
    force_count = len(backend.calls)
    ops.ingest_items([item_id, item_id], lambda _: None, force=True, include_notes=True)
    assert len(backend.calls) == force_count + 1
    assert ops.material_notes(item_id) == "An updated personal conclusion."


def test_budget_rejection_keeps_read_notes_and_can_retry(workflow):
    _, backend, filters = workflow
    item_id = add_material()
    conn = db.connect()
    db.add_to_reading_list(conn, item_id)
    conn.close()
    filters.run.max_cost_usd = 0
    result = ops.register_read(item_id, lambda _: None, notes="已在外部学过；费用失败也保留。")
    assert result["ingest"] is None
    assert ops.ingest_items([item_id], lambda _: None).failures == 1
    assert backend.calls == [] and calls() == []
    conn = db.connect()
    try:
        row = db.item_detail(conn, item_id)
        assert row["reading_state"] == "read" and row["read_at"]
        assert row["processing_status"] == "pending"
        assert db.reading_list_items(conn) == []
    finally:
        conn.close()
    filters.run.max_cost_usd = 100
    assert ops.ingest_items([item_id], lambda _: None).failures == 0
    assert ops.material_notes(item_id) == "已在外部学过；费用失败也保留。"
    before = calls()
    filters.run.max_cost_usd = 0
    assert ops.ingest_items([item_id], lambda _: None).cached
    forced = ops.ingest_items([item_id], lambda _: None, force=True)
    assert forced.failures == 1
    assert calls() == before
    assert ops.ingest_items([item_id], lambda _: None).cached
    assert calls() == before


@pytest.mark.parametrize("known_usage", [False, True])
def test_failed_calls_record_usage_and_read_survives_retry(workflow, known_usage):
    _, backend, _ = workflow
    item_id = add_material()
    backend.fail_at = 1
    backend.failure_usage = Usage("workflow-test", 10, 0, 10, 3) if known_usage else None
    ops.register_read(item_id)
    assert ops.ingest_items([item_id], lambda _: None).failures == 1
    record, = calls()
    assert record["usage_known"] == int(known_usage) and record["error"] == "离线模型失败"
    conn = db.connect()
    try:
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
        assert db.item_detail(conn, item_id)["processing_status"] == "pending"
    finally:
        conn.close()
    backend.fail_at = None
    assert ops.ingest_items([item_id], lambda _: None).failures == 0
    assert len(calls()) == 2


def test_unknown_usage_stops_batch_without_calling_next_material(workflow):
    _, backend, _ = workflow
    first = add_material(key="first")
    second = add_material(key="second", body=BODY + " Another source.")
    backend.fail_at = 1
    summary = ops.ingest_items([first, second], lambda _: None)
    assert summary.failures == 1 and len(backend.calls) == 1
    record, = calls()
    assert record["usage_known"] == 0 and record["stage"] == "WIKI"


def test_actual_budget_overrun_accounts_once_and_retains_read(workflow):
    _, backend, filters = workflow
    item_id = add_material()
    filters.run.max_cost_usd = 1
    backend.actual_tokens = 100_000_000
    ops.register_read(item_id)
    assert ops.ingest_items([item_id], lambda _: None).failures == 1
    record, = calls()
    assert record["cost_usd"] > 1 and "run.max_cost_usd" in record["error"]
    assert record["usage_known"] == 1
    conn = db.connect()
    try:
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
        assert db.item_detail(conn, item_id)["processing_status"] == "pending"
    finally:
        conn.close()


def test_backend_configuration_failure_does_not_undo_registration(workflow, monkeypatch):
    item_id = add_material()

    def missing_backend(*_):
        raise ops.OpsError("API key 未设置")

    monkeypatch.setattr(ops, "_backend", missing_backend)
    result = ops.register_read(item_id, lambda _: None, notes="My retained notes")
    assert result["ingest"] is None
    assert ops.ingest_items([item_id], lambda _: None).failures == 1
    assert ops.material_notes(item_id) == "My retained notes"
    conn = db.connect()
    try:
        assert db.item_detail(conn, item_id)["reading_state"] == "read"
    finally:
        conn.close()


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///private/chat", "https://user:SECRET@example.org/chat",
    "https://example.org:invalid/chat", "https://example.org:70000/chat",
    "https://example.org/line\nbreak", "https:///missing-host", "https://example.org\\@evil.org",
])
def test_invalid_discussion_url_has_safe_error_and_no_changes(workflow, url):
    item_id = add_material()
    with pytest.raises(ops.OpsError) as error:
        ops.register_read(item_id, lambda _: None, discussion_url=url, notes="not saved")
    assert "SECRET" not in str(error.value) and url not in str(error.value)
    assert calls() == [] and ops.material_notes(item_id) == ""
    conn = db.connect()
    try:
        assert db.item_detail(conn, item_id)["reading_state"] == "unread"
        assert db.get_ai(conn, item_id, "reading_record") is None
    finally:
        conn.close()


def test_copied_v11_migration_only_queues_latest_later_and_preserves_records(workflow):
    root, _, _ = workflow
    original = root / "original-v11.db"
    conn = db.connect(original)
    for item_id in range(1, 6):
        add_material(key=f"legacy-{item_id}", body=BODY + str(item_id), conn=conn)
    for table in (
        "material_merges", "material_aliases", "material_lifecycle",
        "source_analyses", "source_catalog_hidden", "reading_list",
    ):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 11")
    conn.executemany(
        "INSERT INTO feedback(item_id,kind,note,ts) VALUES(?,?,?,?)",
        [
            (1, "later", "Queue me", "2026-11-01T00:00:00Z"),
            (2, "later", None, "2026-11-02T00:00:00Z"),
            (2, "clear", None, "2026-11-03T00:00:00Z"),
            (3, "later", None, "2026-11-04T00:00:00Z"),
            (3, "gained", "Read already", "2026-11-05T00:00:00Z"),
            (4, "dismiss", "Preference", "2026-11-06T00:00:00Z"),
        ],
    )
    conn.execute(
        "UPDATE items SET reading_state='read', read_at='2026-11-05T00:00:00Z', "
        "processing_status='processed' WHERE id=3"
    )
    conn.execute("INSERT INTO wiki_ingests(item_id,ts,pages) VALUES(3,'old','[\"legacy\"]')")
    conn.execute(
        "INSERT INTO item_ai(item_id,kind,data,model,created_at) "
        "VALUES(3,'summary','{\"summary\":\"Keep me\"}','fake','old')"
    )
    conn.commit()
    expected_items = [tuple(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]
    expected_feedback = [tuple(row) for row in conn.execute("SELECT * FROM feedback ORDER BY id")]
    expected_origins = [
        tuple(row) for row in conn.execute("SELECT * FROM item_sources ORDER BY id")
    ]
    conn.close()
    original_bytes = original.read_bytes()
    copied = root / "copied-v11.db"
    shutil.copyfile(original, copied)
    migrated = db.connect(copied)
    try:
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        queue, = db.reading_list_items(migrated)
        assert queue["id"] == 1 and queue["added_at"] == "2026-11-01T00:00:00Z"
        assert [
            tuple(row) for row in migrated.execute("SELECT * FROM items ORDER BY id")
        ] == expected_items
        assert [
            tuple(row) for row in migrated.execute("SELECT * FROM feedback ORDER BY id")
        ] == expected_feedback
        assert [
            tuple(row) for row in migrated.execute("SELECT * FROM item_sources ORDER BY id")
        ] == expected_origins
        assert db.get_ai(migrated, 3, "summary")["summary"] == "Keep me"
        pages = migrated.execute("SELECT pages FROM wiki_ingests").fetchone()[0]
        assert json.loads(pages) == ["legacy"]
    finally:
        migrated.close()
    assert original.read_bytes() == original_bytes


def test_first_sources_save_starts_empty_and_preserves_existing_file(workflow):
    example = config.config_dir() / "sources.example.yaml"
    example_bytes = example.read_bytes()
    path, created = ops.ensure_sources_file()
    assert created and path.read_text(encoding="utf-8") == "sources: []\n"
    assert config.load_sources(path) == []
    selected = ops.edit_sources(lambda _: (
        "sources:\n"
        "  - id: personally-selected\n"
        "    type: rss\n"
        "    url: https://example.org/feed\n"
    ))
    assert [source.id for source in selected] == ["personally-selected"]
    personal = path.read_bytes()
    assert ops.ensure_sources_file() == (path, False)
    assert path.read_bytes() == personal
    assert example.read_bytes() == example_bytes
