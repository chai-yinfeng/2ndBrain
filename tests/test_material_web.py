"""Material-first UI: independent reading, notes and wiki without subscriptions/digests."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.web.app import create_app


@pytest.fixture
def isolated_materials(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    (config.config_dir() / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    return tmp_path


def test_cli_standalone_reading_and_reimport_preserve_state(isolated_materials):
    runner = CliRunner()
    text = isolated_materials / "input.txt"
    text.write_text("KV cache notes\n" + "PagedAttention stores KV cache in blocks. " * 30)
    result = runner.invoke(cli_app, ["add", "--text-file", str(text), "--title", "KV notes"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["items", "read-state", "1", "reading"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["add", "--text-file", str(text)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["items", "show", "1"])
    assert "reading" in result.output and "PagedAttention stores KV cache" in result.output
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    finally:
        conn.close()


def test_web_import_notes_and_wiki_do_not_mark_read(isolated_materials):
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/add", data={
            "title": "KV cache web material", "text": "KV cache blocks. " * 60,
            "state": "unread",
        })
        assert response.status_code == 200
        task = app.state.tasks.get("manual_add")
        task.thread.join(20)
        assert task.status == "done", task.error
        item_id = task.result["results"][0]["item_id"]
        assert item_id is not None, task.result
        assert "KV cache web material" in client.get("/materials").text
        response = client.post(f"/item/{item_id}/notes", data={"notes": "我的独立笔记"})
        assert response.status_code == 200
        assert client.post(f"/item/{item_id}/wiki").status_code == 409
        assert app.state.tasks.get(f"wiki-{item_id}") is None
        assert client.get(f"/wiki/page/material-{item_id}").status_code == 404

        result = CliRunner().invoke(cli_app, ["wiki", "ingest", str(item_id)])
        assert result.exit_code == 0, result.output
        html = client.get(f"/item/{item_id}").text
        assert 'data-reading-state="unread"' in html
        assert "我的独立笔记" in html
        assert f'/wiki/page/material-{item_id}' in html
        assert client.get(f"/wiki/page/material-{item_id}").status_code == 200
        assert ops.material_notes(item_id) == "我的独立笔记"
        assert "没有符合条件的材料" in client.get("/materials?reading=read").text
        client.post(f"/item/{item_id}/reading", data={"state": "reading"})
        assert 'data-reading-state="reading"' in client.get(f"/item/{item_id}").text


def test_vault_resources_cannot_escape_or_execute_html(isolated_materials):
    root = config.wiki_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "unsafe.html").write_text("<script>alert(1)</script>")
    (root / "notes.md").write_text("<script>alert(1)</script>")
    outside = isolated_materials / "outside.txt"
    outside.write_text("private")
    (root / "escape.txt").symlink_to(outside)
    with TestClient(create_app()) as client:
        assert client.get("/vault/unsafe.html").status_code == 400
        assert client.get("/vault/escape.txt").status_code == 404
        response = client.get("/vault/notes.md")
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["x-content-type-options"] == "nosniff"
