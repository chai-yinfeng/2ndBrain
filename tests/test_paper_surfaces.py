"""论文名 CLI/Web 使用真实共享服务；仅替换元数据及正文网络传输。"""

import html
import json
import re
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, make_pdf
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops, papers
from secondbrain.cli import app as cli_app
from secondbrain.net import HttpClient
from secondbrain.web.app import create_app

TITLE = "Surface Paper Identity"
ABSTRACT = "An abstract about reproducible material identity and knowledge networks. " * 30


@pytest.fixture
def paper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    mode = {"empty": False, "ambiguous": False, "full": False, "requests": []}

    def handler(request):
        mode["requests"].append(str(request.url))
        records = [] if mode["empty"] else [
            {
                "paperId": "surface-one", "title": TITLE,
                "authors": [{"name": "Ada Example"}], "year": 2024,
                "venue": "SurfaceConf", "abstract": ABSTRACT,
                "externalIds": {"DOI": "10.1234/surface-one"},
                "url": "https://doi.org/10.1234/surface-one",
                "openAccessPdf": {"url": "https://papers.example.org/surface.pdf"}
                if mode["full"] else None,
            }
        ]
        if mode["ambiguous"] and records:
            records.append({**records[0], "paperId": "surface-two", "year": 2025,
                            "externalIds": {"DOI": "10.1234/surface-two"}})
        host, path = request.url.host, request.url.path
        if host == "api.semanticscholar.org":
            if "/search" in path:
                return httpx.Response(200, json={"total": len(records), "data": records})
            return httpx.Response(200, json=records[0]) if records else httpx.Response(404)
        if host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if host in {"export.arxiv.org", "export.arxiv.org:443"}:
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
        if host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        if mode["full"] and host == "papers.example.org":
            return httpx.Response(
                200, content=make_pdf(["Material identity and knowledge networks. " * 80]),
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(404)

    class FixtureClient(HttpClient):
        def __init__(self, *args, **kwargs):
            super().__init__(fast_settings(), transport=httpx.MockTransport(handler))

    client = FixtureClient
    monkeypatch.setattr(papers, "HttpClient", FixtureClient)
    monkeypatch.setattr("secondbrain.web.app._http_client", client)
    return mode


def rows():
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM items").fetchall()
    finally:
        conn.close()


def settle(app):
    for task in list(app.state.tasks._tasks.values()):
        if task.thread is not None:
            task.thread.join(timeout=10)
        assert not task.running


def confirm_material_wiki(client, app, item_id):
    url = f"/item/{item_id}/wiki"
    kind = f"wiki-{item_id}"
    preview = client.post(url)
    assert preview.status_code == 200
    token = re.search(r'name="token" value="([^"]+)"', preview.text)
    assert token is not None
    assert app.state.tasks.get(kind) is None
    response = client.post(url, data={"confirm": "yes", "token": token[1]}, follow_redirects=False)
    assert response.status_code == 303
    settle(app)
    return app.state.tasks.get(kind)


def test_cli_ambiguous_choose_saved_identity_and_repeat(paper_env):
    paper_env["ambiguous"] = True
    runner = CliRunner()
    result = runner.invoke(cli_app, ["paper", "add", TITLE])
    assert result.exit_code == 0, result.output
    assert "ambiguous" in result.output
    assert "Ada Example" in result.output and "2024" in result.output
    assert "SurfaceConf" in result.output and "10.1234/surface-one" in result.output
    assert rows() == []
    command = re.search(r"sb paper choose (\S+) (\S+)", result.output)
    assert command, result.output
    search_id, candidate_id = command.groups()
    paper_env["empty"] = True  # selection must not rerun the now-empty search
    chosen = runner.invoke(cli_app, ["paper", "choose", search_id, candidate_id])
    assert chosen.exit_code == 0, chosen.output
    assert "abstract_only" in chosen.output and "/item/" in chosen.output
    assert len(rows()) == 1 and rows()[0]["read_at"] is None
    repeated = runner.invoke(cli_app, ["paper", "choose", search_id, candidate_id])
    assert repeated.exit_code == 0, repeated.output
    assert len(rows()) == 1
    invalid = runner.invoke(cli_app, ["paper", "choose", search_id, "not-saved"])
    assert invalid.exit_code != 0
    assert len(rows()) == 1


def test_cli_not_found_nonzero(paper_env):
    paper_env["empty"] = True
    result = CliRunner().invoke(cli_app, ["paper", "add", "Absent Paper"])
    assert result.exit_code != 0 and "not_found" in result.output
    assert rows() == []


def test_web_background_choice_refresh_and_untrusted_url(paper_env):
    paper_env["ambiguous"] = True
    app = create_app()
    with TestClient(app) as client:
        assert 'action="/papers/add"' in client.get("/add").text
        response = client.post("/papers/add", data={"query": TITLE}, follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        settle(app)
        page = client.get(location)
        assert 'data-paper-status="ambiguous"' in page.text
        assert client.get(location).text == page.text
        assert rows() == []
        search_id = re.search(r'name="search_id" value="([^"]+)"', page.text)[1]
        candidate_id = re.search(r'name="candidate_id" value="([^"]+)"', page.text)[1]
        paper_env["empty"] = True
        chosen = client.post("/papers/choose", data={
            "search_id": search_id, "candidate_id": candidate_id,
            "url": "https://attacker.invalid/replaced", "kind": "run",
        }, follow_redirects=False)
        settle(app)
        result = client.get(chosen.headers["location"])
        assert 'data-paper-status="abstract_only"' in result.text
        assert f'/item/{rows()[0]["id"]}' in result.text
        assert rows()[0]["read_at"] is None
        assert not any("attacker.invalid" in url for url in paper_env["requests"])
        assert client.post("/tasks/paper-arbitrary").status_code == 404


def test_web_not_found_and_invalid_saved_selection(paper_env):
    paper_env["empty"] = True
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/papers/add", data={"query": "Absent Paper"}, follow_redirects=False
        )
        settle(app)
        assert 'data-paper-status="not_found"' in client.get(response.headers["location"]).text
        assert 'data-task-status="not_found"' in client.get(response.headers["location"]).text
        invalid = client.post("/papers/choose", data={
            "search_id": "missing", "candidate_id": "missing"
        }, follow_redirects=False)
        settle(app)
        assert 'data-task-status="failed"' in client.get(invalid.headers["location"]).text
        assert rows() == []


def test_cli_and_web_full_material_wiki_without_read_feedback(paper_env):
    paper_env["full"] = True
    result = CliRunner().invoke(cli_app, ["paper", "add", TITLE, "--ingest"])
    assert result.exit_code == 0, result.output
    assert "imported" in result.output and "/wiki/page/material-" in result.output
    first, = rows()
    assert first["reading_state"] == "unread" and first["read_at"] is None
    assert first["content_completeness"] == "full"
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/papers/add", data={"query": TITLE, "state": "read"}, follow_redirects=False
        )
        settle(app)
        page = client.get(response.headers["location"])
        assert 'data-paper-status="imported"' in page.text
        imported, = rows()
        assert imported["id"] == first["id"] and imported["content_completeness"] == "full"
        assert imported["reading_state"] == "read" and imported["read_at"] is not None
        item_id = imported["id"]
        wiki_page = f"/wiki/page/material-{item_id}"
        graph = f"/wiki/graph?focus=material-{item_id}"
        assert f"/item/{item_id}" in page.text
        assert wiki_page in page.text  # CLI 创建的草稿仍然存在，导入已读不会发布它
        assert graph not in page.text
        assert client.get(f"/item/{item_id}").status_code == 200
        assert client.get(wiki_page).status_code == 200

        wiki_task = confirm_material_wiki(client, app, item_id)
        assert wiki_task.status == "done", wiki_task.lines
        assert wiki_task.result["cached"] == [item_id]
        assert graph in client.get(response.headers["location"]).text
    assert len(rows()) == 1
    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0
    finally:
        conn.close()


