"""M10（ADR 0017）：形式 / 类型筛选、信源主页、运维页设置（.env / 测试连接 / 模型）、
CSRF 与 Host 校验。全部离线；config 目录与 .env 由 conftest 指向临时路径。"""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import fixture_bytes
from fastapi.testclient import TestClient

from secondbrain import config, db
from secondbrain.ingest import fetch_sources
from secondbrain.llm.base import LLMError
from secondbrain.llm.deepseek import DeepSeekBackend
from secondbrain.models import LLMSettings, SourceConfig
from secondbrain.pipeline.tiering import Placement
from secondbrain.sources.homepage import safe_url, source_homepage
from secondbrain.sources.rss import parse_feed
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app, request_problem
from secondbrain.web.filters import chips, item_form

NOW = datetime.now(UTC)
TODAY = datetime.now().date().isoformat()
SECRET = "sk-test-secret-value-1234567890"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SB_LLM_BACKEND", raising=False)
    return tmp_path


# ---------- 形式 / 类型筛选 ----------


@pytest.mark.parametrize(
    ("source_type", "url", "method", "expected"),
    [
        ("arxiv", "https://arxiv.org/abs/2611.1", "arxiv_html", "paper"),
        ("hf_papers", "https://arxiv.org/abs/2611.2", "summary", "paper"),
        ("rss", "https://arxiv.org/abs/2611.3", "feed", "paper"),
        ("rss", "https://blog.example.com/paper.PDF", "pdf", "paper"),
        ("rss", "https://blog.example.com/post", "pdf", "paper"),
        ("youtube", "https://www.youtube.com/watch?v=x", "transcript", "video"),
        ("rss", "https://youtu.be/abc", "feed", "video"),
        ("rss", "https://blog.example.com/post", "html", "blog"),
        ("sitemap", "https://www.lmsys.org/blog/2026-11-02-x", "html", "blog"),
        (None, "", None, "blog"),
    ],
)
def test_item_form(source_type, url, method, expected):
    assert item_form(source_type, url, method) == expected


def test_chips_keep_other_dimensions():
    params = {"date": "2026-11-04", "form": "paper", "ctype": None}
    forms = chips("/", params, "form", {"paper": "论文", "blog": "博客"})
    assert forms[0] == ("全部", "/?date=2026-11-04", False)
    assert forms[1] == ("论文", "/?date=2026-11-04&form=paper", True)
    ctypes = chips("/", params, "ctype", {"survey": "综述"})
    assert ctypes[1][1] == "/?date=2026-11-04&form=paper&ctype=survey"


def _seed_mixed_digest():
    conn = db.connect()
    db.upsert_sources(
        conn,
        [
            SourceConfig(id="arxiv-x", type="arxiv", query="cat:cs.CL"),
            SourceConfig(id="blog", type="rss", url="https://b"),
            SourceConfig(id="yt", type="youtube", url="https://y"),
        ],
    )
    specs = [
        ("arxiv-x", "https://arxiv.org/abs/1", "A paper survey", "survey", "must_read"),
        ("blog", "https://b/1", "A blog explainer", "explainer", "must_read"),
        ("yt", "https://www.youtube.com/watch?v=v", "A lecture", "explainer", "worth_it"),
        ("blog", "https://b/2", "A quick note", "opinion", "fyi"),
    ]
    for n, (sid, url, title, ctype, lane) in enumerate(specs):
        body = f"{title} " * 50
        db.insert_item(
            conn,
            db.NewItem(
                sid, url, url, content_hash(title, body), title, None, None, body, "feed", NOW
            ),
            NOW,
        )
        item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
        l1 = {
            "relevance": 7,
            "novelty": 6,
            "quality": 7,
            "refresher_value": 3,
            "content_type": ctype,
            "topics": [],
            "one_line_reason": "r",
        }
        db.add_assessment(conn, item, "L1", l1, NOW, "fake-cheap", 7.0)
        db.upsert_digest(conn, TODAY, Placement(item, lane, n + 1, 10, False), NOW)
    conn.commit()
    conn.close()


