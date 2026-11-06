"""M11（ADR 0018）：手动添加文章（已读 / 准备读）——CLI `sb add` 与 Web `/add`。全部离线。"""

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, fixture_bytes
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.loader import parse_profile
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app
from secondbrain.wiki.store import open_wiki

runner = CliRunner()
POST = "https://blog.example.com/post"
OTHER = "https://blog.example.com/other"
DOWN = "https://down.example.com/paywalled"
LONG_HTML = (
    "<html><head><title>A long arXiv paper</title></head><body><article>"
    + "".join(
        f"<p>Section {n}: speculative decoding detail number {n} and KV cache.</p>"
        for n in range(60)
    )
    + "</article></body></html>"
).encode()
ROUTES = {
    POST: httpx.Response(200, content=fixture_bytes("article.html")),
    OTHER: httpx.Response(200, content=fixture_bytes("sitemap_article.html")),
    "https://export.arxiv.org/api/query": httpx.Response(
        200, content=fixture_bytes("arxiv_api.xml")
    ),
    "https://arxiv.org/html/2611.01234": httpx.Response(200, content=LONG_HTML),
}


def mock_client(*_args, **_kwargs) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return ROUTES.get(str(request.url.copy_with(query=None)), httpx.Response(404))

    return HttpClient(fast_settings(), transport=httpx.MockTransport(handler))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(ops, "HttpClient", mock_client)
    monkeypatch.setattr("secondbrain.web.app._http_client", mock_client)
    return tmp_path


def item_row(item_id: int):
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()


def state_of(item_id: int):
    conn = db.connect()
    try:
        return db.feedback_state(conn, [item_id])[item_id]
    finally:
        conn.close()


def test_validation():
    with pytest.raises(ops.OpsError, match="至少"):
        ops.validate_manual(["  ", ""], "later", None, False, None)
    with pytest.raises(ops.OpsError, match="http"):
        ops.validate_manual(["javascript:alert(1)"], "later", None, False, None)
    with pytest.raises(ops.OpsError, match="已读"):
        ops.validate_manual([POST], "later", "gained", False, None)
    with pytest.raises(ops.OpsError, match="有收获"):
        ops.validate_manual([POST], "read", "too_easy", True, None)
    with pytest.raises(ops.OpsError, match="单个"):
        ops.validate_manual([POST, OTHER], "read", None, False, "text")
    assert ops.validate_manual([POST, POST, " "], "read", None, False, None) == [POST]


def test_cli_add_later_and_dedupe(env):
    out = runner.invoke(cli_app, ["add", POST, OTHER, DOWN])  # 默认 --later
    assert out.exit_code == 0, out.output
    assert "合计：新增 2，已存在 0，失败 1" in out.output
    assert "✗ https://down.example.com/paywalled：无法获取正文" in out.output
    conn = db.connect()
    rows = conn.execute("SELECT id, source_id, status, title, read_at FROM items").fetchall()
    later = [r["id"] for r in db.items_in_state(conn, "later")]
    conn.close()
    assert {r["source_id"] for r in rows} == {"manual"} and {r["status"] for r in rows} == {
        "manual"
    }
    assert {r["title"] for r in rows} == {
        "Continuous batching in practice",
        "Hierarchical KV cache for distributed serving",
    }
    assert sorted(later) == sorted(r["id"] for r in rows) and all(not r["read_at"] for r in rows)

    again = runner.invoke(cli_app, ["add", POST, "--read", "--feedback", "too_easy"])
    assert "已存在" in again.output and "合计：新增 0，已存在 1，失败 0" in again.output
    first = next(r["id"] for r in rows if r["title"].startswith("Continuous"))
    assert state_of(first) == "too_easy" and item_row(first)["read_at"]
    assert runner.invoke(cli_app, ["add", POST, "--feedback", "gained"]).exit_code == 1


def test_read_without_feedback_and_pipeline_ignores_manual(env):
    runner.invoke(cli_app, ["add", POST, "--read"])
    conn = db.connect()
    item = conn.execute("SELECT id FROM items").fetchone()[0]
    assert db.feedback_for(conn, item) == []  # 已读、无评价：不写反馈
    report = run_pipeline(
        conn,
        FilterSettings(),
        FakeBackend(),
        parse_profile(""),
        limit=None,
        now=datetime.now(UTC),
        today=date.today(),
    )
    assert report.l0_checked == 0 and report.l1_selected == 0  # 不进 L1 / L2 / 分级
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    conn.close()
    assert state_of(item) is None and item_row(item)["read_at"]
    assert item_row(item)["status"] == "manual"


