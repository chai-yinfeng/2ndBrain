"""阅读登记、编译来源与人工连接约束；仅临时 SQLite/vault 和 fake 后端。"""

import json
from datetime import UTC, date, datetime

import pytest
from conftest import FIXTURES

from secondbrain import db
from secondbrain.llm.base import LLMError
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import PageCreate, PageUpdate, WikiPlan
from secondbrain.models import FilterSettings
from secondbrain.profile.loader import load_profile
from secondbrain.urls import content_hash
from secondbrain.wiki.graph import build_graph, link_index, page_connections
from secondbrain.wiki.ingest import (
    _apply_update,
    apply_plan,
    ingest_item,
    ingest_messages,
    item_payload,
    material_input_hash,
    pending_items,
    promote_material,
    related_pages,
)
from secondbrain.wiki.lint import lint
from secondbrain.wiki.relations import (
    human_relations,
    page_identity,
    relation_controls,
    set_relation,
)
from secondbrain.wiki.store import Page, WikiError, new_meta, open_wiki

TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 10, tzinfo=UTC)
PROFILE = load_profile(FIXTURES / "profile.md")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "brain.db")
    wiki = open_wiki(tmp_path / "wiki", tmp_path / "wiki" / "raw")
    yield conn, wiki
    conn.close()


def material(conn, *, read=False, title="External learning", text="Original material facts"):
    item_id, _, _ = db.persist_material(
        conn,
        db.NewItem(
            source_id=None, url="", url_canonical=f"text:{title}",
            content_hash=content_hash(title, text), title=title, author=None,
            summary=None, content=text, original_text=text, extract_method="manual_text",
            published_at=None, completeness="full", origin_kind="text", origin_locator=title,
        ),
        NOW,
    )
    if read:
        conn.execute("UPDATE items SET reading_state = 'read' WHERE id = ?", (item_id,))
    conn.commit()
    return db.item_detail(conn, item_id)


def page(wiki, slug, *, title=None, body="", status=None, aliases=None):
    meta = new_meta(title or slug, "concept", TODAY, aliases=aliases, mastery="陌生")
    if status:
        meta["knowledge_status"] = status
    result = Page(slug, meta, body or f"# {title or slug}\n")
    wiki.save_page(result)
    return result


def compile_plan(wiki, row, *, text="材料陈述：原文事实；见 [[concept-a]]。", updates=None):
    plan = WikiPlan(
        summary="材料整理",
        creates=[
            PageCreate(
                slug=f"material-{row['id']}", type="material", title=row["title"],
                summary="材料摘要", body=text,
            ),
            PageCreate(
                slug="concept-a", type="concept", title="Concept A",
                summary="概念摘要", body="材料陈述：概念说明。",
            ),
        ],
        updates=updates or [],
    )
    return apply_plan(wiki, plan, row, PROFILE, TODAY)


def test_unread_drafts_are_opt_in_and_do_not_break_index_lint(env):
    conn, wiki = env
    row = material(conn)
    compile_plan(wiki, row)
    assert {p.knowledge_status for p in wiki.pages.values()} == {"draft"}
    assert build_graph(wiki, TODAY) == {"nodes": [], "edges": []}
    assert link_index(wiki) == {}
    assert wiki.concepts() == []
    full = build_graph(wiki, TODAY, include_drafts=True)
    assert {node["knowledge_status"] for node in full["nodes"]} == {"draft"}
    assert len(full["nodes"]) == 2 and len(full["edges"]) == 1
    assert page_connections(wiki, wiki.pages["concept-a"])["backlinks"] == []
    assert page_connections(wiki, wiki.pages["concept-a"], include_drafts=True)["backlinks"]
    index = wiki.index_path.read_text(encoding="utf-8")
    assert "[[concept-a]]" not in index
    assert lint(wiki).index_missing == []
    assert lint(wiki).broken_links == {}


