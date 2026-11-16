"""Stable route selection and CLI traversal over the same Markdown vault."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config
from secondbrain.cli import app as cli_app
from secondbrain.web.app import create_app
from secondbrain.wiki.store import Page, new_meta, open_wiki


@pytest.fixture
def network(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    today = date(2026, 11, 16)
    for slug, title, body in [
        ("a", "重复标题", "材料 A。[[b]]"),
        ("b", "共享概念", "概念 B。[[c]]"),
        ("c", "重复标题", "材料 C。"),
    ]:
        wiki.save_page(Page(slug, new_meta(title, "concept", today), body))
    wiki.rebuild_index()
    return wiki


def test_ambiguous_page_route_requires_selection_and_exact_id_opens(network):
    with TestClient(create_app()) as client:
        response = client.get("/wiki/page/重复标题")
        assert response.status_code == 409
        assert 'href="/wiki/page/a"' in response.text
        assert 'href="/wiki/page/c"' in response.text
        assert "材料 A。" in client.get("/wiki/page/a").text
        assert "材料 C。" in client.get("/wiki/page/c").text


def test_external_root_page_link_reindex_updates_backlinks_and_removal(network):
    with TestClient(create_app()) as client:
        external = network.root / "外部笔记.md"
        external.write_text("# 外部笔记\n\n[[a|材料入口]]", encoding="utf-8")
        assert client.post("/wiki/reindex").status_code == 200
        graph = client.get("/wiki/graph.json").json()
        assert any(node["id"] == "外部笔记" for node in graph["nodes"])
        page = client.get("/wiki/page/a").text
        assert 'href="/wiki/page/外部笔记"' in page
        external.write_text("# 外部笔记\n\n已经移除关联。", encoding="utf-8")
        client.post("/wiki/reindex")
        assert 'href="/wiki/page/外部笔记"' not in client.get("/wiki/page/a").text
        assert external.read_text(encoding="utf-8").endswith("已经移除关联。")


def test_cli_local_expansion_and_ambiguous_lookup(network):
    runner = CliRunner()
    ambiguous = runner.invoke(cli_app, ["wiki", "show", "重复标题"])
    assert ambiguous.exit_code != 0 and "a、c" in ambiguous.output
    one = runner.invoke(cli_app, ["wiki", "graph", "--focus", "a"])
    assert one.exit_code == 0, one.output
    assert "/wiki/page/a" in one.output and "/wiki/page/b" in one.output
    assert "/wiki/page/c" not in one.output
    expanded = runner.invoke(cli_app, ["wiki", "graph", "--focus", "a", "--depth", "2"])
    assert expanded.exit_code == 0, expanded.output
    assert "/wiki/page/c" in expanded.output
    assert "[[a]] — [[c]]" not in expanded.output


def test_home_supports_materials_and_network_without_digest(network):
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "材料库为空" in response.text
        assert 'href="/add"' in response.text and 'href="/wiki/graph"' in response.text
        assert "开始今日过滤" not in response.text
        assert "还没有 digest" in client.get("/recommendations").text


def test_generated_notes_migrate_without_colliding_with_material_filename(tmp_path, monkeypatch):
    from secondbrain import ops

    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    result = ops.add_manual([], lambda _: None, text="材料原文", title="材料")
    item_id = result["results"][0]["item_id"]
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    page = Page(
        f"material-{item_id}", new_meta("材料", "material", date(2026, 11, 16)),
        f"# 材料\n\n用户正文不应变化。\n\n[个人笔记](../../notes/material-{item_id}.md)\n"
        f"\n`notes/material-{item_id}.md` 是应保留的代码示例。\n",
    )
    path = wiki.save_page(page)
    original_page = path.read_text(encoding="utf-8")
    old = wiki.root / "notes" / f"material-{item_id}.md"
    old.parent.mkdir()
    note_bytes = "# 我的笔记\n\n不可覆盖。\n".encode()
    old.write_bytes(note_bytes)
    assert ops.material_notes(item_id) == note_bytes.decode()
    new = old.with_name(f"material-{item_id}-notes.md")
    assert not old.exists() and new.read_bytes() == note_bytes
    assert new.stem != path.stem
    assert path.read_text(encoding="utf-8") == original_page.replace(
        f"notes/material-{item_id}.md", f"notes/material-{item_id}-notes.md", 1
    )
    assert ops.material_notes(item_id) == note_bytes.decode()
