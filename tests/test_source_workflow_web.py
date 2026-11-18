"""来源发现/维护/领域分析的共同 CLI/Web 路径，不接触真实关注和模型。"""

import json
import re
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops, source_analysis, source_management, sources_file
from secondbrain.cli import app as cli_app
from secondbrain.models import FetchSettings, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.web.app import create_app

HOME = "https://huggingface.co/papers"
API = "https://huggingface.co/api/daily_papers"
TITLE = "KV cache memory management for efficient LLM inference"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    requests = []
    original_dns = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if str(host) in {"huggingface.co", "arxiv.org"} or str(host).endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return original_dns(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def transport(request):
        requests.append(str(request.url))
        if str(request.url) == API:
            return httpx.Response(200, json=[{"paper": {
                "id": "2611.10001", "title": TITLE,
                "summary": "Paged KV cache reduces memory waste in large language model serving.",
            }}])
        return httpx.Response(404)

    def client(*args, **kwargs):
        return HttpClient(FetchSettings(retries=0, per_host_interval_seconds=0),
                          transport=httpx.MockTransport(transport))

    monkeypatch.setattr("secondbrain.web.app._http_client", client)
    monkeypatch.setattr(source_management, "HttpClient", client)
    monkeypatch.setattr(source_analysis, "HttpClient", client)
    return requests


def save_sources(*sources):
    sources_file.write("sources:\n" + "\n".join(sources_file.dump_entry(s) for s in sources) + "\n")


def sample_source(**changes):
    values = dict(id="mine", name="我的论文", type="hf_papers", url=API,
                  homepage=HOME, tags=["人工保留"], trust=5, enabled=False, max_age_days=40)
    return SourceConfig(**(values | changes))


def settle(app, kind):
    task = app.state.tasks.get(kind)
    assert task is not None
    task.thread.join(20)
    assert task.status == "done", task.error
    return task.result


def test_follow_repairs_same_identity_then_edit_migrates_origins(environment):
    save_sources(sample_source(type="rss", url=HOME, needs_scraper=True),
                 SourceConfig(id="other", type="rss", url="https://other.example/feed"))
    app = create_app()
    with TestClient(app) as client:
        preview = client.post("/sources/home/preview", data={"url": HOME})
        assert preview.status_code == 200 and TITLE in preview.text
        assert environment == [API]
        result = client.post("/sources/home/follow", data={"url": HOME, "source_id": "mine"},
                             follow_redirects=False)
        assert result.status_code == 303
        source, other = config.load_sources()
        assert (source.id, other.id) == ("mine", "other")
        assert (source.type, source.url, source.name) == ("hf_papers", API, "我的论文")
        assert (source.tags, source.enabled, source.trust) == (["人工保留"], False, 5)
        imported = ops.add_manual(["https://article.example/one"], lambda _: None,
                                  reference_only=True, state="later", origin_source_id="mine")
        item_id = imported["results"][0]["item_id"]
        form = {"id": "my-papers", "name": "个人论文", "type": source.type,
                "url": source.url, "homepage": source.homepage, "tags": "人工保留",
                "trust": "5", "role": "core", "max_age_days": "40"}
        edited = client.post("/sources/mine/edit", data=form, follow_redirects=False)
        assert edited.status_code == 303
        assert [s.id for s in config.load_sources()] == ["my-papers", "other"]
        conn = db.connect()
        assert db.material_origins(conn, item_id)[0]["source_id"] == "my-papers"
        assert [r["id"] for r in db.reading_list_items(conn)] == [item_id]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
        result = CliRunner().invoke(cli_app, ["sources", "edit", "my-papers",
                                             "--set", "name=Personal Reader"])
        assert result.exit_code == 0, result.output
        assert config.load_sources()[0].name == "Personal Reader"


def test_reference_removal_does_not_delete_following_or_adapter(environment):
    save_sources(sample_source())
    before = (config.config_dir() / "sources.yaml").read_bytes()
    with TestClient(create_app()) as client:
        client.post("/sources/catalog/clear")
        assert ops.catalog_entries() == []
        preview = client.post("/sources/home/preview", data={"url": HOME})
        assert TITLE in preview.text
        assert (config.config_dir() / "sources.yaml").read_bytes() == before
        client.post("/sources/catalog/restore")
        assert any(s.id == "google-deepmind" for s in ops.catalog_entries())
        client.post("/sources/catalog/google-deepmind/hide")
        assert all(s.id != "google-deepmind" for s in ops.catalog_entries())
        assert [s.id for s in config.load_sources()] == ["mine"]
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
        conn.close()


def test_explicit_classification_keeps_tags_and_cli_reuses_cache(environment):
    save_sources(sample_source())
    before = (config.config_dir() / "sources.yaml").read_bytes()
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/sources").status_code == 200
        assert environment == []
        client.post("/sources/mine/analyze")
        analysis = settle(app, "source-profile-mine")
        assert not analysis["cached"] and analysis["profile"]["domains"]
        assert analysis["profile"]["evidence"][0]["title"] == TITLE
        assert client.get("/tasks/source-profile-mine").status_code == 200
        assert (config.config_dir() / "sources.yaml").read_bytes() == before
        conn = db.connect()
        calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        conn.close()
        result = CliRunner().invoke(cli_app, ["sources", "analyze", "mine"])
        assert result.exit_code == 0, result.output
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        stored = json.loads(conn.execute("SELECT data FROM source_analyses").fetchone()[0])
        assert stored["profile"]["evidence"][0]["url"] == "https://arxiv.org/abs/2611.10001"
        conn.close()


def test_repair_does_not_overwrite_configuration_changed_during_preview(environment, monkeypatch):
    source = sample_source()
    save_sources(source)

    def discover(*args):
        source_management.update_source("mine", {"url": "https://new.example/feed"})
        return {"source": source, "status": "ready", "title": "Old result", "warnings": []}

    monkeypatch.setattr(source_management, "discover_source", discover)
    with pytest.raises(config.ConfigError, match="检查期间"):
        source_management.repair_source("mine", lambda _: None, apply=True)
    assert config.load_sources()[0].url == "https://new.example/feed"


def test_existing_content_topics_and_catalog_hints_need_no_model(environment):
    save_sources(sample_source())
    before = (config.config_dir() / "sources.yaml").read_bytes()
    imported = ops.add_manual(
        ["https://article.example/paper"], lambda _: None, title="Memory management paper",
        reference_only=True, origin_source_id="mine", state="later",
    )
    item_id = imported["results"][0]["item_id"]
    conn = db.connect()
    conn.execute(
        "INSERT INTO assessments(item_id,stage,model,data,score,created_at) VALUES(?,?,?,?,?,?)",
        (item_id, "L1", "fake-cheap", json.dumps({"topics": ["KV cache"]}), 5, "2026-11-01"),
    )
    conn.commit()
    conn.close()
    hints = source_analysis.source_contexts()["mine"]
    assert "KV cache" in hints["existing_topics"]
    assert "paper" in hints["catalog_tags"]
    cli = CliRunner().invoke(cli_app, ["sources", "list"])
    assert cli.exit_code == 0 and "KV cache" in cli.output
    assert environment == []
    assert (config.config_dir() / "sources.yaml").read_bytes() == before
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    conn.close()


def test_following_defaults_to_latest_import_without_rewriting_configuration(environment):
    sources = [
        sample_source(id="first", name="Zulu"),
        sample_source(id="second", name="Alpha"),
        sample_source(id="third", name="Middle"),
    ]
    save_sources(*sources)
    before = (config.config_dir() / "sources.yaml").read_bytes()
    with TestClient(create_app()) as client:
        response = client.get("/sources")
    shown = re.findall(r'<article[^>]*data-source="([^"]+)"', response.text)
    assert shown == ["third", "second", "first"]
    assert [source.id for source in config.load_sources()] == ["first", "second", "third"]
    assert (config.config_dir() / "sources.yaml").read_bytes() == before