def test_recorded_links_to_hidden_drafts_are_not_missing(env):
    _, wiki = env
    draft = page(wiki, "draft", status="draft")
    recorded = page(wiki, "recorded", body="[[draft]]", status="recorded")
    wiki.rebuild_index()
    connections = page_connections(wiki, recorded)
    assert connections["outgoing"] == connections["missing"] == []
    assert {node["id"] for node in build_graph(wiki, TODAY)["nodes"]} == {"recorded"}
    assert lint(wiki).index_missing == []
    assert lint(wiki).broken_links == {}
    assert page_connections(wiki, recorded, include_drafts=True)["outgoing"] == [draft]


def test_read_compilation_records_but_never_promotes_mastery_or_downgrades(env):
    conn, wiki = env
    row = material(conn, read=True)
    compile_plan(wiki, row)
    concept = wiki.pages["concept-a"]
    assert all(p.knowledge_status == "recorded" for p in wiki.pages.values())
    assert concept.meta["mastery"] == "陌生"
    assert concept.meta["last_reviewed"] is None
    conn.execute("UPDATE items SET reading_state = 'unread' WHERE id = ?", (row["id"],))
    conn.commit()
    compile_plan(wiki, db.item_detail(conn, row["id"]))
    assert all(p.knowledge_status == "recorded" for p in wiki.pages.values())
    assert wiki.pages["concept-a"].meta["mastery"] == "陌生"


def test_unmarked_legacy_page_is_truthful_and_never_rewritten(env):
    _, wiki = env
    legacy = page(wiki, "legacy", body="# Legacy\n\n我的历史文字，不知道何时读过。\n")
    original = legacy.path.read_bytes()
    wiki.reindex()
    assert wiki.pages["legacy"].knowledge_status == "legacy"
    assert "knowledge_status" not in wiki.pages["legacy"].meta
    node = build_graph(wiki, TODAY)["nodes"][0]
    assert node["knowledge_status"] == "legacy"
    assert "历史页：阅读状态未知" in wiki.index_path.read_text(encoding="utf-8")
    assert legacy.path.read_bytes() == original


def test_cached_promotion_changes_only_status_of_referenced_generated_pages(env):
    conn, wiki = env
    row = material(conn)
    compile_plan(wiki, row)
    manual = page(wiki, "manual", body="# Manual\n\n个人原文\n")
    manual_bytes = manual.path.read_bytes()
    concept = wiki.pages["concept-a"]
    concept.body += "\n## 个人补充\n\n我自己的观察 $x^2$。\n"
    concept.meta["mastery"] = "扎实"
    wiki.save_page(concept)
    bodies = {slug: p.body for slug, p in wiki.pages.items()}
    old_meta = {slug: p.meta.copy() for slug, p in wiki.pages.items()}
    updated = promote_material(wiki, row["id"])
    assert set(updated) == {f"material-{row['id']}", "concept-a"}
    for slug in updated:
        assert wiki.pages[slug].body == bodies[slug]
        assert wiki.pages[slug].meta == {**old_meta[slug], "knowledge_status": "recorded"}
    assert wiki.pages["concept-a"].meta["mastery"] == "扎实"
    assert manual.path.read_bytes() == manual_bytes
    assert promote_material(wiki, row["id"]) == []
    assert promote_material(wiki, 999999) == []


def test_input_hash_is_content_and_user_notes_not_reading_flag(env):
    conn, wiki = env
    row = material(conn)
    original = material_input_hash(row, "外部学习记录")
    conn.execute("UPDATE items SET reading_state = 'read' WHERE id = ?", (row["id"],))
    conn.commit()
    read_row = db.item_detail(conn, row["id"])
    assert material_input_hash(read_row, "外部学习记录") == original
    assert material_input_hash(read_row, "新增理解") != original
    conn.execute("UPDATE items SET original_text = '新增材料事实' WHERE id = ?", (row["id"],))
    conn.commit()
    assert material_input_hash(db.item_detail(conn, row["id"]), "外部学习记录") != original