def test_index_filters_only_affect_display(env):
    _seed_mixed_digest()
    client = TestClient(create_app())
    full = client.get("/").text
    assert full.count('class="entry lane-') == 3 and 'data-budget="30/105"' in full
    assert 'href="/?form=paper"' in full and 'href="/?ctype=explainer"' in full

    papers = client.get("/?form=paper").text
    assert "A paper survey" in papers and "A blog explainer" not in papers
    assert 'data-budget="30/105"' in papers  # 预算不受筛选影响
    assert '<a class="chip on" href="/?form=paper">论文</a>' in papers
    assert '<a class="chip" href="/">全部</a>' in papers  # "全部"清除该维度
    worth = papers.split('id="lane-worth_it"', 1)[1].split("</section>", 1)[0]
    assert "<small>0</small>" in worth and "（空）" in worth  # 被筛空的 lane 显示空状态

    combo = client.get("/?form=blog&ctype=explainer").text
    assert "A blog explainer" in combo and "A lecture" not in combo and "A quick note" not in combo
    fyi = combo.split('<details class="lane" id="lane-fyi">', 1)[1].split("</details>", 1)[0]
    assert "速览（0 条）" in fyi
    video = client.get("/?form=video&ctype=bogus").text  # 非法取值视为"全部"
    assert "A lecture" in video and "A paper survey" not in video


def test_history_filtered_counts(env):
    _seed_mixed_digest()
    client = TestClient(create_app())
    html = client.get("/history?ctype=explainer").text
    row = html.split(f'href="/?date={TODAY}&amp;ctype=explainer"', 1)[1].split("</tr>", 1)[0]
    cells = [c.split("</td>")[0] for c in row.split("<td>")[1:]]
    assert cells == ["1", "0", "0", "1", "0", "0", "2"]  # 必读 温故 意外 值得读 顺延 速览 合计
    assert 'class="chip on" href="/history?ctype=explainer"' in html
    assert f'href="/?date={TODAY}"' in client.get("/history").text


# ---------- 信源主页与"进入推荐" ----------


def test_source_homepage_rules():
    def src(**kw):
        return SourceConfig(**kw)

    assert source_homepage(src(id="a", type="rss", url="https://x/feed"), "https://x/") == (
        "https://x/"
    )
    # M11 修复：没有记录到 channel link 时兜底为 feed 的站点根；feed 托管域名不给链接
    assert source_homepage(src(id="a", type="rss", url="https://x/feed")) == "https://x/"
    assert source_homepage(src(id="a", type="rss", url="https://a.substack.com/feed")) == (
        "https://a.substack.com/"
    )
    for hosted in ("https://feeds.feedburner.com/x", "https://feeds.example.com/rss"):
        assert source_homepage(src(id="a", type="rss", url=hosted)) is None
    assert (
        source_homepage(src(id="a", type="rss", url="u", homepage="https://mine.example"), "x")
        == "https://mine.example"
    )
    assert source_homepage(src(id="a", type="rss", url="u"), "javascript:alert(1)") is None
    assert source_homepage(src(id="a", type="rss", url="u", homepage="javascript:x")) is None
    assert source_homepage(
        src(id="a", type="sitemap", url="https://www.lmsys.org/sitemap.xml")
    ) == ("https://www.lmsys.org/")
    assert source_homepage(src(id="a", type="arxiv", query="cat:cs.DC AND abs:LLM")) == (
        "https://arxiv.org/list/cs.DC/recent"
    )
    assert source_homepage(src(id="a", type="arxiv", query='abs:"kv cache"')).startswith(
        "https://arxiv.org/search/?query="
    )
    assert source_homepage(src(id="a", type="hf_papers", url="https://h")) == (
        "https://huggingface.co/papers"
    )
    yt = src(id="a", type="youtube", url="https://www.youtube.com/feeds/videos.xml?channel_id=UC1")
    assert source_homepage(yt) == "https://www.youtube.com/channel/UC1"
    assert safe_url("ftp://x/") is None and safe_url("https://") is None


def test_feed_homepage_is_detected_and_linked(env, make_client):
    assert parse_feed(fixture_bytes("rss.xml")).homepage
    feed = "https://blog.example.com/feed.xml"
    client, _ = make_client({feed: httpx.Response(200, content=fixture_bytes("rss.xml"))})
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    fetch_sources(
        conn, [SourceConfig(id="vllm-blog", type="rss", url=feed)], client, client.settings, now=NOW
    )
    detected = db.source_overview(conn)["vllm-blog"]["homepage"]
    conn.close()
    assert detected == parse_feed(fixture_bytes("rss.xml")).homepage
    html = TestClient(create_app()).get("/sources").text
    row = html.split('data-source="vllm-blog"', 1)[1].split("</tr>", 1)[0]
    assert f'<a href="{detected}" target="_blank" rel="noopener noreferrer"' in row
    lmsys = html.split('data-source="lmsys-sglang-blog"', 1)[1].split("</tr>", 1)[0]
    assert 'href="https://lmsys.org/"' in lmsys
    hazy = html.split('data-source="hazy-research"', 1)[1].split("</tr>", 1)[0]
    # 没抓取过的 rss 信源：兜底为 feed URL 的站点根（M11 修复）
    assert 'href="https://hazyresearch.stanford.edu/"' in hazy.split("</td>", 1)[0]
    assert "进入推荐" in html and "前排" not in html
    assert 'title="被分进 必读/温故/意外/值得读 的条数（不含速览、归档）"' in html


