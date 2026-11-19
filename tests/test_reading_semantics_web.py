"""外部阅读登记、手动清单、信源预览与人工关系的共同业务路径。"""

import re
import socket
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.models import FetchSettings
from secondbrain.net import HttpClient
from secondbrain.wiki.store import Page, new_meta, open_wiki

HOME = "https://reading.example/"
POST = HOME + "posts/kv-cache"
FEED = b'''<rss version="2.0"><channel><title>My blog</title><link>https://reading.example/</link>
<item><title>KV cache and PagedAttention</title><link>https://reading.example/posts/kv-cache</link></item>
</channel></rss>'''
BODY = "KV cache and PagedAttention preserve inference state in blocks. " * 60


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    requested = []
    original_dns = socket.getaddrinfo

    def dns(host, port, *args, **kwargs):
        if str(host).endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return original_dns(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)

    def transport(request):
        requested.append(str(request.url))
        if str(request.url) == HOME:
            return httpx.Response(200, text=(
                '<html><head><title>My blog</title><link rel="alternate" '
                'type="application/rss+xml" href="/feed"/></head><body>My blog</body></html>'
            ))
        if str(request.url) == HOME + "feed":
            return httpx.Response(200, content=FEED)
        if str(request.url) == POST:
            return httpx.Response(200, text=(
                '<html><head><meta name="citation_title" content="KV cache and PagedAttention">'
                '</head><body><article><p>' + BODY + '</p></article></body></html>'
            ))
        return httpx.Response(404)

    def client(*args, **kwargs):
        return HttpClient(
            FetchSettings(per_host_interval_seconds=0, retries=0),
            transport=httpx.MockTransport(transport),
        )

    monkeypatch.setattr("secondbrain.web.app._http_client", client)
    monkeypatch.setattr(ops, "HttpClient", client)
    return requested


def settle(app, kind):
    task = app.state.tasks.get(kind)
    assert task is not None
    task.thread.join(20)
    assert task.status == "done", task.error
    return task.result


def count_calls():
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    finally:
        conn.close()