def test_external_notes_enter_payload_separately_and_find_relevant_context(env):
    conn, wiki = env
    row = material(conn, read=True)
    page(
        wiki, "only-in-notes", title="OnlyInUserNotes",
        body="# Concept\n\n## 个人笔记\n\n我的解释不能改。",
    )
    path = wiki.root / "notes" / f"material-{row['id']}-notes.md"
    path.parent.mkdir(exist_ok=True)
    notes = "# ChatGPT 外部学习记录\n\nOnlyInUserNotes 是我想关联的概念；我的问题仍未解决。\n"
    path.write_text(notes, encoding="utf-8")
    original = path.read_bytes()
    result, _, messages = ingest_item(
        conn, wiki, FakeBackend(), PROFILE, row, FilterSettings(), TODAY,
    )
    payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert payload["item"]["text"] == "Original material facts"
    assert payload["item"]["user_notes"] == notes
    assert payload["item"]["reading_state"] == "read"
    assert "用户" in payload["item"]["user_notes_provenance"]
    assert "only-in-notes" in {p["slug"] for p in payload["related_pages"]}
    assert payload["human_guidance"][0]["user_notes"] == "我的解释不能改。"
    assert result.error is None
    assert path.read_bytes() == original
    assert wiki.pages[f"material-{row['id']}"].meta["input_hash"] == material_input_hash(row, notes)


def test_pin_reason_and_personal_content_survive_recompile(env):
    conn, wiki = env
    row = material(conn, read=True)
    compile_plan(wiki, row)
    other = page(wiki, "other", title="其他概念", body="# Other\n\n用户原文不覆盖。\n")
    other_bytes = other.path.read_bytes()
    reason = "我用这个例子理解边界。\n保留我的措辞 $x^2$。"
    result = set_relation(wiki, "Concept A", "其他概念", "pin", reason, TODAY)
    assert result["source"] == "concept-a"
    compile_plan(wiki, row, updates=[PageUpdate(
        slug="concept-a", action="replace_section", section="人工关系", text="机器不能替代人工关系",
    )])
    concept = wiki.pages["concept-a"]
    entry = human_relations(concept)[0]
    assert entry["target"] == page_identity(wiki, wiki.pages["other"])
    assert entry["owner"] == "human" and entry["mode"] == "pin" and entry["reason"] == reason
    assert "[[pages/concepts/other.md]]" in concept.body
    assert "保留我的措辞 $x^2$。" in concept.body
    assert wiki.pages["other"] in page_connections(wiki, concept)["outgoing"]
    assert other.path.read_bytes() == other_bytes


def test_block_hides_both_directions_without_deleting_user_prose(env):
    _, wiki = env
    original_a = "# A\n\n## 个人笔记\n\n我的判断仍提到 [[b|B]]，保留此句。\n"
    original_b = "# B\n\n## 个人笔记\n\n反向个人文字 [[a]] 也不删除。\n"
    a = page(wiki, "a", body=original_a)
    b = page(wiki, "b", body=original_b, aliases=["B alias"])
    b_bytes = b.path.read_bytes()
    reason = "不是同一层次，不应自动关联。"
    set_relation(wiki, "a", "B alias", "block", reason, TODAY)
    wiki.reindex()
    assert original_a.strip() in wiki.pages["a"].body
    assert wiki.pages["b"].body == original_b
    assert b.path.read_bytes() == b_bytes
    assert human_relations(wiki.pages["a"])[0]["reason"] == reason
    assert build_graph(wiki, TODAY)["edges"] == []
    assert page_connections(wiki, a)["outgoing"] == []
    assert page_connections(wiki, b)["backlinks"] == []
    assert lint(wiki).broken_links == {}