# ---------- settings 深合并 ----------


def test_settings_deep_merge(env):
    assert config.deep_merge({"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"c": [3]}, "e": 2}) == {
        "a": {"b": 1, "c": [3]},
        "d": 1,
        "e": 2,
    }
    before = config.load_settings()
    assert before["llm"]["models"]["cheap"] == "deepseek-flash"
    (config.config_dir() / "settings.yaml").write_text(
        "llm:\n  models:\n    strong: deepseek-v4-pro\ndaily_budget_minutes: 60\n"
        "pricing_schedule:\n  peak_hours_utc: [[0, 1]]\n",
        encoding="utf-8",
    )
    merged = config.load_settings()
    assert merged["llm"]["models"] == {"cheap": "deepseek-flash", "strong": "deepseek-v4-pro"}
    assert merged["llm"]["base_url"] == before["llm"]["base_url"]  # 其余键来自 example
    assert merged["daily_budget_minutes"] == 60
    assert merged["pricing_schedule"]["peak_hours_utc"] == [[0, 1]]  # list 整体替换
    assert merged["pricing_schedule"]["off_peak_multiplier"] == 0.5
    os.environ["SB_LLM_BACKEND"] = "fake"
    try:
        assert config.llm_backend(merged) == "fake"  # 环境变量优先级最高
    finally:
        del os.environ["SB_LLM_BACKEND"]


def test_config_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("SB_ENV_FILE", str(tmp_path / "x.env"))
    assert config.config_dir() == tmp_path / "c" and config.env_file() == tmp_path / "x.env"


# ---------- .env 写入 ----------


def test_set_env_var_preserves_other_lines(env):
    path = config.env_file()
    path.write_text(
        "# comment\nOTHER=1\nDEEPSEEK_API_KEY=old\nexport DEEPSEEK_API_KEY=dup\n", encoding="utf-8"
    )
    assert config.set_env_var("DEEPSEEK_API_KEY", SECRET) is True
    assert path.read_text(encoding="utf-8") == (f"# comment\nOTHER=1\nDEEPSEEK_API_KEY={SECRET}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        os.environ["DEEPSEEK_API_KEY"] == SECRET and config.env_source("DEEPSEEK_API_KEY") == ".env"
    )
    assert config.set_env_var("NEW_KEY", "v") is True
    assert path.read_text(encoding="utf-8").endswith("NEW_KEY=v\n")
    config.set_env_var("DEEPSEEK_API_KEY", None)
    assert "DEEPSEEK_API_KEY" not in path.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" not in os.environ
    with pytest.raises(config.ConfigError):
        config.set_env_var("X", "a\nb")
    os.environ.pop("NEW_KEY", None)


def test_shell_env_wins_over_dotenv(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")
    assert config.set_env_var("DEEPSEEK_API_KEY", SECRET) is False
    assert os.environ["DEEPSEEK_API_KEY"] == "from-shell"
    assert config.env_source("DEEPSEEK_API_KEY") == "环境变量"


# ---------- 运维页"设置" ----------


def test_ops_key_settings_never_echo_value(env):
    client = TestClient(create_app())
    page = client.get("/ops").text
    assert "DEEPSEEK_API_KEY：<b>未设置</b>" in page and 'type="password"' in page
    assert "没有输入" in client.post("/ops/settings/key", data={"value": "  "}).text
    assert not config.env_file().exists()
    resp = client.post("/ops/settings/key", data={"value": SECRET})
    assert "已设置" in resp.text and SECRET not in resp.text
    page = client.get("/ops").text
    assert "<b>已设置</b>（来源：.env）" in page and SECRET not in page
    assert str(len(SECRET)) not in page.split('id="settings"', 1)[1].split("</section>", 1)[0]
    assert config.parse_env_file(config.env_file())["DEEPSEEK_API_KEY"] == SECRET
    assert "已从 .env 删除" in client.post("/ops/settings/key/clear").text
    assert "<b>未设置</b>" in client.get("/ops").text


def test_ops_key_shell_override_note(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")
    client = TestClient(create_app())
    page = client.get("/ops").text
    assert "来源：环境变量" in page and "环境变量优先" in page and "from-shell" not in page
    resp = client.post("/ops/settings/key", data={"value": SECRET}).text
    assert "需要重启服务或 unset" in resp and SECRET not in resp


def test_ping_fake_and_missing_key(env, monkeypatch):
    client = TestClient(create_app())
    missing = client.post("/ops/settings/ping").text
    assert "连接失败" in missing and "DEEPSEEK_API_KEY 未设置" in missing
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    ok = client.post("/ops/settings/ping").text
    assert "✓ 连接正常：fake / fake-cheap" in ok and " ms" in ok
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = 'PING'").fetchone()[0] == 1
    conn.close()


def test_deepseek_ping_error_hides_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}})

    backend = DeepSeekBackend(
        LLMSettings(max_retries=0),
        SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LLMError) as exc:
        backend.ping()
    assert str(exc.value) == "AuthenticationError（HTTP 401）"

    def ok(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert '"max_tokens":1' in body.replace(" ", "")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "p"},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    backend = DeepSeekBackend(
        LLMSettings(), SECRET, http_client=httpx.Client(transport=httpx.MockTransport(ok))
    )
    usage = backend.ping()
    assert (usage.model, usage.completion_tokens) == ("deepseek-flash", 1)


