"""论文名 CLI/Web 使用真实共享服务；仅替换元数据及正文网络传输。"""

import re
from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, make_pdf
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import db, papers
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
        task.thread.join(timeout=10)
        assert not task.running


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
            "/papers/add", data={"query": TITLE, "ingest": "1"}, follow_redirects=False
        )
        settle(app)
        page = client.get(response.headers["location"])
        assert 'data-paper-status="imported"' in page.text
        assert f'/item/{first["id"]}' in page.text
        assert f'/wiki/page/material-{first["id"]}' in page.text
        assert f'/wiki/graph?focus=material-{first["id"]}' in page.text
        assert client.get(f'/item/{first["id"]}').status_code == 200
        assert client.get(f'/wiki/page/material-{first["id"]}').status_code == 200
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
        response = client.post("/papers/add", data={"query": TITLE, "ingest": "1"})
        settle(app)
        response = client.get(response.url)
        assert "尚未完成" in response.text
        assert "材料仍已保存" in response.text
        assert rows()[0]["reading_state"] == "unread"


def test_web_ambiguous_selection_retains_requested_wiki_processing(paper_env):
    paper_env["full"] = True
    paper_env["ambiguous"] = True
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/papers/add", data={"query": TITLE, "ingest": "1"})
        settle(app)
        response = client.get(response.url)
        assert 'name="ingest" value="1" checked' in response.text
        task = next(task for task in app.state.tasks._tasks.values() if task.result)
        result = task.result
        chosen = client.post("/papers/choose", data={
            "search_id": result["search_id"],
            "candidate_id": result["candidates"][0]["id"], "ingest": "1",
        })
        settle(app)
        assert "/wiki/page/material-" in client.get(chosen.url).text
