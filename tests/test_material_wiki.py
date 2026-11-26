"""材料整理与阅读、推荐、掌握度解耦；全部使用临时 vault 和离线后端。"""

import json
from datetime import UTC, date, datetime

import pytest
from conftest import FIXTURES

from secondbrain import db
from secondbrain.llm.base import LLMError, Usage
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import ChunkNotes, PageCreate, PageUpdate, WikiPlan
from secondbrain.llm.usage import full_price
from secondbrain.models import FilterSettings
from secondbrain.profile.loader import load_profile
from secondbrain.urls import content_hash
from secondbrain.wiki.ingest import (
    MAX_MAP_NOTES_CHARS,
    apply_plan,
    estimate_ingest_cost,
    ingest_item,
    pending_items,
)
from secondbrain.wiki.loop import related_knowledge
from secondbrain.wiki.store import Page, new_meta, open_wiki

TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 10, tzinfo=UTC)
PROFILE = load_profile(FIXTURES / "profile.md")


@pytest.fixture
def material_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "brain.db")
    wiki = open_wiki(tmp_path / "wiki", tmp_path / "wiki" / "raw")
    yield conn, wiki
    conn.close()


def add_material(conn, key, body, *, title="KV cache notes", summary=None, completeness="full"):
    item_id, _, _ = db.persist_material(
        conn,
        db.NewItem(
            source_id=None,
            url="",
            url_canonical=f"text:{key}",
            content_hash=content_hash(title, body),
            title=title,
            author=None,
            summary=summary,
            content=body,
            original_text=body,
            extract_method="manual_text",
            published_at=None,
            completeness=completeness,
            origin_kind="text",
            origin_locator=key,
        ),
        NOW,
    )
    conn.commit()
    return item_id


def ingest(conn, wiki, item_id, backend=None, settings=None, **kwargs):
    return ingest_item(
        conn, wiki, backend or FakeBackend(), PROFILE, db.item_detail(conn, item_id),
        settings or FilterSettings(), TODAY, **kwargs,
    )


class RecordingFake(FakeBackend):
    def __init__(self, *, fail_at=None, fail_schema=None, note=None):
        self.calls = []
        self.usages = []
        self.events = []
        self.fail_at = fail_at
        self.fail_schema = fail_schema
        self.note = note

    def model_name(self, tier):
        return f"priced-{tier}"

    def complete_json(self, messages, schema, tier):
        user = messages[-1]["content"]
        payload = json.loads(user[user.index("{"):])
        self.calls.append((messages, schema, tier, payload))
        self.events.append(("start", schema))
        if len(self.calls) == self.fail_at or schema is self.fail_schema:
            raise LLMError("test failure", Usage(self.model_name(tier), 7, 0, 7, 2))
        response, usage = super().complete_json(messages, schema, tier)
        if schema is ChunkNotes and self.note is not None:
            response = ChunkNotes(summary=self.note)
        self.usages.append(usage)
        self.events.append(("success", schema))
        return response, usage


def long_settings():
    return FilterSettings.model_validate({
        "wiki": {"ingest_max_chars": 1000},
        "l2": {"chunk_chars": 1000, "chunk_overlap_chars": 100, "max_chunks": 20},
        "pricing": {
            "priced-cheap": {"input_cache_hit": 0.1, "input_cache_miss": 1, "output": 3},
            "priced-strong": {"input_cache_hit": 0.2, "input_cache_miss": 2, "output": 6},
        },
    })


def long_body():
    return "\n".join(f"segment {i}: " + "x" * 150 for i in range(40)) + "\nFINAL_TAIL_9f8a"


