"""Offline coverage for the explicit Web read-capture authorization boundary."""

import json
import re
import sqlite3
from contextlib import closing

import httpx
import pytest
from conftest import fast_settings, make_pdf
from fastapi.testclient import TestClient

from secondbrain import config, db, ops, papers
from secondbrain.net import HttpClient
from secondbrain.web.app import create_app

URL = "https://capture.example.org/knowledge"
BLOCKED = "https://capture.example.org/blocked"
BODY = "Knowledge compilation preserves the original source and context. " * 50


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    requested = []

    def transport(request):
        url = str(request.url)
        requested.append(url)
        if url == URL:
            return httpx.Response(200, text=(
                '<html><head><title>Knowledge compilation</title></head><body><article>'
                f'<h1>Knowledge compilation</h1><p>{BODY}</p></article></body></html>'
            ))
        return httpx.Response(403, text="Forbidden by fixture")

    def client(_settings):
        return HttpClient(fast_settings(), transport=httpx.MockTransport(transport))

    monkeypatch.setattr("secondbrain.web.app._http_client", client)
    return requested


def token(page):
    assert page.status_code == 200
    return re.search(r'name="token" value="([^\"]+)"', page.text)[1]


def submit(client, *, mode="url", files=None, **data):
    page = client.get("/quick-capture", params={"mode": mode})
    return client.post("/quick-capture", data={"token": token(page), "confirm": "yes", **data},
                       files=files, follow_redirects=False)


def settle(app, response):
    assert response.status_code == 303, response.text
    task = app.state.tasks.get(response.headers["location"].rsplit("/", 1)[1])
    task.thread.join(timeout=10)
    assert not task.running
    return task


def calls():
    with closing(db.connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]


def detail(item_id):
    with closing(db.connect()) as conn:
        return db.item_detail(conn, item_id)


def test_shared_url_has_explicit_context_and_repeat_uses_cache(offline):
    app = create_app()
    with TestClient(app) as client:
        assert 'href="/quick-capture">我读完了' in client.get("/").text
        page = client.get("/quick-capture")
        assert "fake" in page.text and "不发送独立个人笔记" in page.text
        invalid = submit(client, url=f"{URL} 和 {BLOCKED}")
        assert invalid.status_code == 422 and not app.state.tasks.records()
        no_token = client.post("/quick-capture", data={"url": URL, "confirm": "yes"})
        assert no_token.status_code == 409
        first = settle(app, submit(client, url=f"分享给你：{URL}。已读"))
        assert first.status == "done", first.error
        item_id = first.result["capture_item_id"]
        row = detail(item_id)
        assert row["read_at"] and "Knowledge compilation" in row["original_text"]
        assert first.result["capture_url"] == URL
        first_calls = calls()
        assert first_calls > 0
        second = settle(app, submit(client, url=URL, include_notes="1", force="1"))
        assert second.result["ingest"]["cached"] == [item_id]
        assert calls() == first_calls and offline == [URL]
        assert "已复用整理缓存" in client.get(f"/task-history/{second.id}").text


@pytest.mark.parametrize("upload", [False, True])
def test_text_or_pdf_supplements_link_without_network(offline, upload):
    app = create_app()
    with TestClient(app) as client:
        fields = {"files": {"pdf": ("read.pdf", make_pdf([BODY]), "application/pdf")}}
        if not upload:
            fields = {"text": BODY}
        task = settle(app, submit(client, url=BLOCKED, title="Supplied material", **fields))
        assert task.status == "done", task.error
        row = detail(task.result["capture_item_id"])
        assert row["read_at"] and row["url"] == BLOCKED
        assert row["extract_method"] == ("manual_pdf" if upload else "manual_text")
        assert BODY[:40] in row["original_text"]
        assert offline == [] and calls() > 0


def test_403_preserves_read_reference_and_requires_new_consent_after_remedy(offline):
    app = create_app()
    with TestClient(app) as client:
        task = settle(app, submit(client, url=BLOCKED))
        assert task.result["capture_fallback"] is True
        item_id = task.result["capture_item_id"]
        assert detail(item_id)["reading_state"] == "read"
        assert task.result["capture_completeness"] == "missing" and calls() == 0
        page = client.get(f"/task-history/{task.id}")
        assert "403" in page.text and "未取得正文" in page.text
        assert f'href="/item/{item_id}#complete"' in page.text
        completed = client.post(f"/item/{item_id}/complete", data={"text": BODY})
        assert "未自动调用 AI" in completed.text and calls() == 0
        assert detail(item_id)["url"] == BLOCKED
        assert detail(item_id)["original_text"].strip() == BODY.strip()
        url = f"/item/{item_id}/wiki"
        preview = client.post(url)
        assert "确认 AI 整理" in preview.text and calls() == 0
        response = client.post(url, data={"confirm": "yes", "token": token(preview)},
                               follow_redirects=False)
        assert response.status_code == 303
        organize = app.state.tasks.get(f"wiki-{item_id}")
        organize.thread.join(10)
        assert organize.status == "done" and calls() > 0


