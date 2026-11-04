"""M7：知识图谱（ADR 0013）——/wiki/graph.json 的节点与边、/wiki/graph 页面、vendored Cytoscape。"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondbrain import config
from secondbrain.web.app import create_app
from secondbrain.wiki.graph import build_graph
from secondbrain.wiki.store import Page, new_meta, open_wiki

TODAY = date(2026, 11, 4)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    return tmp_path


def seed(env: Path):
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    kv = new_meta("KV cache", "concept", TODAY - timedelta(days=5), mastery="想回炉")
    wiki.save_page(Page("kv-cache", kv, "# KV cache\n\n[[paged-attention]] [[ghost]]"))
    later = new_meta("Batching", "concept", TODAY, mastery="扎实")
    wiki.save_page(Page("batching", later, "# Batching\n\n[[kv-cache]] [[kv-cache]]"))
    paper = new_meta("PagedAttention", "paper", TODAY)
    wiki.save_page(Page("paged-attention", paper, "# P\n\n[[kv-cache]] [[batching]]"))
    wiki.save_page(Page("vllm", new_meta("vLLM", "entity", TODAY), "# vLLM"))
    wiki.write_raw(1, "raw [[kv-cache]]", "https://x", "[[batching]]")
    return open_wiki(config.wiki_dir(), config.raw_dir())


def test_build_graph(env):
    graph = build_graph(seed(env), TODAY)
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert set(nodes) == {"kv-cache", "batching", "paged-attention", "vllm"}  # raw 不是节点
    assert graph["edges"] == [  # 无向、去重；指向不存在页面（ghost）的链接不画
        {"source": "batching", "target": "kv-cache"},
        {"source": "batching", "target": "paged-attention"},
        {"source": "kv-cache", "target": "paged-attention"},
    ]
    assert nodes["kv-cache"]["degree"] == 2 and nodes["vllm"]["degree"] == 0
    assert nodes["kv-cache"]["due"] and not nodes["batching"]["due"]  # 到期高亮
    assert nodes["paged-attention"]["due"] is False  # 只有概念页参与复习
    assert (nodes["kv-cache"]["mastery"], nodes["vllm"]["mastery"]) == ("想回炉", None)
    assert nodes["batching"]["url"] == "/wiki/page/batching"


def test_graph_endpoints(env):
    seed(env)
    client = TestClient(create_app())
    data = json.loads(client.get("/wiki/graph.json").text)
    assert len(data["nodes"]) == 4 and len(data["edges"]) == 3
    page = client.get("/wiki/graph").text
    assert 'src="/static/cytoscape.min.js"' in page and "cdn" not in page.lower()
    assert 'class="f-type" value="concept"' in page and 'value="想回炉"' in page
    assert 'href="/wiki/graph"' in client.get("/wiki").text


def test_empty_graph_shows_guide(env):
    client = TestClient(create_app())
    page = client.get("/wiki/graph").text
    assert "点「有收获」并编进 wiki 后" in page and 'id="graph"' not in page
    assert json.loads(client.get("/wiki/graph.json").text) == {"nodes": [], "edges": []}


def test_cytoscape_is_vendored(env):
    resp = TestClient(create_app()).get("/static/cytoscape.min.js")
    assert resp.status_code == 200
    assert "The Cytoscape Consortium" in resp.text[:200] and '"3.34.4"' in resp.text
