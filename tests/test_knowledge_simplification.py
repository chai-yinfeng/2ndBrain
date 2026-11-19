"""分类迁移、真实生成消费者与回收防复活；仅使用临时数据库和 vault。"""

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from secondbrain import db
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import PageCreate, PageUpdate, ProfileProposal, WikiPlan
from secondbrain.models import FilterSettings
from secondbrain.pipeline.run import backfill_priority, run_pipeline, system_prompt
from secondbrain.profile.loader import parse_profile
from secondbrain.wiki import sync
from secondbrain.wiki.classification import classify_material, sync_material_kinds
from secondbrain.wiki.graph import build_graph, page_connections
from secondbrain.wiki.ingest import apply_plan, ingest_item, pending_items, promote_material
from secondbrain.wiki.lint import lint
from secondbrain.wiki.loop import related_knowledge
from secondbrain.wiki.store import Page, WikiError, new_meta, open_wiki

TODAY = date(2026, 11, 18)
NOW = datetime(2026, 11, 18, tzinfo=UTC)
PROFILE = parse_profile("# 兴趣\n\n## 关注领域\n\n- KV cache 与推理服务\n")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    conn = db.connect()
    wiki = open_wiki(tmp_path / "wiki", tmp_path / "wiki" / "raw")
    yield conn, wiki
    conn.close()


def material(conn, *, key="article", url="https://example.test/post", read=True):
    text = "KV cache stores attention keys and values. Its size grows with the context."
    item_id, _, _ = db.persist_material(conn, db.NewItem(
        source_id=None, url=url, url_canonical=url or f"text:{key}",
        content_hash=f"hash-{key}", title="Understanding KV cache", author=None,
        summary=None, content=text, original_text=text, extract_method="manual_text",
        published_at=None, completeness="full", origin_kind="text", origin_locator=key,
    ), NOW)
    if read:
        db.set_reading_state(conn, item_id, "read")
    conn.commit()
    return db.item_detail(conn, item_id)


@pytest.mark.parametrize(("row", "facts", "expected"), [
    ({"url": "https://example.test/post", "title": "A paper about papers"}, {}, "article"),
    ({"url": "https://example.test/p.pdf"}, {}, "other"),
    ({"url": "", "content": "some pasted text"}, {}, "other"),
    ({"url": "https://youtu.be/abcdef"}, {}, "video"),
    ({"url": "https://www.bilibili.com/video/BV123"}, {}, "video"),
    ({"url": "", "source_type": "youtube"}, {}, "video"),
    ({"url": "", "doi": "10.1234/verified"}, {}, "paper"),
    ({"url": "", "arxiv_id": "2401.12345"}, {}, "paper"),
    ({"url": "https://example.test/paper"}, {"kind": "paper", "verified": True}, "paper"),
    ({"url": "https://example.test/paper"}, {"kind": "paper"}, "article"),
])
def test_classification_uses_evidence_not_title(row, facts, expected):
    assert classify_material(row, facts) == expected


def test_metadata_migration_preserves_paths_manual_fields_and_exact_prose(workspace):
    conn, wiki = workspace
    row = material(conn)
    meta = new_meta(row["title"], "material", TODAY)
    meta.update(generated_by="wiki_ingest", material_id=row["id"],
                knowledge_kind="video", trashed=True, custom={"personal": "keep"},
                mastery="扎实", human_relations=[{"mode": "block", "target": "x"}])
    body = "# 资料\r\n\r\n## 个人笔记\r\n\r\n手写：机器整理，未经用户核对；材料 42。\r\n"
    page = Page(f"material-{row['id']}", meta, body)
    path = wiki.save_page(page)
    original_body = path.read_bytes().split(b"---", 2)[2]
    assert sync_material_kinds(wiki, conn) == [page.slug]
    migrated = wiki.pages[page.slug]
    assert migrated.path == path and migrated.meta["material_kind"] == "article"
    assert migrated.kind == "video" and migrated.is_trashed
    assert migrated.meta["custom"] == {"personal": "keep"}
    assert migrated.meta["mastery"] == "扎实"
    assert migrated.meta["human_relations"] == meta["human_relations"]
    assert path.read_bytes().split(b"---", 2)[2] == original_body
    assert sync_material_kinds(wiki, conn) == []
    assert "## 视频" not in wiki.index_path.read_text()  # 回收页不入目录。


def test_only_confirmed_machine_block_is_cleaned(workspace):
    conn, wiki = workspace
    row = material(conn)
    key = f"material-{row['id']}:" + "a" * 20
    manual = "## 个人笔记\n\n机器整理，未经用户核对；我自己写的材料 42。\n"
    body = (
        "# 概念\n\n" + manual + f"\n## 机器整理 · 材料 {row['id']}\n\n"
        f"<!-- {key} -->\n\n> 机器整理，未经用户核对；材料陈述与机器推断分别标注。\n\n"
        "### 事实\n\n有依据的解释。\n\n## 来源\n\n- 手工来源\n"
    )
    page = Page("concept", new_meta("概念", "concept", TODAY), body)
    path = wiki.save_page(page)
    sync_material_kinds(wiki, conn)
    migrated = wiki.pages["concept"]
    assert migrated.path == path and manual in migrated.body
    assert f"## 机器整理 · 材料 {row['id']}" not in migrated.body
    assert migrated.body.count("机器整理，未经用户核对") == 1
    assert f"<!-- wiki-ingest:start {key} -->" in migrated.body
    assert f"<!-- wiki-ingest:end {key} -->" in migrated.body
    assert "有依据的解释。" in migrated.body and "- 手工来源" in migrated.body
    once = path.read_bytes()
    assert sync_material_kinds(wiki, conn) == []
    assert path.read_bytes() == once


