"""M14：Markdown 派生网络、跨端名称解析与外部重新索引。"""

from datetime import date

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient

from secondbrain.llm.schemas import PageCreate, PageUpdate, WikiPlan
from secondbrain.profile.loader import load_profile
from secondbrain.web.app import create_app
from secondbrain.wiki.graph import build_graph, page_connections
from secondbrain.wiki.ingest import _append_machine, _apply_update, apply_plan
from secondbrain.wiki.lint import lint
from secondbrain.wiki.store import Page, WikiError, new_meta, open_wiki

TODAY = date(2026, 11, 16)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    root = tmp_path / "wiki"
    return open_wiki(root, root / "raw")


def add_page(wiki, slug, title, body="", page_type="concept", aliases=None):
    page = Page(slug, new_meta(title, page_type, TODAY, aliases=aliases), body)
    wiki.save_page(page)
    return page


def test_shared_concept_connects_blog_and_paper(wiki):
    concept = add_page(wiki, "kv-cache", "键值缓存", aliases=["KV cache", "KV 缓存"])
    blog = add_page(wiki, "material-1", "博客：推理实践", "[[KV 缓存|核心概念]]", "material")
    blog.meta["material_id"] = 1
    blog.meta["sources"] = [1]
    wiki.save_page(blog)
    paper = add_page(wiki, "paged-attention", "PagedAttention", "[[键值缓存#实现]]", "paper")
    wiki.reindex()

    graph = build_graph(wiki, TODAY)
    assert graph["edges"] == [
        {"source": "kv-cache", "target": "material-1"},
        {"source": "kv-cache", "target": "paged-attention"},
    ]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["kv-cache"]["aliases"] == ["KV cache", "KV 缓存"]
    assert nodes["material-1"]["material_id"] == 1
    assert nodes["material-1"]["sources"] == [1]
    links = page_connections(wiki, concept)
    assert [page.slug for page in links["backlinks"]] == [blog.slug, paper.slug]
    assert links["outgoing"] == links["missing"] == links["ambiguous"] == []
    assert [page.slug for page in page_connections(wiki, blog)["outgoing"]] == [concept.slug]
    assert lint(wiki).broken_links == {}


def test_chinese_filename_aliases_headings_and_exact_paths(wiki):
    concept = add_page(wiki, "注意力机制", "注意力机制", aliases=["Attention", "注意力"])
    source = add_page(
        wiki, "阅读记录", "阅读记录",
        "[[注意力#定义|别名展示]] [[../concepts/注意力机制.md#公式]] "
        "[[pages/concepts/注意力机制|完整路径]] [[#当前段落]]",
        "material",
    )
    assert wiki.resolve("Attention") == [concept]
    assert wiki.find("注意力") is concept
    assert wiki.find("[[注意力#定义|展示]]") is concept
    assert wiki.find("../concepts/注意力机制.md", source=source) is concept
    assert wiki.find("pages/concepts/注意力机制") is concept
    assert wiki.find("注意力机制.md") is concept
    assert source.links() == {
        "注意力", "../concepts/注意力机制.md", "pages/concepts/注意力机制", "",
    }
    links = page_connections(wiki, source)
    assert links["outgoing"] == [concept]
    assert links["missing"] == links["ambiguous"] == []
    graph = build_graph(wiki, TODAY)
    assert len(graph["edges"]) == 1
    assert all(node["url"].startswith("/wiki/page/") for node in graph["nodes"])


def test_duplicate_titles_and_aliases_require_selection(wiki):
    first = add_page(wiki, "first", "共同标题", aliases=["共享别名"])
    second = add_page(wiki, "second", "共同标题", aliases=["共享别名", "first"])
    source = add_page(wiki, "source", "Source", "[[共同标题]] [[共享别名]] [[缺失目标]]")
    wiki.reindex()
    assert [page.slug for page in wiki.resolve("共同标题")] == [first.slug, second.slug]
    assert wiki.find("共同标题") is None
    assert wiki.find("共享别名") is None
    assert wiki.find("first").slug == first.slug  # 精确稳定 ID 胜过另一页的别名。
    links = page_connections(wiki, source)
    assert links["outgoing"] == []
    assert links["missing"] == ["缺失目标"]
    assert set(links["ambiguous"]) == {"共同标题", "共享别名"}
    assert build_graph(wiki, TODAY)["edges"] == []
    report = lint(wiki)
    assert set(report.ambiguous_links) == {"source"}
    assert set(report.ambiguous_links["source"]) == {"共同标题", "共享别名"}
    assert report.broken_links == {"source": ["缺失目标"]}
    assert report.missing_pages == {"缺失目标": 1}

    source = wiki.find("source")
    source.body += "\n[[pages/concepts/second.md|明确选择]]"
    wiki.save_page(source)
    assert [page.slug for page in page_connections(wiki, source)["outgoing"]] == [second.slug]
    backlinks = page_connections(wiki, wiki.find("second"))["backlinks"]
    assert [page.slug for page in backlinks] == [source.slug]