def test_ops_models_save_only_changed_keys(env, monkeypatch):
    client = TestClient(create_app())
    page = client.get("/ops").text
    assert '<option value="deepseek-v4-pro">' in page and '<option value="fake">' in page
    same = {"cheap": "deepseek-flash", "strong": "deepseek-flash", "backend": "deepseek"}
    client.post("/ops/settings/models", data=same)
    assert not (config.config_dir() / "settings.yaml").exists()  # 与 example 相同：不建文件
    resp = client.post("/ops/settings/models", data={**same, "strong": "deepseek-v4-pro"}).text
    assert "已保存" in resp
    path = config.config_dir() / "settings.yaml"
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "llm": {"models": {"strong": "deepseek-v4-pro"}}
    }
    fs = config.pipeline_settings(config.load_settings())
    assert fs.llm.models == {"cheap": "deepseek-flash", "strong": "deepseek-v4-pro"}  # 立即生效
    assert "未知模型" in client.post("/ops/settings/models", data={**same, "cheap": "gpt"}).text
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    note = client.post("/ops/settings/models", data={**same, "backend": "fake"}).text
    assert "SB_LLM_BACKEND 已设置" in note
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["llm"]["backend"] == "fake"
    assert "已恢复默认" in client.post("/ops/settings/models/reset").text
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {}
    assert config.load_settings()["llm"]["models"]["strong"] == "deepseek-flash"


def test_models_save_while_running_notes_next_run(env):
    from secondbrain import ops

    client = TestClient(create_app())
    with ops.run_lock():
        resp = client.post(
            "/ops/settings/models",
            data={"cheap": "deepseek-flash", "strong": "deepseek-v4-pro", "backend": "deepseek"},
        ).text
    assert "已保存" in resp and "下次运行生效" in resp


# ---------- CSRF 与 Host ----------


def test_request_problem_rules():
    allowed = frozenset({"127.0.0.1", "localhost", "::1"})
    host = "127.0.0.1:8765"
    assert request_problem("POST", host, "http://127.0.0.1:8765", None, allowed) is None
    assert request_problem("POST", host, "http://evil.example", None, allowed)
    assert request_problem("POST", host, "null", None, allowed)
    assert request_problem("POST", host, None, "http://127.0.0.1:8765/ops", allowed) is None
    assert request_problem("POST", host, None, "http://localhost:8765/ops", allowed)  # 不同源
    assert request_problem("POST", host, None, None, allowed) is None  # 非浏览器客户端
    assert request_problem("GET", "evil.example:8765", None, None, allowed)  # DNS rebinding
    assert request_problem("GET", "[::1]:8765", None, None, allowed) is None
    assert request_problem("GET", "LOCALHOST:8000", None, None, allowed) is None
    assert request_problem("GET", None, None, None, allowed)


def test_csrf_middleware(env):
    _seed_mixed_digest()
    client = TestClient(create_app())
    item = 1
    cross = client.post(
        "/feedback",
        data={"item_id": item, "kind": "later"},
        headers={"Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    same = client.post(
        "/feedback",
        data={"item_id": item, "kind": "later"},
        headers={"Origin": "http://testserver"},
    )
    assert same.status_code == 200
    assert client.post("/feedback", data={"item_id": item, "kind": "later"}).status_code == 200
    bad_ref = client.post("/ops/settings/ping", headers={"Referer": "http://evil.example/x"})
    assert bad_ref.status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/ops/settings/ping", headers={"Host": "evil.example"}).status_code == 403
    # sb serve --host 0.0.0.0 时额外允许该值
    lan = TestClient(create_app(extra_hosts=("192.168.1.5",)))
    assert lan.get("/", headers={"Host": "192.168.1.5:8000"}).status_code == 200