def test_actual_ingest_is_classified_without_review_or_visible_material_numbers(workspace):
    conn, wiki = workspace
    row = material(conn)
    result, _, _ = ingest_item(conn, wiki, FakeBackend(), PROFILE, row, FilterSettings(), TODAY)
    assert result.error is None
    page = wiki.material_page(row["id"])
    assert page.kind == "article" and page.meta["material_kind"] == "article"
    assert not {"mastery", "last_reviewed", "interval_days", "ease"}.intersection(page.meta)
    assert "机器整理，未经用户核对" not in page.body
    assert f"机器整理 · 材料 {row['id']}" not in page.body
    assert "<!-- wiki-ingest:start " in page.body
    concept = wiki.find("KV cache")
    assert f"[[{page.slug}|{page.title}]]" in concept.body
    assert "mastery" not in concept.meta
    assert lint(wiki).missing_fields == {}
    node = next(node for node in build_graph(wiki)["nodes"] if node["id"] == page.slug)
    assert node["type"] == "article" and "due" not in node and "mastery" not in node


def test_legacy_paper_identity_is_reused_without_a_material_substitute(workspace):
    conn, wiki = workspace
    row = material(conn)
    old = Page("stable-paper", new_meta("旧论文", "paper", TODAY, sources=[row["id"]]),
               "# 旧论文\n\n## 个人笔记\n\n保留我的推导。\n")
    old_path = wiki.save_page(old)
    sync_material_kinds(wiki, conn)
    result, _, _ = ingest_item(conn, wiki, FakeBackend(), PROFILE, row, FilterSettings(), TODAY)
    assert "stable-paper" in result.updated
    assert f"material-{row['id']}" not in wiki.pages
    page = wiki.material_page(row["id"])
    assert page.path == old_path and page.kind == "paper"
    assert "保留我的推导。" in page.body


def test_trashed_pages_are_excluded_even_from_drafts_and_automatic_consumers(workspace):
    conn, wiki = workspace
    row = material(conn)
    meta = new_meta(row["title"], "material", TODAY, sources=[row["id"]])
    meta.update(material_id=row["id"], generated_by="wiki_ingest", trashed=True,
                knowledge_status="draft", input_hash="old")
    recycled = Page(f"material-{row['id']}", meta, "# 回收资料\n\n## 个人笔记\n\n原样保留\n")
    wiki.save_page(recycled)
    active = Page("active", new_meta("Active", "concept", TODAY), f"[[{recycled.slug}]]")
    wiki.save_page(active)
    original = recycled.path.read_bytes()
    assert not recycled.is_visible
    nodes = build_graph(wiki, include_drafts=True)["nodes"]
    assert recycled.slug not in {node["id"] for node in nodes}
    assert page_connections(wiki, active, include_drafts=True)["outgoing"] == []
    assert wiki.mentioned_in(row["title"], 5) == []
    assert pending_items(conn, wiki) == []
    assert promote_material(wiki, row["id"]) == []
    assert not any(change["needs_ingest"] for change in sync._snapshot(conn, wiki)[0]["changes"])
    with pytest.raises(WikiError, match="知识页已回收"):
        ingest_item(conn, wiki, FakeBackend(), PROFILE, row, FilterSettings(), TODAY)
    assert recycled.path.read_bytes() == original


def test_model_cannot_update_or_recreate_trashed_concept_by_alias(workspace):
    conn, wiki = workspace
    row = material(conn)
    meta = new_meta("KV cache", "concept", TODAY, aliases=["KV 缓存"])
    meta["trashed"] = True
    target = Page("old-kv", meta, "# KV cache\n\n手写关系 [[active]]\n")
    wiki.save_page(target)
    original = target.path.read_bytes()
    plan = WikiPlan(creates=[PageCreate(
        slug="replacement-kv", type="concept", title="KV 缓存", summary="解释", body="不要写入",
    )], updates=[PageUpdate(slug="old-kv", action="add_aliases", aliases=["new alias"])])
    result = apply_plan(wiki, plan, row, PROFILE, TODAY)
    assert len(result.rejected) == 2 and "replacement-kv" not in wiki.pages
    assert target.path.read_bytes() == original


def test_old_mastery_is_not_prompt_input_or_recommendation_state(workspace):
    conn, wiki = workspace
    row = material(conn)
    meta = new_meta("KV cache", "concept", TODAY, summary="存储键值")
    meta.update(mastery="扎实", last_reviewed="2000-01-01", interval_days=1)
    wiki.save_page(Page("kv-cache", meta, "# KV cache"))
    context = related_knowledge(wiki, [row], 500, 2000)
    assert "存储键值" in context and "扎实" not in context
    profile = parse_profile("## 领域掌握度\n\n扎实时 only-new-progress。\n\n"
                            "| 领域 | 掌握度 | 优先级 | 说明 |\n"
                            "|---|---|---|---|\n| KV cache | 扎实 | 高 | 兴趣 |\n")
    prompt = system_prompt("l1_system", profile)
    assert "| 扎实 |" not in prompt and "| KV cache | 高 | 兴趣 |" in prompt
    assert "only-new-progress" not in prompt
    assert "| 扎实 |" in profile.text
    run_pipeline(conn, FilterSettings(), FakeBackend(), profile, limit=None,
                 now=NOW, today=TODAY, wiki=wiki)
    assert conn.execute("SELECT COUNT(*) FROM digest_concepts").fetchone()[0] == 0
    assert backfill_priority({"content_type": "frontier", "topics": ["KV cache"]}) == (
        backfill_priority({"content_type": "frontier", "topics": ["gardening"]})
    )
    with pytest.raises(ValidationError):
        ProfileProposal.model_validate({"mastery_changes": []})
    assert json.loads(ProfileProposal().model_dump_json()) == {"summary": "", "section_edits": []}