def test_duplicate_stems_are_all_openable_and_write_to_original_paths(wiki):
    first = add_page(wiki, "同名", "概念版本", "个人笔记", "concept")
    second = add_page(wiki, "同名", "论文版本", "原有材料", "paper")
    wiki.reindex()
    ids = {"pages/concepts/同名.md", "pages/papers/同名.md"}
    assert set(wiki.pages) == ids
    assert {page.slug for page in wiki.resolve("同名")} == ids
    assert wiki.find("同名") is None
    assert wiki.find("pages/concepts/同名.md").path == first.path
    assert wiki.find("./同名", source=wiki.find("pages/papers/同名")).path == second.path
    original_paper = second.path.read_bytes()
    concept = wiki.find("pages/concepts/同名")
    concept.body += "\n新的个人笔记"
    assert wiki.save_page(concept) == first.path
    assert second.path.read_bytes() == original_paper
    assert "新的个人笔记" in first.path.read_text(encoding="utf-8")
    graph = build_graph(wiki, TODAY)
    assert {node["id"] for node in graph["nodes"]} == ids
    assert all("/wiki/page/pages/" in node["url"] for node in graph["nodes"])
    client = TestClient(create_app())
    assert all(client.get(node["url"]).status_code == 200 for node in graph["nodes"])
    ambiguous = client.get("/wiki/page/同名")
    assert "概念版本" in ambiguous.text and "论文版本" in ambiguous.text
    assert lint(wiki).index_missing == []
    with pytest.raises(WikiError):
        wiki.save_page(Page("同名", new_meta("第三份", "concept", TODAY), "不得覆盖"))
    second.path.unlink()
    wiki.reindex()
    assert wiki.find("同名").path == first.path
    assert wiki.find("pages/concepts/同名.md").path == first.path
    assert client.get("/wiki/page/pages/concepts/同名.md").status_code == 200
    assert "新的个人笔记" in first.path.read_text(encoding="utf-8")


def test_nested_external_markdown_and_resource_exclusion(wiki):
    folder = wiki.root / "pages" / "concepts" / "外部笔记"
    folder.mkdir()
    (folder / "分块.md").write_text("# 分块\n\n[[../主概念]]", encoding="utf-8")
    add_page(wiki, "主概念", "主概念")
    for folder in (
        wiki.raw_root, wiki.root / "resources", wiki.root / "pages" / "assets",
        wiki.root / "notes", wiki.root / ".obsidian",
    ):
        folder.mkdir(exist_ok=True)
        (folder / "不应入图.md").write_text("[[主概念]]", encoding="utf-8")
    source = add_page(
        wiki, "source", "Source",
        "[[../../raw/不应入图.md]] ![[../../resources/figure.png]] "
        "[[../../notes/不应入图.md]] [[分块]]",
    )
    wiki.reindex()
    assert set(wiki.pages) == {"分块", "主概念", "source"}
    links = page_connections(wiki, source)
    assert [page.slug for page in links["outgoing"]] == ["分块"]
    assert links["missing"] == []
    assert len(build_graph(wiki, TODAY)["edges"]) == 2


def test_external_vault_root_pages_keep_unambiguous_paths(wiki):
    root_page = wiki.root / "外部.md"
    root_page.write_text("# 外部页面\n\n个人笔记", encoding="utf-8")
    wiki.reindex()
    assert wiki.find("外部").path == root_page
    assert wiki.find("外部.md").path == root_page
    assert wiki.raw_link(7, "concept", source=wiki.find("外部")) == "raw/7.md"
    original = root_page.read_bytes()
    add_page(wiki, "外部", "类型目录中的另一个页面")
    wiki.reindex()
    assert wiki.find("外部") is None
    assert {page.slug for page in wiki.resolve("外部")} == {"外部.md", "pages/concepts/外部.md"}
    assert wiki.find("外部.md").path == root_page
    assert root_page.read_bytes() == original
    client = TestClient(create_app())
    assert all(
        client.get(node["url"]).status_code == 200
        for node in build_graph(wiki, TODAY)["nodes"]
    )