def test_web_preserves_material_and_reports_failed_wiki_processing(paper_env, monkeypatch):
    from secondbrain.llm.base import LLMError
    from secondbrain.llm.fake import FakeBackend
    from secondbrain.llm.schemas import WikiPlan

    paper_env["full"] = True
    complete = FakeBackend.complete_json

    def fail_wiki(self, messages, schema, tier):
        if schema is WikiPlan:
            raise LLMError("知识整理服务暂不可用")
        return complete(self, messages, schema, tier)

    monkeypatch.setattr(FakeBackend, "complete_json", fail_wiki)
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/papers/add", data={"query": TITLE, "state": "read"})
        settle(app)
        page = client.get(response.url)
        assert 'data-paper-status="imported"' in page.text
        assert 'data-task-status="done"' in page.text
        imported, = rows()
        assert imported["reading_state"] == "read" and imported["read_at"] is not None
        item_id = imported["id"]
        wiki_page = f"/wiki/page/material-{item_id}"
        assert client.get(wiki_page).status_code == 404
        with closing(db.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0

        wiki_task = confirm_material_wiki(client, app, item_id)
        assert wiki_task.status == "partial", wiki_task.lines
        assert wiki_task.result["failures"] == 1
        task_page = client.get(f"/task-history/{wiki_task.id}").text
        assert 'data-task-status="partial"' in task_page
        assert "知识整理服务暂不可用" in task_page
        saved, = rows()
        assert (saved["id"], saved["content"]) == (item_id, imported["content"])
        assert (saved["reading_state"], saved["read_at"]) == ("read", imported["read_at"])
        assert client.get(wiki_page).status_code == 404
        assert 'data-task-status="done"' in client.get(response.url).text


def test_web_ambiguous_selection_retains_read_state_before_explicit_wiki(paper_env):
    paper_env["full"] = True
    paper_env["ambiguous"] = True
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/papers/add", data={"query": TITLE, "state": "read"})
        settle(app)
        response = client.get(response.url)
        assert 'name="state" value="read" checked' in response.text
        task = next(task for task in app.state.tasks._tasks.values() if task.result)
        result = task.result
        candidate_id = result["candidates"][0]["id"]
        assert rows() == []
        chosen = client.post("/papers/choose", data={
            "search_id": result["search_id"], "candidate_id": candidate_id, "state": "read",
        })
        settle(app)
        page = client.get(chosen.url)
        assert 'data-paper-status="imported"' in page.text
        imported, = rows()
        item_id = imported["id"]
        assert imported["reading_state"] == "read" and imported["read_at"] is not None
        assert f"/item/{item_id}" in page.text
        assert f'data-candidate-id="{candidate_id}"' in page.text
        wiki_page = f"/wiki/page/material-{item_id}"
        assert wiki_page not in page.text
        assert client.get(wiki_page).status_code == 404

        wiki_task = confirm_material_wiki(client, app, item_id)
        assert wiki_task.status == "done", wiki_task.lines
        assert wiki_page in client.get(chosen.url).text
        assert client.get(wiki_page).status_code == 200


def test_web_waiting_choice_survives_restart_with_learning_record(paper_env):
    paper_env.update(ambiguous=True, full=True)
    original = create_app()
    notes = "外部讨论结论：候选版本需要人工核对。\nA < B & C"
    discussion = "https://chatgpt.com/share/public-reference"
    with TestClient(original) as client:
        response = client.post("/papers/add", data={
            "query": TITLE, "state": "read",
            "learning_notes": notes, "discussion_url": discussion,
        }, follow_redirects=False)
        settle(original)
        location = response.headers["location"]
        task = next(t for t in original.state.tasks.records() if t.kind.startswith("paper-"))
        assert task.status == "waiting"
        assert rows() == []
        assert client.post(f"/task-history/{task.id}/dismiss").status_code == 409
    restarted = create_app()
    with TestClient(restarted) as client:
        page = client.get(location)
        assert 'data-task-status="waiting"' in page.text
        assert "等待选择 · 尚未导入" in page.text
        assert 'name="state" value="read" checked' in page.text
        assert html.escape(notes) in page.text and discussion in page.text
        assert 'action="/papers/choose"' not in client.get("/add").text
        history = client.get("/task-history").text
        assert f"/task-history/{task.id}" in history
        assert 'action="/papers/choose"' not in history
        restored = restarted.state.tasks.get(task.id).result
        paper_env["empty"] = True  # 已保存候选仍可选，不能重新检索。
        chosen = client.post("/papers/choose", data={
            "search_id": restored["search_id"],
            "candidate_id": restored["candidates"][1]["id"],
            "state": restored["state_requested"],
            "learning_notes": restored["learning_notes"],
            "discussion_url": restored["discussion_url"],
        }, follow_redirects=False)
        settle(restarted)
        result_page = client.get(chosen.headers["location"])
        assert restarted.state.tasks.get(task.id).status == "resolved"
        old_page = client.get(f"/task-history/{task.id}").text
        assert 'action="/papers/choose"' not in old_page
        assert "历史候选" in old_page
        assert 'data-paper-status="imported"' in result_page.text
        imported, = rows()
        assert imported["reading_state"] == "read"
        assert ops.material_notes(imported["id"]) == notes
        with closing(db.connect()) as conn:
            assert db.get_ai(conn, imported["id"], "reading_record")["discussion_url"] == discussion


def test_web_recovers_legacy_paper_attempt_without_changing_history(paper_env):
    paper_env["ambiguous"] = True
    result = CliRunner().invoke(cli_app, ["paper", "add", TITLE])
    assert result.exit_code == 0, result.output
    with closing(db.connect()) as conn:
        searches = [tuple(r) for r in conn.execute("SELECT * FROM paper_searches")]
        attempts = [tuple(r) for r in conn.execute("SELECT * FROM paper_import_attempts")]
        stored = json.loads(attempts[0][3])
    app = create_app()
    with TestClient(app) as client:
        assert "/task-history" in client.get("/add").text
        task, = app.state.tasks.records()
        assert task.status == "waiting"
        assert task.result["search_id"] == stored["search_id"]
        assert 'action="/papers/choose"' in client.get(f"/task-history/{task.id}").text
        assert "等待选择" in client.get("/task-history").text
        paper_env["empty"] = True
        chosen = client.post("/papers/choose", data={
            "search_id": stored["search_id"], "candidate_id": stored["candidates"][0]["id"],
            "state": "unread",
        }, follow_redirects=False)
        settle(app)
        assert 'data-paper-status="abstract_only"' in client.get(chosen.headers["location"]).text
    with closing(db.connect()) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM paper_searches")] == searches
        assert [tuple(r) for r in conn.execute(
            "SELECT * FROM paper_import_attempts WHERE id = ?", (attempts[0][0],)
        )] == attempts
    restarted = create_app()
    assert len(restarted.state.tasks.records()) == 2  # 恢复不重复生成旧记录。


def test_web_restart_marks_running_interrupted_without_retry(paper_env):
    script = """
import sys
import threading
from pathlib import Path
from secondbrain.web.tasks import TaskRegistry
ready = threading.Event()
gate = threading.Event()
registry = TaskRegistry(Path(sys.argv[1]))
def work(task):
    task.log("已保存进度，不自动重试")
    ready.set()
    gate.wait(60)
task = registry.start("manual_add", "被重启的导入", work,
                      initial_result={"progress": "已收到输入"})
assert ready.wait(5)
print(task.id, flush=True)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(config.data_dir() / "web_tasks.db")],
        capture_output=True, text=True, check=True, timeout=10,
    )
    task_id = proc.stdout.strip()
    app = create_app()
    with TestClient(app) as client:
        task = app.state.tasks.get(task_id)
        assert task.status == "interrupted" and task.thread is None
        assert task.result["progress"] == "已收到输入"
        page = client.get(f"/task-history/{task_id}")
        assert 'data-task-status="interrupted"' in page.text
        assert "未自动重试" in page.text and "已保存进度" in page.text
        assert "hx-trigger" not in page.text


@pytest.mark.parametrize("material_type", ["paper", "link", "file", "text"])
def test_material_state_and_external_learning_are_symmetric(paper_env, material_type):
    paper_env["full"] = True
    app = create_app()
    notes = "已经在外部学习，保留个人结论。"
    discussion = "https://example.org/discussion"
    with TestClient(app) as client:
        material_id = None
        for state in ("later", "read", "unread"):
            data = {"state": state, "learning_notes": notes, "discussion_url": discussion}
            if material_type == "paper":
                response = client.post(
                    "/papers/add", data={**data, "query": TITLE}, follow_redirects=False
                )
            elif material_type == "file":
                response = client.post("/add", data=data, files={
                    "pdf": ("material.pdf", make_pdf([ABSTRACT]), "application/pdf"),
                })
            else:
                data["text"] = ABSTRACT
                if material_type == "link":
                    data["urls"] = "https://example.org/material"
                response = client.post("/add", data=data)
            assert response.status_code in {200, 303}
            settle(app)
            imported, = rows()
            assert material_id in (None, imported["id"])
            material_id = imported["id"]
            with closing(db.connect()) as conn:
                queued = {r["id"] for r in db.reading_list_items(conn)}
                assert (material_id in queued) == (state == "later")
                assert imported["reading_state"] == ("read" if state == "read" else "unread")
                if state == "read":
                    record = db.get_ai(conn, material_id, "reading_record")
                    assert record["discussion_url"] == discussion
                    assert ops.material_notes(material_id) == notes
        assert len(app.state.tasks.records()) == 3


def test_manual_batch_failure_and_closed_feedback_keep_own_results(paper_env):
    app = create_app()
    with TestClient(app) as client:
        client.post("/add", data={
            "urls": "https://example.org/first\nhttps://example.org/second",
            "state": "later", "reference_only": "1",
        })
        settle(app)
        first = app.state.tasks.get("manual_add")
        assert len(first.result["results"]) == 2
        client.post("/add", data={
            "urls": "https://example.org/first\nhttps://example.org/unavailable", "state": "later",
        })
        settle(app)
        second = app.state.tasks.get("manual_add")
        assert second.status == "partial"
        assert sum(r["status"] == "失败" for r in second.result["results"]) == 1
        page = client.get(f"/task-history/{second.id}").text
        assert 'data-task-status="partial"' in page and "unavailable" in page
        assert "<summary>运行日志" in page and "<details><summary>运行日志" in page
        invalid = client.post("/add", data={"state": "later"})
        assert "unavailable" not in invalid.text
        client.post(f"/task-history/{second.id}/dismiss")
        assert second.dismissed
        assert f'data-task-record="{second.id}"' not in client.get("/task-history").text
        assert f'data-task-record="{second.id}"' in client.get("/task-history?closed=1").text
        assert "unavailable" in client.get(f"/task-history/{second.id}").text
        assert "unavailable" not in client.get(f"/task-history/{first.id}").text
        assert "unavailable" not in client.get("/add").text


def test_web_recovers_search_with_no_import_attempt(paper_env):
    candidate = papers._candidate(
        provider="semantic_scholar", title=TITLE, authors=["Ada Example"], year=2024,
        doi="10.1234/pending", url="https://doi.org/10.1234/pending",
        abstract=ABSTRACT,
    )
    with closing(db.connect()) as conn:
        conn.execute("INSERT INTO paper_searches VALUES (?, ?, ?, ?, ?)", (
            "saved-before-attempt", TITLE, json.dumps([candidate]), "[]",
            "2026-11-18T00:00:00Z",
        ))
        conn.commit()
    app = create_app()
    with TestClient(app) as client:
        task, = app.state.tasks.records()
        assert task.status == "waiting"
        page = client.get(f"/task-history/{task.id}").text
        assert 'name="search_id" value="saved-before-attempt"' in page
        assert f'name="candidate_id" value="{candidate["id"]}"' in page
        assert "等待选择 · 尚未导入" in page
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_import_attempts").fetchone()[0] == 0


def test_legacy_successful_selection_resolves_older_waiting_record(paper_env):
    paper_env["ambiguous"] = True
    runner = CliRunner()
    searched = runner.invoke(cli_app, ["paper", "add", TITLE])
    assert searched.exit_code == 0, searched.output
    command = re.search(r"sb paper choose (\S+) (\S+)", searched.output)
    assert command
    selected = runner.invoke(cli_app, ["paper", "choose", *command.groups()])
    assert selected.exit_code == 0, selected.output
    app = create_app()
    with TestClient(app) as client:
        records = app.state.tasks.records()
        assert len(records) == 2
        assert not any(task.status == "waiting" for task in records)
        historic = next(task for task in records if task.status == "resolved")
        historic_page = client.get(f"/task-history/{historic.id}").text
        assert 'data-task-status="resolved"' in historic_page
        assert 'action="/papers/choose"' not in historic_page
        assert "查看后续导入结果" in historic_page


def test_running_task_cannot_be_closed_from_feedback(paper_env):
    app = create_app()
    gate = threading.Event()

    def work(task):
        task.log("运行中")
        gate.wait(10)
        return {"results": []}

    task = app.state.tasks.start("manual_add", "仍在运行的导入", work)
    try:
        with TestClient(app) as client:
            assert client.post(f"/task-history/{task.id}/dismiss").status_code == 409
            assert not task.dismissed
            assert f'data-task-record="{task.id}"' in client.get("/task-history").text
    finally:
        gate.set()
        task.thread.join(timeout=10)


def test_cli_shared_identifier_short_metadata_is_one_saved_candidate(paper_env, monkeypatch):
    short = papers._candidate(
        title="Surface", provider="crossref", authors=["Ada Example"], year=2024,
        doi="10.1234/surface-one", url="https://doi.org/10.1234/surface-one",
        version="published",
    )
    monkeypatch.setattr(papers, "_crossref", lambda *_: [short])
    result = CliRunner().invoke(cli_app, ["paper", "add", TITLE])
    assert result.exit_code == 0, result.output
    assert "abstract_only" in result.output
    conn = db.connect()
    try:
        saved = conn.execute("SELECT candidates_json FROM paper_searches").fetchone()
        candidates = json.loads(saved["candidates_json"])
        assert len(candidates) == 1
        assert candidates[0]["title"] == TITLE
        assert {record["title"] for record in candidates[0]["records"]} == {TITLE, "Surface"}
        assert len(rows()) == 1
    finally:
        conn.close()