def test_unread_material_ingests_without_feedback_l2_or_recommendation(material_env):
    conn, wiki = material_env
    body = "KV cache stores attention keys and values."
    item_id = add_material(conn, "unread", body)
    before = db.item_detail(conn, item_id)
    backend = RecordingFake()
    result, _, messages = ingest(conn, wiki, item_id, backend)

    assert [call[1] for call in backend.calls] == [WikiPlan]
    assert result.created[0] == f"material-{item_id}"
    payload = json.loads(messages[-1]["content"].split("\n", 1)[1])["item"]
    assert payload["text"] == body and payload["text_kind"] == "full"
    assert payload["key_points"] == []
    row = db.item_detail(conn, item_id)
    assert row["reading_state"] == before["reading_state"] == "unread"
    assert row["read_at"] is before["read_at"] is None
    assert row["status"] == before["status"]
    assert row["processing_status"] == "processed"
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert db.ingested_ids(conn) == {item_id}
    assert all(page.type != "paper" for page in wiki.pages.values())
    assert "mastery" not in wiki.pages["kv-cache"].meta
    assert "last_reviewed" not in wiki.pages["kv-cache"].meta
    assert related_knowledge(wiki, [row], 500, 2000) == ""
    from secondbrain import ops

    ops.register_read(item_id)
    wiki.reload_pages()
    assert related_knowledge(wiki, [row], 500, 2000) == ""
    assert ops.ingest_items([item_id], lambda _: None, require_read=True).cached == [item_id]
    wiki.reload_pages()
    assert "KV cache（concept）" in related_knowledge(wiki, [row], 500, 2000)


def test_stable_material_ids_and_repeat_do_not_duplicate_sections(material_env):
    conn, wiki = material_env
    first = add_material(conn, "first", "KV cache stores keys.", title="Same title")
    second = add_material(conn, "second", "KV cache also stores values.", title="Same title")
    ingest(conn, wiki, first)
    ingest(conn, wiki, second)
    bodies = {slug: page.body for slug, page in wiki.pages.items()}
    ingest(conn, wiki, first)
    ingest(conn, wiki, second)
    assert {slug: page.body for slug, page in wiki.pages.items()} == bodies
    assert {page.slug for page in wiki.pages.values() if page.type == "material"} == {
        f"material-{first}", f"material-{second}",
    }
    assert wiki.pages["kv-cache"].meta["sources"] == [first, second]
    for item_id in (first, second):
        body = wiki.pages[f"material-{item_id}"].body
        assert body.count("## 整理") == 1
        assert f"[[material-{item_id}|" in wiki.pages["kv-cache"].body


def test_external_notes_mastery_and_legacy_paper_survive_reprocessing(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "notes", "KV cache details.", title="Legacy paper")
    meta = new_meta("KV cache", "concept", TODAY)
    meta.update(mastery="扎实", interval_days=45)
    meta["custom"] = {"personal": "保留"}
    original = "# KV cache\n\n## 个人笔记\n\n手写观察 [[legacy-paper|旧论文]]。\n"
    wiki.save_page(Page("kv-cache", meta, original))
    legacy = Page("legacy-paper", new_meta("Legacy paper", "paper", TODAY), "# 旧论文\n\n旧笔记")
    legacy_path = wiki.save_page(legacy)
    legacy_bytes = legacy_path.read_bytes()
    notes = wiki.root / "notes" / f"material-{item_id}-notes.md"
    notes.parent.mkdir()
    notes.write_text("# 我的独立笔记\n\n不要改写。\n", encoding="utf-8")
    note_bytes = notes.read_bytes()
    plan = WikiPlan(
        creates=[PageCreate(
            slug="model-invented-title", type="material", title="Legacy paper",
            summary="材料总结", body="## 材料陈述\n\n见 [[kv-cache|键值缓存]]。",
        )],
        updates=[PageUpdate(
            slug="kv-cache", action="replace_section", section="个人笔记", text="机器建议而非替换",
        )],
    )
    row = db.item_detail(conn, item_id)
    apply_plan(wiki, plan, row, PROFILE, TODAY)
    concept_path = wiki.pages["kv-cache"].path
    external = "\n## Obsidian 新增\n\n外部编辑保留 $x^2$ 与 [[legacy-paper]]。\n"
    concept_path.write_text(concept_path.read_text(encoding="utf-8") + external, encoding="utf-8")
    apply_plan(wiki, plan, row, PROFILE, TODAY)
    once = {slug: page.body for slug, page in wiki.pages.items()}
    apply_plan(wiki, plan, row, PROFILE, TODAY)
    assert {slug: page.body for slug, page in wiki.pages.items()} == once
    concept = wiki.pages["kv-cache"]
    assert original.strip() in concept.body and external.strip() in concept.body
    assert concept.meta == {**meta, "sources": [item_id]}
    assert concept.body.count("机器建议而非替换") == 1
    material = wiki.pages[f"material-{item_id}"]
    assert "[[kv-cache|键值缓存]]" in material.body
    assert f"(../../notes/material-{item_id}-notes.md)" in material.body
    assert "[[legacy-paper|Legacy paper]]" in material.body
    assert "model-invented-title" not in wiki.pages
    assert notes.read_bytes() == note_bytes
    assert legacy_path.read_bytes() == legacy_bytes


