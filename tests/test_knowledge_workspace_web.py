"""知识工作区消费者回归：临时库、fake 模型，不访问真实个人数据。"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from secondbrain import config, db, ops
from secondbrain.models import SourceConfig
from secondbrain.web.app import create_app
from secondbrain.web.tasks import TaskRegistry
from secondbrain.wiki.management import manage_pages
from secondbrain.wiki.rewrite import get_session, history, list_sessions
from secondbrain.wiki.store import Page, WikiError, open_wiki

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    with closing(db.connect()) as conn:
        db.upsert_sources(conn, [SourceConfig(id="workspace", type="rss", url="https://example.com")])
        for index in range(2):
            title = f"缓存调度原文 {index}"
            content = "KV cache tracks attention state. " * 80
            db.insert_item(conn, db.NewItem(
                source_id="workspace", url=f"https://example.com/posts/{index}",
                url_canonical=f"https://example.com/posts/{index}",
                content_hash=hashlib.sha256(f"{index}{content}".encode()).hexdigest(),
                title=title, author=None, summary=None, content=content,
                extract_method="html", published_at=NOW,
            ), NOW)
        ids = [row[0] for row in conn.execute("SELECT id FROM items ORDER BY id")]
        for item_id in ids:
            db.add_to_reading_list(conn, item_id)
        conn.commit()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    wiki.save_page(Page("cache", {
        "type": "concept", "title": "缓存机制", "sources": [ids[0]],
        "knowledge_status": "recorded", "summary": "推理缓存与调度",
    }, "## 解释\n\n缓存保存重复计算的状态。\n\n## 个人笔记\n\n我的手写笔记。\n\n"
       "## 来源\n\n原始资料。\n"))
    wiki.save_page(Page("scheduler", {
        "type": "concept", "title": "调度器", "sources": [ids[0]],
        "knowledge_status": "recorded",
    }, "## 解释\n\n调度器使用 [[cache]]。\n"))
    wiki.save_page(Page(f"material-{ids[0]}", {
        "type": "material", "title": "缓存调度原文 0", "sources": [ids[0]],
        "material_id": ids[0], "generated_by": "wiki_ingest", "knowledge_status": "recorded",
    }, "## 材料摘要\n\n缓存调度的原文内容。\n"))
    wiki.rebuild_index()
    return {"ids": ids, "wiki": wiki, "root": tmp_path / "data"}


@pytest.fixture
def client(workspace):
    return TestClient(create_app())


def _finish(task):
    assert task is not None and task.thread is not None
    task.thread.join(timeout=10)
    assert not task.running
    return task


def _rewrite(client, context="从推理服务项目重新理解缓存机制"):
    response = client.post("/wiki/rewrite/start", data={"slug": "cache", "context": context},
                           follow_redirects=False)
    assert response.status_code == 303
    task = _finish(client.app.state.tasks.records()[0])
    assert task.status == "done", task.error or task.result
    assert "messages" not in task.result and "draft" not in task.result
    return get_session(task.result["session_id"])


def test_knowledge_categories_are_read_only_and_manually_overridable(client, workspace):
    wiki = workspace["wiki"]
    material = wiki.material_page(workspace["ids"][0])
    before = material.path.read_bytes()
    response = client.get("/wiki?kind=article")
    assert response.status_code == 200
    assert "缓存调度原文 0" in response.text
    assert 'value="video"' not in response.text
    assert material.path.read_bytes() == before
    assert "调度器" not in client.get("/wiki?q=推理缓存").text
    body = material.body
    path = material.path
    response = client.post("/wiki/category", data={"slug": material.slug, "kind": "video"})
    assert response.status_code == 200
    wiki.reload_pages()
    updated = wiki.pages[material.slug]
    assert updated.path == path and updated.body == body
    assert updated.meta["knowledge_kind"] == "video"
    assert "缓存调度原文 0" in client.get("/wiki?kind=video").text
    response = client.post("/wiki/category", data={"slug": material.slug, "kind": "invalid"})
    assert response.status_code == 400


def test_knowledge_recycle_confirms_backlinks_and_can_restore(client, workspace):
    wiki = workspace["wiki"]
    path = wiki.pages["cache"].path
    body = wiki.pages["cache"].body
    response = client.post("/wiki/manage", data={"slugs": "cache", "action": "trash"})
    assert response.status_code == 200 and "调度器" in response.text
    assert "反向链接" in response.text
    wiki.reload_pages()
    assert not wiki.pages["cache"].meta.get("trashed")
    response = client.post("/wiki/manage", data={
        "slugs": "cache", "action": "trash", "confirm": "yes",
    })
    assert response.status_code == 200 and "已回收" in response.text
    nodes = client.get("/wiki/graph.json?drafts=true").json()["nodes"]
    assert all(node["id"] != "cache" for node in nodes)
    assert "目标已回收" in client.get("/wiki/page/scheduler").text
    assert "恢复知识页" in client.get("/wiki/page/cache").text
    client.post("/wiki/reindex")
    wiki.reload_pages()
    assert wiki.pages["cache"].meta["trashed"] is True
    assert wiki.pages["cache"].body == body and wiki.pages["cache"].path == path
    response = client.post("/wiki/manage", data={"slugs": "cache", "action": "restore"})
    assert response.status_code == 200
    assert any(node["id"] == "cache" for node in client.get("/wiki/graph.json").json()["nodes"])


def test_batch_recycle_and_restore_keeps_personal_content(client, workspace):
    selected = ["cache", "scheduler"]
    before = {slug: workspace["wiki"].pages[slug].body for slug in selected}
    response = client.post("/wiki/manage", data={
        "slugs": selected, "action": "trash", "confirm": "yes",
    })
    assert response.status_code == 200
    response = client.post("/wiki/manage", data={"slugs": selected, "action": "restore"})
    assert response.status_code == 200
    workspace["wiki"].reload_pages()
    assert {slug: workspace["wiki"].pages[slug].body for slug in selected} == before


def test_remove_reading_batch_preserves_materials(client, workspace):
    ids = workspace["ids"]
    assert 'action="/reading/remove"' in client.get("/later").text
    response = client.post("/reading/remove", data={"item_ids": ids})
    assert response.status_code == 200
    with closing(db.connect()) as conn:
        assert db.reading_list_items(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == len(ids)
    assert client.post("/reading/remove", data={}).status_code == 400


def test_removed_review_and_internal_numbers_do_not_appear(client, workspace):
    for url in ("/wiki", "/wiki/page/cache", "/wiki/graph", "/recommendations", "/profile"):
        response = client.get(url)
        assert response.status_code == 200
        assert "到期复习" not in response.text and "<th>掌握度</th>" not in response.text
        if url != "/profile":  # 画像原文允许保留用户历史表格，不再解释或参与行为。
            assert "掌握度" not in response.text
        assert "/wiki/review" not in response.text
    assert client.post("/wiki/review", data={"slug": "cache", "grade": "good"}).status_code == 404
    page = client.get("/wiki/page/cache").text
    assert "材料 #" not in page and "缓存调度原文 0" in page
    graph = client.get("/wiki/graph.json").json()
    assert all("mastery" not in node and "due" not in node for node in graph["nodes"])
    material = next(node for node in graph["nodes"] if node["id"].startswith("material-"))
    assert material["type"] == "article"
    assert "缓存调度原文 0" in material["source_titles"].values()


def test_delete_finished_tasks_is_real_and_protects_waiters(client, workspace):
    registry = client.app.state.tasks
    completed = _finish(registry.start("test-complete", "已结束测试", lambda task: {}))
    waiting = _finish(registry.start(
        "test-waiting", "待选论文", lambda task: {"status": "ambiguous"}
    ))
    response = client.post("/task-history/delete", data={"task_ids": completed.id})
    assert response.status_code == 200 and "永久删除" in response.text
    assert registry.get(completed.id) is completed
    assert client.post("/task-history/delete", data={
        "task_ids": [completed.id, waiting.id], "confirm": "yes",
    }).status_code == 409
    assert registry.get(completed.id) is completed
    response = client.post(
        "/task-history/delete", data={"task_ids": completed.id, "confirm": "yes"}
    )
    assert response.status_code == 200
    assert registry.get(completed.id) is None and registry.get(waiting.id) is waiting
    restored = TaskRegistry(workspace["root"] / "web_tasks.db")
    assert restored.get(completed.id) is None and restored.get(waiting.id) is not None


def test_delete_all_tasks_keeps_running_and_waiting(client):
    registry = client.app.state.tasks
    release = threading.Event()
    running = registry.start("test-running", "运行保护", lambda task: (release.wait(10), {})[1])
    waiting = _finish(registry.start(
        "test-waiting", "待选保护", lambda task: {"status": "ambiguous"}
    ))
    finished = _finish(registry.start("test-done", "可删除", lambda task: {}))
    try:
        assert client.post("/task-history/delete", data={
            "task_ids": running.id, "confirm": "yes",
        }).status_code == 409
        response = client.post("/task-history/delete", data={"all": "yes", "confirm": "yes"})
        assert response.status_code == 200
        assert registry.get(finished.id) is None
        assert registry.get(running.id) is running and registry.get(waiting.id) is waiting
    finally:
        release.set()
        _finish(running)


def test_deleted_native_paper_task_is_not_resurrected_from_business_history(client, workspace):
    result = {
        "search_id": "search-one", "status": "imported", "material_id": workspace["ids"][0],
        "selected": {"id": "candidate-one"},
    }
    with closing(db.connect()) as conn, conn:
        conn.execute("INSERT INTO paper_searches VALUES (?, ?, ?, ?, ?)", (
            "search-one", "缓存论文", "[]", "[]", NOW.isoformat(),
        ))
        conn.execute("INSERT INTO paper_import_attempts"
                     " (search_id, candidate_id, result_json, created_at)"
                     " VALUES (?, ?, ?, ?)", (
                         "search-one", "candidate-one", json.dumps(result), NOW.isoformat(),
                     ))
    task = _finish(client.app.state.tasks.start("paper-native", "论文导入", lambda task: result))
    response = client.post(
        "/task-history/delete", data={"task_ids": task.id, "confirm": "yes"}
    )
    assert response.status_code == 200
    restarted = TaskRegistry(workspace["root"] / "web_tasks.db")
    restarted.recover_papers(db.db_path())
    assert not restarted.records(include_dismissed=True)
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_import_attempts").fetchone()[0] == 1
        assert db.item_detail(conn, workspace["ids"][0]) is not None


def test_zhihu_403_remedy_retains_original_link(client):
    url = "https://zhuanlan.zhihu.com/p/12345?from=feed&x=1"
    task = _finish(client.app.state.tasks.start("manual_add", "导入失败", lambda task: {
        "results": [{"url": url, "status": "失败", "message": "HTTP 403 Forbidden"}],
    }))
    response = client.get(f"/task-history/{task.id}")
    assert "403 Forbidden" in response.text and "粘贴正文 / 上传 PDF 补救" in response.text
    response = client.get("/add", params={"url": url, "remedy": "1"})
    assert "https://zhuanlan.zhihu.com/p/12345?from=feed&amp;x=1" in response.text
    assert "不会再次请求原站" in response.text and "不保证" in response.text
    assert 'name="text"' in response.text and 'name="pdf"' in response.text


def test_rewrite_web_flow_recovers_after_navigation_and_task_deletion(client, workspace):
    page = workspace["wiki"].pages["cache"]
    original = page.path.read_bytes()
    session = _rewrite(client)
    assert page.path.read_bytes() == original
    response = client.get("/wiki/page/cache")
    assert session["id"] in response.text and "草稿待采用" in response.text
    assert client.get("/later").status_code == 200
    restarted_client = TestClient(create_app())
    assert session["id"] in restarted_client.get("/wiki/page/cache").text
    task_id = client.app.state.tasks.records()[0].id
    client.post("/task-history/delete", data={"task_ids": task_id, "confirm": "yes"})
    assert session["id"] in client.get("/wiki/page/cache").text
    response = client.post("/wiki/rewrite/continue", data={
        "session_id": session["id"], "message": "增加适用条件与失败模式",
    }, follow_redirects=False)
    assert response.status_code == 303
    _finish(client.app.state.tasks.records()[0])
    assert "增加适用条件" in get_session(session["id"])["draft"]
    response = client.post("/wiki/rewrite/apply", data={"session_id": session["id"]})
    assert response.status_code == 200
    assert "我的手写笔记" in page.path.read_text() and "原始资料" in page.path.read_text()
    revisions = history("cache")
    assert len(revisions) >= 2
    oldest = min(revisions, key=lambda revision: revision["created_at"])
    response = client.post("/wiki/rewrite/restore", data={
        "slug": "cache", "revision_id": oldest["id"],
    })
    assert response.status_code == 200 and page.path.read_bytes() == original


def test_rewrite_rejects_stale_draft_and_can_delete_session_and_history(client, workspace):
    session = _rewrite(client)
    path = workspace["wiki"].pages["cache"].path
    with path.open("a") as stream:
        stream.write("\n人工追加内容。\n")
    assert client.post("/wiki/rewrite/apply", data={"session_id": session["id"]}).status_code == 409
    assert "人工追加内容" in path.read_text()
    second = _rewrite(client, "保持追加内容，重新组织")
    assert client.post("/wiki/rewrite/apply", data={"session_id": second["id"]}).status_code == 200
    current = path.read_bytes()
    assert history("cache")
    response = client.post("/wiki/rewrite/clear-history", data={"slug": "cache"})
    assert response.status_code == 200 and "永久删除" in response.text
    assert history("cache")
    client.post("/wiki/rewrite/clear-history", data={"slug": "cache", "confirm": "yes"})
    assert history("cache") == [] and path.read_bytes() == current
    response = client.post("/wiki/rewrite/delete", data={"session_id": second["id"]})
    assert response.status_code == 200 and get_session(second["id"])
    client.post("/wiki/rewrite/delete", data={"session_id": second["id"], "confirm": "yes"})
    assert all(value["id"] != second["id"] for value in list_sessions("cache"))
    assert path.read_bytes() == current


def test_source_deletion_and_notes_clear_preserve_materials(client, workspace):
    config_dir = config.config_dir()
    (config_dir / "sources.yaml").write_text(
        "sources:\n  - id: workspace\n    type: rss\n    url: https://example.com\n",
        encoding="utf-8",
    )
    response = client.get("/sources")
    assert "删除信源" in response.text and "取消关注" not in response.text
    response = client.post("/sources/workspace/delete")
    assert response.status_code == 200 and "确认删除" in response.text
    assert any(source.id == "workspace" for source in config.load_sources())
    client.post("/sources/workspace/delete", data={"confirm": "yes"})
    assert not config.load_sources()
    item_id = workspace["ids"][0]
    ops.material_notes(item_id, "暂存笔记")
    assert client.post(f"/item/{item_id}/notes", data={"notes": ""}).status_code == 200
    assert ops.material_notes(item_id) == ""
    with closing(db.connect()) as conn:
        assert db.item_detail(conn, item_id) is not None
    before = config.profile_path().read_bytes() if config.profile_path().exists() else None
    assert "已拒绝" in client.post("/profile/reject").text
    after = config.profile_path().read_bytes() if config.profile_path().exists() else None
    assert before == after


def test_management_rejects_unknown_pages_without_partial_changes(workspace):
    wiki = workspace["wiki"]
    with pytest.raises(WikiError, match="知识页不存在"):
        manage_pages(wiki, ["cache", "not-a-page"], "trash")
    wiki.reload_pages()
    assert not wiki.pages["cache"].meta.get("trashed")


def test_batch_management_reports_partial_success_without_overwriting(workspace, monkeypatch):
    wiki = workspace["wiki"]
    save = wiki.save_metadata

    def conflict(page, *, expected_hash=None):
        if page.slug == "scheduler":
            raise WikiError("wiki 页面已变化，请重新预览")
        save(page, expected_hash=expected_hash)

    monkeypatch.setattr(wiki, "save_metadata", conflict)
    with pytest.raises(WikiError) as failure:
        manage_pages(wiki, ["cache", "scheduler"], "trash")
    assert "已处理：缓存机制" in str(failure.value)
    assert "未处理：调度器" in str(failure.value)
    wiki.reload_pages()
    assert wiki.pages["cache"].meta["trashed"] is True
    assert not wiki.pages["scheduler"].meta.get("trashed")


def test_legacy_machine_notice_is_not_rendered_or_written_on_get(client, workspace):
    page = workspace["wiki"].pages["cache"]
    marker = "a" * 20
    with page.path.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n## 机器整理 · 材料 1\n\n"
            f"<!-- material-1:{marker} -->\n\n"
            "> 机器整理，未经用户核对；材料陈述与机器推断分别标注。\n\n"
            "旧机器整理正文。\n"
        )
    before = page.path.read_bytes()
    response = client.get("/wiki/page/cache")
    assert response.status_code == 200 and "旧机器整理正文" in response.text
    assert "机器整理 · 材料 1" not in response.text
    assert "机器整理，未经用户核对" not in response.text
    assert page.path.read_bytes() == before


def test_running_rewrite_protects_session_and_versions(client):
    session = _rewrite(client)
    release = threading.Event()
    kind = "rewrite-" + hashlib.sha256(b"cache").hexdigest()[:20]
    running = client.app.state.tasks.start(
        kind, "重整运行中", lambda task: (release.wait(10), {})[1],
        initial_result={"slug": "cache", "session_id": session["id"]},
    )
    try:
        assert client.post("/wiki/rewrite/delete", data={
            "session_id": session["id"], "confirm": "yes",
        }).status_code == 409
        assert client.post("/wiki/rewrite/clear-history", data={
            "slug": "cache", "confirm": "yes",
        }).status_code == 409
        assert client.post("/wiki/rewrite/apply", data={
            "session_id": session["id"],
        }).status_code == 409
        assert get_session(session["id"])["draft"]
    finally:
        release.set()
        _finish(running)


def test_graph_keeps_only_explicit_page_links(client):
    graph = client.get("/wiki/graph.json").json()
    assert graph["edges"] == [{"source": "cache", "target": "scheduler"}]