def test_blocks_reject_new_machine_text_links_and_automatic_material_edges(env):
    conn, wiki = env
    row = material(conn, read=True)
    compile_plan(wiki, row)
    material_slug = f"material-{row['id']}"
    set_relation(wiki, material_slug, "concept-a", "block", "人工禁止双向自动边", TODAY)
    compile_plan(wiki, row, text="材料陈述：不应链接 [[concept-a|Concept A]]。", updates=[
        PageUpdate(slug="concept-a", action="add_links", links=[material_slug]),
        PageUpdate(
            slug="concept-a", action="append_section", section="关联",
            text=f"机器推断：反向也不允许 [[{material_slug}]]。",
        ),
    ])
    assert build_graph(wiki, TODAY)["edges"] == []
    assert wiki.pages[material_slug].links() == set()
    assert wiki.pages["concept-a"].links() == set()
    assert "Concept A" in wiki.pages[material_slug].body
    assert human_relations(wiki.pages[material_slug])[0]["reason"] == "人工禁止双向自动边"
    settings = FilterSettings()
    payload = item_payload(row, None, settings)
    messages = ingest_messages(wiki, payload, related_pages(wiki, payload, settings), settings)
    context = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert context["human_relations"][0]["mode"] == "block"
    assert context["human_relations"][0]["reason"] == "人工禁止双向自动边"


def test_block_keeps_code_and_unrelated_links_and_reset_does_not_restore_old_machine_edge(env):
    _, wiki = env
    page(
        wiki, "a",
        body="# A\n\n## 机器关联\n\n- [[b|展示]]\n- [[c]]\n\n```\n[[b]]\n```\n",
    )
    page(wiki, "b", body="# B\n\n## 机器整理 · 材料 42\n\n材料陈述：[[a]]。\n")
    page(wiki, "c")
    set_relation(wiki, "a", "b", "block", "不连接", TODAY)
    assert "```\n[[b]]\n```" in wiki.pages["a"].body
    assert "[[c]]" in wiki.pages["a"].body
    assert "展示" in wiki.pages["a"].body
    assert wiki.pages["b"].links() == set()
    set_relation(wiki, "b", "a", "reset", today=TODAY)
    assert human_relations(wiki.pages["a"]) == []
    assert human_relations(wiki.pages["b"]) == []
    edges = build_graph(wiki, TODAY)["edges"]
    assert edges == [{"source": "a", "target": "c"}]
    a = wiki.pages["a"]
    assert _apply_update(wiki, a, PageUpdate(slug="a", action="add_links", links=["b"]), 42) is None
    wiki.save_page(a)
    assert wiki.pages["b"] in page_connections(wiki, a)["outgoing"]


def test_reset_pin_removes_only_managed_relation_and_keeps_other_user_content(env):
    _, wiki = env
    original = "# A\n\n## 人工关系\n\n我的自由文字与 [[c]]，不是API条目。\n"
    page(wiki, "a", body=original)
    page(wiki, "b")
    page(wiki, "c")
    set_relation(wiki, "a", "b", "pin", "临时保留", TODAY)
    set_relation(wiki, "a", "b", "reset", today=TODAY)
    assert original.strip() in wiki.pages["a"].body
    assert human_relations(wiki.pages["a"]) == []
    assert wiki.pages["a"].links() == {"c"}


def test_ambiguous_missing_self_and_invalid_targets_never_modify_files(env):
    _, wiki = env
    page(wiki, "a")
    page(wiki, "b", title="共享标题", aliases=["共享别名"])
    page(wiki, "c", title="共享标题", aliases=["共享别名"])
    original = {path: path.read_bytes() for path in wiki.root.rglob("*.md")}
    cases = [
        ("共享标题", "pin"), ("共享别名", "block"), ("missing", "pin"),
        ("a", "pin"), ("b", "other"),
    ]
    for target, mode in cases:
        with pytest.raises(WikiError):
            set_relation(wiki, "a", target, mode, "原因", TODAY)
        assert {path: path.read_bytes() for path in wiki.root.rglob("*.md")} == original
    set_relation(wiki, "a", "pages/concepts/b.md", "pin", "明确路径", TODAY)
    assert human_relations(wiki.pages["a"])[0]["target"] == "pages/concepts/b.md"


