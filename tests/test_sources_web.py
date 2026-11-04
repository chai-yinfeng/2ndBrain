"""M7：Web /sources 信源管理（ADR 0013）——列表、试抓、保存、启停、删除确认、最近抓取记录。"""

import shutil
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES, fast_settings, fixture_bytes
from fastapi.testclient import TestClient

from secondbrain import config, db
from secondbrain.ingest import fetch_sources
from secondbrain.models import SourceConfig
from secondbrain.net import HttpClient
from secondbrain.web.app import create_app

FEED = "https://blog.example.com/feed.xml"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURES / "profile.md", data / "profile.md")
    conf = tmp_path / "config"
    conf.mkdir()
    for name in ("settings.example.yaml", "sources.example.yaml", "profile.example.md"):
        shutil.copy(config.CONFIG_DIR / name, conf / name)
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setattr(config, "CONFIG_DIR", conf)

    def offline_client(fs):
        routes = {
            FEED: httpx.Response(200, content=fixture_bytes("rss.xml")),
            "https://blog.example.com/sitemap.xml": httpx.Response(
                200, content=fixture_bytes("sitemap_dated.xml")
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return routes.get(str(request.url), httpx.Response(404))

        return HttpClient(fast_settings(), transport=httpx.MockTransport(handler))

    monkeypatch.setattr("secondbrain.web.app._http_client", offline_client)
    return tmp_path


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


def personal(env: Path) -> Path:
    return env / "config" / "sources.yaml"


def test_list_reads_example_until_first_change(client, env):
    html = client.get("/sources").text
    assert "sources.example.yaml" in html and "第一次修改时会先复制" in html
    assert 'data-source="vllm-blog"' in html and 'data-source="lmsys-sglang-blog"' in html
    assert not personal(env).exists()
    assert 'href="/sources"' in html


def test_preview_does_not_write(client, env):
    form = {"id": "my-blog", "type": "rss", "url": FEED, "trust": "4", "tags": "a, b，c"}
    html = client.post("/sources/preview", data=form).text
    assert "试抓成功：解析到 3 条" in html
    assert "Paged KV cache explained" in html and "2026-11-03" in html
    assert 'action="/sources/add"' in html and 'name="tags" value="a, b，c"' in html
    assert not personal(env).exists()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    conn.close()


def test_preview_sitemap_and_validation(client):
    form = {
        "id": "sm",
        "type": "sitemap",
        "url": "https://blog.example.com/sitemap.xml",
        "url_pattern": r"/blog/\d{4}-",
        "use_lastmod": "1",
    }
    html = client.post("/sources/preview", data=form).text
    assert "试抓成功：解析到 4 条" in html and "URL slug" in html
    assert "2026-11-02-hicache" in html
    bad = client.post("/sources/preview", data={"id": "Bad ID", "type": "rss", "url": "x"}).text
    assert "✗" in bad and "id" in bad
    dup = client.post("/sources/preview", data={"id": "vllm-blog", "type": "rss", "url": FEED})
    assert "信源 id 已存在：vllm-blog" in dup.text
    missing = client.post("/sources/preview", data={"id": "x", "type": "rss", "url": FEED + "x"})
    assert "试抓失败" in missing.text


def test_add_toggle_delete(client, env):
    form = {"id": "my-blog", "type": "rss", "url": FEED, "trust": "4", "tags": "a, b"}
    resp = client.post("/sources/add", data=form, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/sources?saved=my-blog"
    text = personal(env).read_text(encoding="utf-8")
    assert text.startswith("# 个人信源配置") and "sources.example.yaml" in text
    sources = config.load_sources(personal(env))
    added = next(s for s in sources if s.id == "my-blog")
    assert (added.trust, added.tags, added.enabled) == (4, ["a", "b"], True)
    assert any(s.id == "vllm-blog" for s in sources)  # 从 example 复制后追加
    assert "needs_scraper: false" not in text  # 默认值不写出
    assert not list((env / "config").glob(".*.tmp"))  # 原子写入不留临时文件
    assert client.post("/sources/add", data=form).status_code == 400  # id 唯一

    client.post("/sources/my-blog/toggle")
    assert not next(s for s in config.load_sources() if s.id == "my-blog").enabled
    assert "启用" in client.get("/sources").text

    confirm = client.post("/sources/my-blog/delete")
    assert confirm.status_code == 200 and "确认删除" in confirm.text
    assert any(s.id == "my-blog" for s in config.load_sources())  # 未确认不删除
    client.post("/sources/my-blog/delete", data={"confirm": "yes"})
    assert all(s.id != "my-blog" for s in config.load_sources())
    assert client.post("/sources/nope/toggle").status_code == 404


def test_last_fetch_is_recorded_and_shown(client, env, make_client):
    http, _ = make_client({FEED: httpx.Response(200, content=fixture_bytes("rss.xml"))})
    sources = [
        SourceConfig(id="vllm-blog", type="rss", url=FEED, max_age_days=3650),
        SourceConfig(id="pytorch-blog", type="rss", url="https://down.example.com/feed"),
    ]
    conn = db.connect()
    fetch_sources(conn, sources, http, http.settings)
    overview = db.source_overview(conn)
    conn.close()
    assert overview["vllm-blog"]["last_fetch_status"] == "ok"
    assert overview["vllm-blog"]["items"] == overview["vllm-blog"]["last_fetch_added"] > 0
    assert overview["pytorch-blog"]["last_fetch_status"] == "failed"
    html = client.get("/sources").text
    row = html.split('data-source="pytorch-blog"', 1)[1].split("</tr>", 1)[0]
    assert "✗" in row and "HTTP 404" in row