def test_long_material_maps_every_segment_and_final_tail(material_env):
    conn, wiki = material_env
    body = long_body()
    item_id = add_material(conn, "long", body)
    backend = RecordingFake()
    callbacks = []

    def account(stage, usage):
        callbacks.append((stage, usage))
        backend.events.append(("callback", ChunkNotes if stage == "WIKI_MAP" else WikiPlan))

    result, estimate, _ = ingest(conn, wiki, item_id, backend, long_settings(), on_usage=account)
    maps = [call[3]["text"] for call in backend.calls if call[1] is ChunkNotes]
    assert len(maps) > 1
    assert all(any(line in chunk for chunk in maps) for line in body.splitlines())
    assert "FINAL_TAIL_9f8a" in maps[-1]
    final = backend.calls[-1][3]["item"]
    assert final["text_kind"] == "chunk_notes"
    assert final["coverage"] == "all_available_text"
    assert final["full_length_chars"] == len(body)
    assert final["processed_chunks"] == len(maps)
    assert "FINAL_TAIL_9f8a" in final["text"]
    assert "FINAL_TAIL_9f8a" in wiki.pages[f"material-{item_id}"].body
    assert body in wiki.raw_path(item_id).read_text(encoding="utf-8")
    assert callbacks == result.calls
    assert [stage for stage, _ in callbacks] == ["WIKI_MAP"] * len(maps) + ["WIKI"]
    assert result.usage is callbacks[-1][1]
    assert estimate >= sum(full_price(usage, long_settings().pricing) for _, usage in callbacks)
    for i in range(0, len(backend.events), 3):
        assert [event[0] for event in backend.events[i:i + 3]] == ["start", "success", "callback"]


def test_estimate_reserves_reduce_expansion_and_output_caps_without_calls(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "cost", long_body())
    settings = long_settings()
    backend = RecordingFake(note="\\\x00中文" * 800)
    result, estimate, preview = ingest(
        conn, wiki, item_id, backend, settings, estimate_only=True,
    )
    assert result is None and estimate > 0 and backend.calls == []
    preview_item = json.loads(preview[-1]["content"].split("\n", 1)[1])["item"]
    assert preview_item["text_kind"] == "chunk_notes"
    assert not wiki.raw_path(item_id).exists()
    assert db.ingested_ids(conn) == set()
    result, actual_estimate, _ = ingest(conn, wiki, item_id, backend, settings)
    assert actual_estimate == estimate
    actual_upper = sum(
        estimate_ingest_cost(messages, backend.model_name(tier), settings, tier=tier)
        for messages, _, tier, _ in backend.calls
    )
    assert estimate >= actual_upper
    assert len(result.calls) == len(backend.calls)


@pytest.mark.parametrize("failure", ["map", "final"])
def test_failure_keeps_prior_usage_and_does_not_mark_processed(material_env, failure):
    conn, wiki = material_env
    item_id = add_material(conn, "failed", long_body())
    backend = RecordingFake(fail_at=2 if failure == "map" else None,
                            fail_schema=WikiPlan if failure == "final" else None)
    recorded = []
    with pytest.raises(LLMError, match="test failure") as caught:
        ingest(conn, wiki, item_id, backend, long_settings(),
               on_usage=lambda stage, usage: recorded.append((stage, usage)))
    assert caught.value.stage == ("WIKI_MAP" if failure == "map" else "WIKI")
    assert caught.value.usage.prompt_tokens == 7
    assert len(recorded) == len(backend.calls) - 1
    assert all(stage == "WIKI_MAP" for stage, _ in recorded)
    assert db.item_detail(conn, item_id)["processing_status"] == "pending"
    assert db.ingested_ids(conn) == set()
    assert not wiki.raw_path(item_id).exists()
    assert wiki.pages == {}