def test_manual_reason_is_durable_but_prompt_context_is_bounded(env):
    conn, wiki = env
    row = material(conn)
    page(wiki, "a", title="Original", body="# A\n\n## 个人笔记\n\n" + "人工解释" * 10000)
    page(wiki, "b")
    reason = "不能连接的长原因" * 10000
    set_relation(wiki, "a", "b", "block", reason, TODAY)
    settings = FilterSettings()
    payload = item_payload(row, None, settings)
    messages = ingest_messages(wiki, payload, [wiki.pages["a"]], settings)
    context = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert len(context["human_relations"][0]["reason"]) <= 600
    assert len(context["human_guidance"][0]["user_notes"]) <= 1200
    wiki.reload_pages()
    assert human_relations(wiki.pages["a"])[0]["reason"] == reason


def test_batch_queue_uses_read_registration_without_gained_feedback(env):
    conn, _ = env
    unread = material(conn, title="Unread")
    read = material(conn, read=True, title="Registered read", text="Registered material facts")
    assert read["id"] in pending_items(conn)
    assert unread["id"] not in pending_items(conn)


def test_relation_display_includes_reverse_control_and_canonical_target_survives_collisions(env):
    _, wiki = env
    page(wiki, "a", body="[[pages/concepts/b.md]]")
    page(wiki, "b")
    set_relation(wiki, "a", "b", "block", "从A管理的决定，B也应解释", TODAY)
    controls = relation_controls(wiki, wiki.pages["b"])
    assert len(controls) == 1
    assert controls[0]["incoming"] is True
    assert controls[0]["other"] == "a"
    assert controls[0]["reason"] == "从A管理的决定，B也应解释"
    assert controls[0]["source"] == "a" and controls[0]["target"] == "b"
    wiki.save_page(Page("b", new_meta("另一个B", "entity", TODAY), "# 不同的页面"))
    wiki.reindex()
    assert wiki.find("b") is None
    target = wiki.find("pages/concepts/b.md")
    assert relation_controls(wiki, target)[0]["target"] == "pages/concepts/b.md"
    assert page_connections(wiki, wiki.pages["a"])["outgoing"] == []
    assert build_graph(wiki, TODAY)["edges"] == []


def test_missing_original_uses_only_learning_notes_and_empty_input_never_calls_model(env):
    conn, wiki = env
    row = material(conn, read=True, text="")

    class RecordingFake(FakeBackend):
        def __init__(self):
            self.calls = []

        def complete_json(self, messages, schema, tier):
            self.calls.append(messages)
            return super().complete_json(messages, schema, tier)

    backend = RecordingFake()
    with pytest.raises(LLMError, match="学习笔记"):
        ingest_item(conn, wiki, backend, PROFILE, row, FilterSettings(), TODAY)
    assert backend.calls == []
    assert wiki.pages == {}
    notes = "我在外部讨论后理解了这个概念；这不是抓取的原文。"
    result, _, messages = ingest_item(
        conn, wiki, backend, PROFILE, row, FilterSettings(), TODAY, user_notes=notes,
    )
    payload = json.loads(messages[-1]["content"].split("\n", 1)[1])["item"]
    assert result.error is None and len(backend.calls) == 1
    assert payload["text"] == "" and payload["text_kind"] == "missing"
    assert payload["user_notes"] == notes and payload["compilation_basis"] == "user_notes_only"
    generated = wiki.pages[f"material-{row['id']}"]
    assert generated.meta["content_completeness"] == "missing"
    assert generated.meta["compilation_basis"] == "user_notes_only"
    assert generated.meta["input_hash"] == material_input_hash(row, notes)
    assert "仅依据用户学习记录；原文缺失" in generated.body