@pytest.mark.parametrize("raises", [False, True])
def test_model_failure_cannot_rollback_durable_import_or_read(offline, monkeypatch, raises):
    def fail(ids, _echo, **kwargs):
        item_id, = ids
        assert detail(item_id)["reading_state"] == "read"
        with closing(sqlite3.connect(config.data_dir() / "web_tasks.db")) as conn:
            records = [json.loads(row[0]) for row in conn.execute("SELECT record FROM tasks")]
        assert any(t["result"].get("capture_read") for t in records)
        assert kwargs["expected_backend"] == "fake" and kwargs["expected_models"]
        assert kwargs["require_read"] is True and kwargs["include_notes"] is False
        if raises:
            raise ops.OpsError("模拟模型不可用")
        return ops.IngestSummary(failures=1, errors=["模拟模型不可用"])

    monkeypatch.setattr(ops, "ingest_items", fail)
    app = create_app()
    with TestClient(app) as client:
        task = settle(app, submit(client, url=URL, learning_notes="应被忽略"))
        assert task.status == ("failed" if raises else "partial")
        item_id = task.result["capture_item_id"]
        assert not ops.material_notes(item_id)
        page = client.get(f"/task-history/{task.id}")
        assert "模拟模型不可用" in page.text and "已读记录均已保留" in page.text
        assert f'href="/item/{item_id}?organize=1#wiki-organize"' in page.text
    with TestClient(create_app()) as restarted:
        assert "打开原材料" in restarted.get(f"/task-history/{task.id}").text


def test_capture_lock_conflict_keeps_material_with_retry(offline):
    app = create_app()
    with TestClient(app) as client, ops.run_lock():
        task = settle(app, submit(client, url=URL))
        assert task.status == "failed" and task.result["capture_ai"] == "failed"
        assert detail(task.result["capture_item_id"])["read_at"] and calls() == 0
        assert "重新确认与整理" in client.get(f"/task-history/{task.id}").text


def test_configuration_change_during_import_does_not_call_other_backend(offline, monkeypatch):
    register = ops.register_read

    def switch_backend(item_id):
        register(item_id)
        monkeypatch.setenv("SB_LLM_BACKEND", "deepseek")

    monkeypatch.setattr(ops, "register_read", switch_backend)
    app = create_app()
    with TestClient(app) as client:
        task = settle(app, submit(client, url=URL))
        assert task.status == "failed" and "费用上限已变更" in task.error
        assert detail(task.result["capture_item_id"])["read_at"] and calls() == 0
        assert "重新确认与整理" in client.get(f"/task-history/{task.id}").text


def test_stored_abstract_does_not_run_model(offline):
    item_id = ops.add_manual([BLOCKED], lambda _: None, text=BODY)["results"][0]["item_id"]
    with closing(db.connect()) as conn:
        conn.execute("UPDATE items SET content_completeness='abstract' WHERE id=?", (item_id,))
        conn.commit()
    app = create_app()
    with TestClient(app) as client:
        task = settle(app, submit(client, url=BLOCKED))
        assert task.result["capture_ai"] == "skipped" and calls() == 0
        assert "未取得全文，仅有摘要" in client.get(f"/task-history/{task.id}").text


def test_paper_choice_uses_persisted_grant_not_form_values(offline, monkeypatch):
    def search(query, _echo, *, ingest=False, client):
        assert not ingest
        return {
            "query": query, "search_id": query, "status": "ambiguous",
            "candidates": [{"id": "candidate-1", "title": "Exact name", "authors": []}],
        }

    def choose(search_id, candidate_id, echo, *, ingest=False, client):
        assert not ingest and candidate_id == "candidate-1"
        assert search_id in {"Exact name", "Normal"}
        row = ops.add_manual([], echo, text=f"{search_id}\n\n{BODY}", title=search_id)["results"][0]
        return {"status": "imported", "search_id": search_id, "material_id": row["item_id"]}

    monkeypatch.setattr(papers, "import_by_name", search)
    monkeypatch.setattr(papers, "choose_candidate", choose)
    app = create_app()
    forged = {"state": "read", "ingest": "1", "quick_capture": "1", "expected_backend": "fake"}
    with TestClient(app) as client:
        response = client.post("/add", data={"text": BODY, **forged})
        assert response.status_code == 200
        app.state.tasks.get("manual_add").thread.join(10)
        assert calls() == 0
        normal_response = client.post("/papers/add", data={"query": "Normal", **forged},
                                      follow_redirects=False)
        normal = settle(app, normal_response)
        assert normal.status == "waiting" and "quick_capture" not in normal.result
        route = f"/quick-capture/papers/{normal.id}/choose"
        assert client.post(route, data={"candidate_id": "candidate-1", **forged}).status_code == 409
        ordinary = settle(app, client.post("/papers/choose", follow_redirects=False, data={
            **forged, "search_id": "Normal", "candidate_id": "candidate-1",
        }))
        assert ordinary.status == "done" and calls() == 0
        paper = settle(app, submit(client, mode="paper", query="Exact name"))
        assert paper.status == "waiting" and calls() == 0
    restarted = create_app()
    with TestClient(restarted) as client:
        route = f"/quick-capture/papers/{paper.id}/choose"
        assert route in client.get(f"/task-history/{paper.id}").text
        assert client.post(route, data={"candidate_id": "unknown"}).status_code == 409
        chosen = settle(restarted, client.post(route, follow_redirects=False, data={
            "candidate_id": "candidate-1", "search_id": "forged", "backend": "forged",
        }))
        assert chosen.status == "done", chosen.error
        assert detail(chosen.result["material_id"])["read_at"] and calls() > 0
        persisted = restarted.state.tasks.get(paper.id)
        assert persisted.status == "resolved" and persisted.result["resolved_by"] == chosen.id
        assert client.post(route, data={"candidate_id": "candidate-1"}).status_code == 409
    assert create_app().state.tasks.get(paper.id).status == "resolved"