def test_oversized_map_notes_are_rejected_after_accounting_not_truncated(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "huge-notes", long_body())
    backend = RecordingFake(note="z" * (MAX_MAP_NOTES_CHARS + 1))
    recorded = []
    with pytest.raises(LLMError, match="未截断") as caught:
        ingest(conn, wiki, item_id, backend, long_settings(),
               on_usage=lambda stage, usage: recorded.append((stage, usage)))
    assert caught.value.stage == "WIKI_MAP" and caught.value.usage is None
    assert len(recorded) == len(backend.calls) == 1
    assert db.ingested_ids(conn) == set()
    assert db.item_detail(conn, item_id)["processing_status"] == "pending"


def test_too_many_chunks_fail_before_spending_or_writing(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "too-long", long_body())
    settings = long_settings()
    settings.l2.max_chunks = 1
    backend = RecordingFake()
    with pytest.raises(LLMError, match="不会只处理开头") as caught:
        ingest(conn, wiki, item_id, backend, settings)
    assert caught.value.stage == "WIKI_MAP"
    assert backend.calls == [] and wiki.pages == {}
    assert db.ingested_ids(conn) == set()


def test_completed_material_becomes_pending_again_after_content_improvement(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "improved", "KV cache abstract.", completeness="abstract")
    db.add_feedback(conn, item_id, "gained", None, NOW)
    conn.commit()
    assert pending_items(conn, wiki) == [item_id]
    ingest(conn, wiki, item_id)
    assert pending_items(conn, wiki) == []
    improved = "KV cache full text with detailed implementation.\nNew ending to preserve."
    assert add_material(conn, "improved", improved) == item_id
    assert pending_items(conn, wiki) == [item_id]
    ingest(conn, wiki, item_id)
    assert pending_items(conn, wiki) == []
    assert db.ingested_ids(conn) == {item_id}
    material = wiki.pages[f"material-{item_id}"]
    assert material.meta["content_completeness"] == "full"
    assert "New ending to preserve." in material.body
    assert improved in wiki.raw_path(item_id).read_text(encoding="utf-8")


def test_abstract_is_labeled_and_missing_text_never_marks_success(material_env):
    conn, wiki = material_env
    item_id = add_material(
        conn, "abstract", "", summary="Only an abstract.", completeness="abstract"
    )
    backend = RecordingFake()
    ingest(conn, wiki, item_id, backend)
    payload = backend.calls[-1][3]["item"]
    assert payload["text"] == "Only an abstract." and payload["text_kind"] == "abstract"
    missing = add_material(conn, "missing", "", title="No text", completeness="missing")
    with pytest.raises(LLMError, match="补全材料") as caught:
        ingest(conn, wiki, missing, backend)
    assert caught.value.stage == "WIKI"
    assert missing not in db.ingested_ids(conn)
    assert db.item_detail(conn, missing)["processing_status"] == "pending"


def test_original_material_text_wins_over_cached_short_content(material_env):
    conn, wiki = material_env
    original = long_body()
    item_id = add_material(conn, "original", original)
    conn.execute("UPDATE items SET content = ? WHERE id = ?", ("cached short preview", item_id))
    conn.commit()
    backend = RecordingFake()
    ingest(conn, wiki, item_id, backend, long_settings())
    maps = [call[3]["text"] for call in backend.calls if call[1] is ChunkNotes]
    assert any("FINAL_TAIL_9f8a" in text for text in maps)
    assert all("cached short preview" not in text for text in maps)
    assert original in wiki.raw_path(item_id).read_text(encoding="utf-8")


def test_related_content_is_not_filtered_by_old_mastery_or_page_type(material_env):
    conn, wiki = material_env
    item_id = add_material(conn, "knowledge", "KV cache connects vLLM and a material.")
    for slug, page_type, mastery in (
        ("kv-cache", "concept", "陌生"),
        ("vllm", "entity", "扎实"),
        ("material", "material", "扎实"),
    ):
        meta = new_meta(slug, page_type, TODAY)
        meta["mastery"] = mastery
        wiki.save_page(Page(slug, meta, f"# {slug}"))
    context = related_knowledge(wiki, [db.item_detail(conn, item_id)], 500, 2000)
    assert "kv-cache" in context and "vllm" in context
    assert "扎实" not in context and "陌生" not in context