def test_existing_feed_item_keeps_its_source_and_status(env):
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="https://b")])
    body = "existing body " * 40
    db.insert_item(
        conn,
        db.NewItem(
            "blog",
            POST,
            POST,
            content_hash("Existing", body),
            "Existing",
            None,
            None,
            body,
            "feed",
            None,
        ),
        datetime.now(UTC),
    )
    conn.commit()
    item = conn.execute("SELECT id FROM items").fetchone()[0]
    conn.close()
    out = runner.invoke(cli_app, ["add", POST + "?utm_source=x", "--read", "--feedback", "gained"])
    assert "已存在" in out.output
    row = item_row(item)
    assert (row["source_id"], row["status"]) == ("blog", "new") and state_of(item) == "gained"


def test_paste_text_fallback_and_arxiv(env, tmp_path):
    text = tmp_path / "body.txt"
    text.write_text("Paywalled essay title\n\n" + "Full text pasted by the user. " * 40, "utf-8")
    out = runner.invoke(cli_app, ["add", DOWN, "--read", "--text-file", str(text)])
    assert "新增" in out.output, out.output
    conn = db.connect()
    row = conn.execute("SELECT * FROM items WHERE url = ?", (DOWN,)).fetchone()
    assert row["extract_method"] == "manual_text" and row["title"] == "Paywalled essay title"

    out = runner.invoke(cli_app, ["add", "https://arxiv.org/pdf/2611.01234v3"])
    assert "新增" in out.output, out.output
    paper = conn.execute("SELECT * FROM items WHERE url LIKE '%arxiv%'").fetchone()
    conn.close()
    assert paper["url"] == "https://arxiv.org/abs/2611.01234"
    assert (
        paper["title"] == "Speculative Decoding at Scale"
        and paper["extract_method"] == "arxiv_html"
    )
    dup = runner.invoke(cli_app, ["add", "https://arxiv.org/abs/2611.01234"])
    assert "已存在" in dup.output  # 不同形式的 arXiv 链接规范化为同一条


def test_cli_ingest_gained(env):
    out = runner.invoke(cli_app, ["add", OTHER, "--read", "--feedback", "gained", "--ingest"])
    assert out.exit_code == 0, out.output
    assert "编入 wiki：1 条" in out.output and "编入 wiki 费用" in out.output
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    assert wiki.pages  # fake 后端生成了页面
    conn = db.connect()
    assert len(db.ingested_ids(conn)) == 1
    conn.close()


def test_web_add_page_and_task(env):
    client = TestClient(create_app())
    page = client.get("/add").text
    assert 'name="urls"' in page and 'value="read"' in page and 'name="ingest"' in page
    assert 'href="/add"' in client.get("/").text and 'href="/add"' in client.get("/later").text
    bad = client.post("/add", data={"urls": POST, "state": "later", "feedback": "gained"}).text
    assert "读后反馈只能在「已读」时选择" in bad
    client.post("/add", data={"urls": f"{POST}\n{DOWN}\n", "state": "read", "feedback": "gained"})
    task = client.app.state.tasks.get("manual_add")
    task.thread.join(10)
    status = client.get("/tasks/manual_add").text
    assert 'data-add-status="新增"' in status and 'data-add-status="失败"' in status
    assert "Continuous batching in practice" in status
    home = client.get("/").text
    assert 'data-to-ingest="1"' in home  # 有收获的手动条目进入队列（没有 digest 时也显示）
    sources = client.get("/sources").text
    assert 'data-source="manual"' not in sources  # 虚拟信源不出现在信源页
    conn = db.connect()
    assert conn.execute("SELECT type, enabled FROM sources WHERE id = 'manual'").fetchone()[:] == (
        "manual",
        0,
    )
    item = conn.execute("SELECT id FROM items").fetchone()[0]
    conn.close()
    detail = client.get(f"/item/{item}").text
    assert f'hx-post="/summary/{item}"' in detail and f'hx-post="/prereq/{item}"' in detail