def test_follow_preview_pick_register_cache_and_human_override(environment):
    from secondbrain.web.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert config.load_sources() == []
        assert not (config.config_dir() / "sources.yaml").exists()
        preview = client.post("/sources/home/preview", data={"url": HOME})
        assert preview.status_code == 200
        assert POST in preview.text
        assert not (config.config_dir() / "sources.yaml").exists()
        source_id = re.search(r'name="source_id" value="([^"]+)"', preview.text)[1]
        saved = client.post("/sources/home/follow", data={"url": HOME, "source_id": source_id})
        assert saved.status_code == 200
        assert len(config.load_sources()) == 1
        conn = db.connect()
        assert db.reading_list_items(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        conn.close()
        assert client.post(f"/sources/{source_id}/peek").status_code == 200
        assert count_calls() == 0
        client.post("/reading/import", data={"url": POST, "origin_source_id": source_id})
        imported = settle(app, "manual_add")
        item_id = imported["results"][0]["item_id"]
        assert item_id is not None
        conn = db.connect()
        assert db.material_origins(conn, item_id)[0]["source_id"] == source_id
        assert db.item_detail(conn, item_id)["status"] == "manual"
        conn.close()
        assert f'data-item-id="{item_id}"' in client.get("/later").text
        assert client.get("/wiki/graph.json").json()["nodes"] == []
        notes = "我在外部讨论理解了 KV cache；这段个人笔记不能被 AI 覆盖。"
        payload = {"state": "read", "notes": notes,
                   "discussion_url": "https://chatgpt.com/c/learning-reference"}
        client.post(f"/item/{item_id}/reading", data=payload)
        registered = settle(app, f"read-{item_id}")
        assert registered["ingest"]["failures"] == 0
        assert f'data-item-id="{item_id}"' not in client.get("/later").text
        assert "已登记阅读" in client.get(f"/item/{item_id}").text
        assert ops.material_notes(item_id) == notes
        assert not any("chatgpt.com" in url for url in environment)
        nodes = client.get("/wiki/graph.json").json()["nodes"]
        assert any(node["id"] == f"material-{item_id}" for node in nodes)
        before = count_calls()
        client.post(f"/item/{item_id}/reading", data=payload)
        assert settle(app, f"read-{item_id}")["cached"]
        assert count_calls() == before
        source = f"material-{item_id}"
        client.post("/wiki/relation", data={
            "source": source, "target": "kv-cache", "mode": "block", "reason": "这里不要直接连接",
        })
        client.post(f"/item/{item_id}/reading", data={**payload, "force": "1"})
        assert not settle(app, f"read-{item_id}")["ingest"]["failures"]
        edges = client.get("/wiki/graph.json").json()["edges"]
        assert not any({e["source"], e["target"]} == {source, "kv-cache"} for e in edges)
        page = client.get(f"/wiki/page/{source}").text
        assert 'data-human-mode="block"' in page and "这里不要直接连接" in page
        assert ops.material_notes(item_id) == notes
        client.post("/wiki/relation", data={
            "source": source, "target": "kv-cache", "mode": "pin", "reason": "用户确认的关联",
        })
        edges = client.get("/wiki/graph.json").json()["edges"]
        assert any({e["source"], e["target"]} == {source, "kv-cache"} for e in edges)
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
        conn.close()


def test_web_reference_only_external_learning_and_cli_reuse(environment):
    from secondbrain.web.app import create_app

    app = create_app()
    with TestClient(app) as client:
        client.post("/add", data={"urls": "https://website.example/", "title": "网站学习",
                                  "reference_only": "1", "state": "later"})
        imported = settle(app, "manual_add")
        item_id = imported["results"][0]["item_id"]
        assert environment == []
        notes = "学习记录：KV cache 是注意力计算中的缓存。"
        client.post(f"/item/{item_id}/reading", data={"state": "read", "notes": notes})
        result = settle(app, f"read-{item_id}")
        assert not result["ingest"]["failures"]
        conn = db.connect()
        row = db.item_detail(conn, item_id)
        assert row["content_completeness"] == "missing" and row["content"] == ""
        assert row["reading_state"] == "read"
        conn.close()
        before = count_calls()
        out = CliRunner().invoke(cli_app, ["reading", "read", str(item_id)])
        assert out.exit_code == 0, out.output
        assert count_calls() == before
        assert environment == []


def test_reference_identity_is_not_title_only_and_drafts_are_not_knowledge(environment):
    first = ops.add_manual(["https://one.example/"], lambda _: None, reference_only=True,
                           title="Same title", state="later")
    second = ops.add_manual(["https://two.example/"], lambda _: None, reference_only=True,
                            title="Same title", state="later")
    assert first["results"][0]["item_id"] != second["results"][0]["item_id"]
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    meta = new_meta("Known-looking draft", "concept", date.today())
    meta["mastery"] = "扎实"  # 历史字段不改变草稿的可见性。
    meta["knowledge_status"] = "draft"
    wiki.save_page(Page("draft", meta, "A draft"))
    out = CliRunner().invoke(cli_app, ["wiki", "graph", "--focus", "draft"])
    assert out.exit_code != 0
    out = CliRunner().invoke(cli_app, ["wiki", "graph", "--focus", "draft", "--drafts"])
    assert out.exit_code == 0 and "/wiki/page/draft" in out.output


def test_missing_notes_field_cannot_erase_existing_user_notes(environment):
    from secondbrain.web.app import create_app

    result = ops.add_manual([], lambda _: None, text="Original source facts")
    item_id = result["results"][0]["item_id"]
    ops.material_notes(item_id, "请保留这段用户笔记")
    with TestClient(create_app()) as client:
        assert client.post(f"/item/{item_id}/notes", data={"text": "old field"}).status_code == 422
        assert ops.material_notes(item_id) == "请保留这段用户笔记"
        assert client.post(f"/item/{item_id}/notes", data={"notes": ""}).status_code == 200
        assert ops.material_notes(item_id) == ""
    assert count_calls() == 0
