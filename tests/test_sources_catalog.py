"""M11（ADR 0018）：按文本块编辑 sources.yaml（保留注释）、信源目录与预览、空信源、反馈分布。"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, fixture_bytes
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db
from secondbrain import sources_file as sf
from secondbrain.cli import app as cli_app
from secondbrain.models import SourceConfig
from secondbrain.net import HttpClient
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

runner = CliRunner()
EXAMPLE = (Path(__file__).resolve().parents[1] / "config" / "sources.example.yaml").read_text(
    encoding="utf-8"
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")

    def offline_client(fs):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://vllm.ai/blog/rss.xml":
                return httpx.Response(200, content=fixture_bytes("rss.xml"))
            return httpx.Response(404)

        return HttpClient(fast_settings(), transport=httpx.MockTransport(handler))

    monkeypatch.setattr("secondbrain.web.app._http_client", offline_client)
    return tmp_path


def personal() -> Path:
    return config.config_dir() / "sources.yaml"


# ---------- 文本块编辑 ----------


def test_every_example_entry_round_trips_through_empty_config():
    ids = [s.id for s in config.parse_sources_text(EXAMPLE)]
    text = "sources: []\n"
    for sid in ids:
        text = sf.append_entry(text, sf.entry_text(EXAMPLE, sid))
    assert [s.id for s in config.parse_sources_text(text)] == ids
    assert "# 排除分类列表页" in text  # 条目内的注释随条目带过来
    for sid in ids:
        text = sf.remove_entry(text, sid)
    assert config.parse_sources_text(text) == [] and "sources: []" in text


def test_edits_keep_comments_and_other_entries():
    removed = sf.remove_entry(EXAMPLE, "vllm-blog")
    assert removed.count("#") == EXAMPLE.count("#")  # 注释一行不少
    assert "- id: vllm-blog" not in removed and "- id: lmsys-sglang-blog" in removed
    toggled = sf.set_enabled(EXAMPLE, "gwern", True)
    gwern = next(s for s in config.parse_sources_text(toggled) if s.id == "gwern")
    assert gwern.enabled and "enabled: true  # 停更" in toggled
    no_enabled = "sources:\n  - id: a\n    type: rss\n    url: https://a\n"
    assert config.parse_sources_text(sf.set_enabled(no_enabled, "a", False))[0].enabled is False
    with pytest.raises(config.ConfigError):
        sf.remove_entry(EXAMPLE, "nope")


def test_empty_sources_variants_parse():
    assert config.parse_sources_text("sources: []\n") == []
    assert config.parse_sources_text("# comment\nsources:\n") == []
    with pytest.raises(config.ConfigError):
        config.parse_sources_text("other: 1\n")


def test_write_validates_before_replacing(env):
    personal().write_text("sources: []\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        sf.write("sources:\n  - id: Bad Id\n    type: rss\n    url: u\n")
    assert personal().read_text(encoding="utf-8") == "sources: []\n"


# ---------- 信源目录 ----------


def test_catalog_lists_missing_entries_and_adds_verbatim(env):
    personal().write_text("# 我的信源\nsources: []\n", encoding="utf-8")
    client = TestClient(create_app())
    html = client.get("/sources").text
    assert "还没有信源" in html and 'data-catalog="vllm-blog"' in html
    arxiv = html.split('data-catalog="arxiv-cs-cl"', 1)[1].split("</div>", 1)[0]
    assert "论文类" in arxiv and 'href="https://arxiv.org/list/cs.CL/recent"' in arxiv
    assert (
        '<span class="tag">论文类</span>' not in html.split('data-catalog="vllm-blog"', 1)[1][:400]
    )

    resp = client.post("/sources/catalog/anthropic-news/add", follow_redirects=False)
    assert resp.status_code == 303
    text = personal().read_text(encoding="utf-8")
    assert text.startswith("# 我的信源\nsources:\n")
    assert sf.entry_text(EXAMPLE, "anthropic-news") in text  # 原样追加（含注释）
    html = client.get("/sources").text
    assert 'data-source="anthropic-news"' in html and 'data-catalog="anthropic-news"' not in html
    assert client.post("/sources/catalog/anthropic-news/add").status_code == 400
    assert client.post("/sources/catalog/nope/add").status_code == 404


def test_preview_catalog_and_configured_sources(env):
    personal().write_text("sources: []\n", encoding="utf-8")
    client = TestClient(create_app())
    peek = client.post("/sources/vllm-blog/peek").text
    assert "解析到 3 条" in peek and "Paged KV cache explained" in peek and "2026-11-03" in peek
    failed = client.post("/sources/pytorch-blog/peek").text
    assert "抓取失败" in failed and "HTTP 404" in failed
    assert "needs_scraper" in client.post("/sources/hazy-research/peek").text
    assert client.post("/sources/nope/peek").status_code == 404
    client.post("/sources/catalog/vllm-blog/add")
    assert "解析到 3 条" in client.post("/sources/vllm-blog/peek").text  # 已配置的也能预览
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0  # 预览不写库
    conn.close()


# ---------- 空信源 ----------


def test_empty_sources_are_friendly_everywhere(env):
    personal().write_text("sources: []\n", encoding="utf-8")
    fetch = runner.invoke(cli_app, ["fetch"])
    assert fetch.exit_code == 0 and "还没有启用的信源，去 /sources 添加" in fetch.output
    run = runner.invoke(cli_app, ["run"])
    assert run.exit_code == 0 and "还没有启用的信源" in run.output
    assert "L1：" not in run.output  # 不进入流水线、不调用 LLM
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    conn.close()
    client = TestClient(create_app())
    home = client.get("/").text
    assert "还没有启用的信源" in home and "开始今日过滤" not in home
    assert client.get("/sources").status_code == 200 and client.get("/ops").status_code == 200
    tune = runner.invoke(cli_app, ["sources", "tune"])
    assert tune.exit_code == 0 and "还没有信源" in tune.output


def test_delete_keeps_history(env):
    personal().write_text(EXAMPLE, encoding="utf-8")
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="vllm-blog", type="rss", url="u")])
    body = "body " * 50
    db.insert_item(
        conn,
        db.NewItem(
            "vllm-blog",
            "https://v/1",
            "https://v/1",
            content_hash("t", body),
            "t",
            None,
            None,
            body,
            "feed",
            None,
        ),
        datetime.now(UTC),
    )
    conn.commit()
    item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    db.add_feedback(conn, item, "gained", None, datetime.now(UTC))
    conn.close()
    client = TestClient(create_app())
    client.post("/sources/vllm-blog/delete", data={"confirm": "yes"})
    assert "- id: vllm-blog" not in personal().read_text(encoding="utf-8")
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    conn.close()
    assert client.get(f"/item/{item}").status_code == 200


def test_sources_page_shows_feedback_distribution(env):
    personal().write_text(EXAMPLE, encoding="utf-8")
    conn = db.connect()
    db.upsert_sources(conn, config.load_sources())
    for n, kind in enumerate(["gained", "too_hard", "later"]):
        body = f"body {n} " * 50
        db.insert_item(
            conn,
            db.NewItem(
                "vllm-blog",
                f"https://v/{n}",
                f"https://v/{n}",
                content_hash(str(n), body),
                f"t{n}",
                None,
                None,
                body,
                "feed",
                None,
            ),
            datetime.now(UTC),
        )
        item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
        db.add_feedback(conn, item, kind, None, datetime.now(UTC))
    conn.commit()
    conn.close()
    html = TestClient(create_app()).get("/sources").text
    row = html.split('data-source="vllm-blog"', 1)[1].split("</tr>", 1)[0]
    assert 'data-dist="1/0/1/0/0/1"' in row and "有收获 1" in row and "稍后读 1" in row
    assert "建议" not in row  # 纯参考，不附建议