def test_code_examples_do_not_create_edges_or_diagnostics(wiki):
    add_page(wiki, "target", "Target")
    source = add_page(
        wiki, "source", "Source",
        "[[Target|有效链接]]\n"
        "`[[inline-missing]]` and ``a ` [[long-inline-missing]]``\n"
        "```python\n[[fenced-missing]]\n```\n"
        "~~~~markdown\n[[tilde-missing]]\n~~~~\n"
        "`[[target]]`\n",
    )
    assert source.links() == {"Target"}
    assert [page.slug for page in page_connections(wiki, source)["outgoing"]] == ["target"]
    assert page_connections(wiki, source)["missing"] == []
    assert lint(wiki).broken_links == {}


def test_reindex_rebuilds_add_remove_delete_and_isolates_without_rewriting(wiki):
    assert build_graph(wiki, TODAY) == {"nodes": [], "edges": []}
    first = add_page(wiki, "first", "第一篇", "## 个人笔记\n\n保留原文。")
    first.meta.update({"mastery": "想回炉", "last_reviewed": "2026-11-01", "interval_days": 12})
    wiki.save_page(first)
    second = add_page(wiki, "second", "第二篇")
    before = first.path.read_bytes()
    metadata = dict(first.meta)
    wiki.reindex()
    assert build_graph(wiki, TODAY)["edges"] == []
    assert all(node["degree"] == 0 for node in build_graph(wiki, TODAY)["nodes"])
    assert first.path.read_bytes() == before
    assert wiki.find("first").meta == metadata

    # 外部编辑仅操作 Markdown；重建不可覆盖个人段落或复习状态。
    first.path.write_bytes(before + "\n[[第二篇]]\n".encode())
    wiki.reindex()
    assert build_graph(wiki, TODAY)["edges"] == [{"source": "first", "target": "second"}]
    backlinks = page_connections(wiki, wiki.find("second"))["backlinks"]
    assert [page.slug for page in backlinks] == ["first"]
    assert wiki.find("first").meta == metadata
    first.path.write_bytes(before)
    wiki.reindex()
    assert build_graph(wiki, TODAY)["edges"] == []
    assert page_connections(wiki, wiki.find("second"))["backlinks"] == []
    second.path.unlink()
    wiki.reindex()
    assert set(wiki.pages) == {"first"}
    assert "[[second]]" not in wiki.index_path.read_text(encoding="utf-8")
    assert first.path.read_bytes() == before
    first.path.unlink()
    wiki.reindex()
    assert build_graph(wiki, TODAY) == {"nodes": [], "edges": []}


def test_machine_links_preserve_chinese_aliases_paths_and_ambiguity(wiki):
    add_page(wiki, "first", "重复标题")
    add_page(wiki, "second", "重复标题")
    concept = add_page(wiki, "attention", "注意力机制", aliases=["注意力"])
    page = add_page(wiki, "material-1", "机器整理", page_type="material")
    text = "[[注意力#定义|显示文字]] [[重复标题]] [[../concepts/attention.md]] `[[代码]]`"
    _append_machine(page, 1, text)
    assert text in page.body
    update = PageUpdate(slug=page.slug, action="add_links", links=["注意力|别名", "重复标题"])
    assert _apply_update(wiki, page, update, 1) is None
    assert "[[注意力|别名]]" in page.body and "[[重复标题]]" in page.body
    links = page_connections(wiki, page)
    assert links["outgoing"] == [concept]
    assert links["ambiguous"] == ["重复标题"]
    assert links["missing"] == []


def test_ingest_never_merges_an_ambiguous_title_or_stem(wiki):
    first = add_page(wiki, "first", "共同标题", "第一份个人内容")
    second = add_page(wiki, "second", "共同标题", "第二份个人内容")
    originals = {page.path: page.path.read_bytes() for page in (first, second)}
    plan = WikiPlan(creates=[PageCreate(
        slug="third", title="共同标题", type="concept", summary="独立概念", body="新内容",
    )])
    row = {"id": 1, "title": "博客", "content": "正文", "summary": "", "url": ""}
    profile = load_profile(FIXTURES / "profile.md")
    result = apply_plan(wiki, plan, row, profile, TODAY)
    assert "third" in result.created
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert wiki.find("共同标题") is None

    duplicate = wiki.root / "pages" / "entities" / "first.md"
    duplicate.write_text("# 外部同名文件\n\n不可覆盖", encoding="utf-8")
    original = first.path.read_bytes()
    plan = WikiPlan(creates=[PageCreate(
        slug="first", title="共同标题", type="concept", summary="s", body="不得合并",
    )])
    result = apply_plan(wiki, plan, row, profile, TODAY)
    assert any("歧义" in reason for reason in result.rejected)
    assert first.path.read_bytes() == original
    assert "不可覆盖" in duplicate.read_text(encoding="utf-8")
