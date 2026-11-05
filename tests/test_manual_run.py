"""M9：手动运行与信源产出率（ADR 0016）——不限时间窗、首页"开始今日过滤"、运行方式开关、
速览 / 归档折叠、低产出信源。全部离线；不碰真实的 LaunchAgents 与 launchctl。"""

import plistlib
import shutil
import subprocess
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops, schedule
from secondbrain.cli import app as cli_app
from secondbrain.ingest import fetch_sources
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.run import run_pipeline
from secondbrain.pipeline.tiering import Placement
from secondbrain.profile.loader import parse_profile
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

runner = CliRunner()
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
FEED = "https://blog.example.com/feed.xml"
OLD_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Fresh post</title><link>https://blog.example.com/fresh</link>
<pubDate>Wed, 04 Nov 2026 08:00:00 +0000</pubDate><description>fresh</description></item>
<item><title>Old classic one</title><link>https://blog.example.com/old1</link>
<pubDate>Mon, 01 Jan 2024 08:00:00 +0000</pubDate><description>old</description></item>
<item><title>Old classic two</title><link>https://blog.example.com/old2</link>
<pubDate>Sun, 01 Jan 2023 08:00:00 +0000</pubDate><description>old</description></item>
</channel></rss>"""


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
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    return data


def add_item(conn, source: str, n: int, title: str, fetched: datetime, published=None) -> int:
    body = f"{title}. " + "Inference serving and kv cache details. " * 30
    db.insert_item(
        conn,
        db.NewItem(
            source,
            f"https://e.com/{source}/{n}",
            f"https://e.com/{source}/{n}",
            content_hash(title, body),
            title,
            None,
            title,
            body,
            "feed",
            published,
        ),
        fetched,
    )
    return conn.execute("SELECT MAX(id) FROM items").fetchone()[0]


# ---------- 不限时间窗 ----------


def test_all_time_fetch_keeps_old_entries_but_respects_limit(env, make_client):
    src = SourceConfig(id="blog", type="rss", url=FEED, max_age_days=7)
    client, _ = make_client({FEED: httpx.Response(200, content=OLD_FEED)})
    conn = db.connect()
    (normal,) = fetch_sources(conn, [src], client, client.settings, limit=2, now=NOW)
    assert normal.added == 1  # 只有时间窗内的 Fresh post
    (wide,) = fetch_sources(conn, [src], client, client.settings, limit=2, now=NOW, all_time=True)
    assert (wide.added, wide.duplicates) == (1, 1)  # 最新 2 条：Fresh（重复）+ Old classic one
    titles = {r[0] for r in conn.execute("SELECT title FROM items")}
    assert titles == {"Fresh post", "Old classic one"}
    assert conn.execute("SELECT SUM(is_backfill) FROM items").fetchone()[0] == 0
    conn.close()


def test_all_time_l0_ignores_age_for_new_items(env):
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="u")])
    add_item(conn, "blog", 1, "Old inference serving post", NOW, NOW - timedelta(days=400))
    conn.commit()
    report = run_pipeline(
        conn,
        FilterSettings(),
        FakeBackend(),
        parse_profile(""),
        limit=None,
        now=NOW,
        today=date(2026, 11, 5),
    )
    assert report.l0_rejected["超出时间窗"] == 1
    conn.execute("UPDATE items SET status = 'new'")
    report = run_pipeline(
        conn,
        FilterSettings(),
        FakeBackend(),
        parse_profile(""),
        limit=None,
        now=NOW,
        today=date(2026, 11, 5),
        all_time=True,
    )
    assert sum(report.l0_rejected.values()) == 0 and report.l1_assessed == 1
    conn.close()


def test_cli_run_all_time(env):
    (config.CONFIG_DIR / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    result = runner.invoke(cli_app, ["run", "--all-time"])
    assert result.exit_code == 0, result.output
    assert "不限时间窗：只去重" in result.output
    assert "不限时间窗" not in runner.invoke(cli_app, ["run", "--no-fetch"]).output


# ---------- 首页"开始今日过滤" ----------


def seed_digest(day: str) -> int:
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="u")])
    item = add_item(conn, "blog", 1, "A post", NOW, NOW)
    archived = add_item(conn, "blog", 2, "Archived post", NOW, NOW)
    db.upsert_digest(conn, day, Placement(item, "fyi", 1, 10, False), NOW)
    db.upsert_digest(conn, day, Placement(archived, "archived", 1, 10, False), NOW)
    conn.commit()
    conn.close()
    return item


def test_home_start_button_until_today_has_run(env):
    client = TestClient(create_app())
    home = client.get("/").text
    assert 'data-run-state="todo"' in home and "开始今日过滤" in home
    assert 'name="all_time"' in home and 'name="refresh" value="1"' in home
    assert "（非高峰）" in home  # 费用区间
    seed_digest((datetime.now().date() - timedelta(days=1)).isoformat())
    assert "开始今日过滤" in client.get("/").text  # 只有昨天的 digest
    seed_digest(datetime.now().date().isoformat())
    home = client.get("/").text
    assert 'data-run-state="done"' in home and "开始今日过滤" not in home
    assert '<a href="/ops">再运行一次</a>' in home


def test_home_shows_running_when_locked_or_task_running(env):
    client = TestClient(create_app())
    with ops.run_lock():
        home = client.get("/").text
        assert 'data-run-state="running"' in home and "开始今日过滤" not in home
        refused = client.post("/tasks/run", data={"refresh": "1"}).text
        assert "已有 sb run 在运行" in refused
    gate = threading.Event()
    task = client.app.state.tasks.start(
        "run", "立即运行", lambda t: {"cost": 0.0, "waited": gate.wait(5)}
    )
    home = client.get("/").text
    assert 'hx-get="/tasks/run?refresh=1"' in home and 'data-run-state="running"' in home
    gate.set()
    task.thread.join(5)
    done = client.get("/tasks/run?refresh=1")
    assert done.headers.get("HX-Refresh") == "true"  # 完成后整页刷新
    assert "HX-Refresh" not in client.get("/tasks/run").headers


def test_web_run_all_time_option(env):
    (config.CONFIG_DIR / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    client = TestClient(create_app())
    client.post("/tasks/run", data={"all_time": "1", "refresh": "1"})
    task = client.app.state.tasks.get("run")
    task.thread.join(20)
    assert task.status == "done" and "不限时间窗" in task.title
    assert any("不限时间窗：只去重" in line for line in task.lines)
    assert 'name="all_time"' in client.get("/ops").text


def test_fyi_and_archived_are_folded_with_working_feedback(env):
    item = seed_digest(datetime.now().date().isoformat())
    client = TestClient(create_app())
    html = client.get("/").text
    fyi = html.split('<details class="lane" id="lane-fyi">', 1)[1].split("</details>", 1)[0]
    assert "<summary><h2>速览（1 条）</h2></summary>" in fyi
    assert f'id="feedback-{item}"' in fyi and 'hx-post="/feedback"' in fyi
    archived = html.split('<details class="lane" id="lane-archived">', 1)[1].split("</details>")[0]
    assert "归档（1 条）" in archived and "Archived post" in archived
    assert "<details open" not in html  # 默认折叠
    resp = client.post("/feedback", data={"item_id": item, "kind": "dismiss"})
    assert resp.status_code == 200 and "已记录：不感兴趣" in resp.text


# ---------- 运行方式开关 ----------


class FakeLaunchctl:
    """记录 launchctl 调用；`list` 在 plist 已加载时返回 0。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[1] == "load":
            self.loaded = True
        elif args[1] == "unload":
            self.loaded = False
        code = 0 if args[1] != "list" or self.loaded else 113
        return subprocess.CompletedProcess(args, code)


def test_ops_schedule_toggle(env, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(schedule.subprocess, "run", fake)
    agents = Path(schedule.launch_agents_dir())
    assert "LaunchAgents" in str(agents) and str(Path.home()) not in str(agents)
    client = TestClient(create_app())
    page = client.get("/ops").text
    assert 'data-mode="manual"' in page and "开启每日自动" in page and "hx-confirm=" in page
    assert "冬令时：本地 07:00" in page
    assert fake.calls == []  # 未安装时不调用 launchctl

    result = client.post("/ops/schedule/install", data={"at": "08:30"}).text
    assert "已开启每日自动：每天本地 08:30" in result and "UTC" in result
    plist = plistlib.loads((agents / f"{schedule.LABEL}.plist").read_bytes())
    assert plist["StartCalendarInterval"] == {"Hour": 8, "Minute": 30}
    page = client.get("/ops").text
    assert 'data-mode="auto"' in page and "每天 08:30 自动运行" in page and "已加载" in page
    assert "改为手动" in page

    with ops.run_lock():
        refused = client.post("/ops/schedule/uninstall").text
    assert "当前有运行在进行" in refused and (agents / f"{schedule.LABEL}.plist").exists()
    done = client.post("/ops/schedule/uninstall").text
    assert "已改为手动" in done and not (agents / f"{schedule.LABEL}.plist").exists()
    assert [c[1] for c in fake.calls] == ["load", "list", "unload"]
    assert "当前已经是手动模式" in client.post("/ops/schedule/uninstall").text
    assert "格式应为 HH:MM" in client.post("/ops/schedule/install", data={"at": "7am"}).text


# ---------- 信源产出率 ----------


def test_low_yield_sources(env):
    conn = db.connect()
    now = datetime.now(UTC)
    sources = config.load_sources()
    db.upsert_sources(conn, sources)
    old = now - timedelta(days=60)
    for sid in ("vllm-blog", "pytorch-blog"):
        add_item(conn, sid, 0, f"{sid} old", old, old)  # 历史覆盖整个窗口
    for n in range(1, 7):
        add_item(conn, "vllm-blog", n, f"vllm recent {n}", now - timedelta(days=2), now)
        item = add_item(conn, "pytorch-blog", n, f"pytorch recent {n}", now, now)
        if n == 1:
            db.upsert_digest(
                conn, now.date().isoformat(), Placement(item, "must_read", 1, 10, False), now
            )
            db.add_feedback(conn, item, "gained", None, now)
    for n in range(1, 7):  # 新加入的信源：历史不足 30 天，不标记
        add_item(conn, "tri-dao", n, f"tri dao {n}", now, now)
    conn.commit()
    conn.close()

    html = TestClient(create_app()).get("/sources").text

    def row(sid: str) -> str:
        return html.split(f'data-source="{sid}"', 1)[1].split("</tr>", 1)[0]

    assert "低产出，建议停用" in row("vllm-blog") and 'data-yield="6/0/0/0/0"' in row("vllm-blog")
    assert "低产出" not in row("pytorch-blog") and 'data-yield="6/0/1/1/0"' in row("pytorch-blog")
    assert "低产出" not in row("tri-dao")
    assert 'action="/sources/vllm-blog/toggle"' in row("vllm-blog")

    out = runner.invoke(cli_app, ["sources", "tune"]).output
    assert "低产出信源（近 30 天入库 ≥ 5 条、进入前排 0 条）：1 个" in out
    assert "vllm-blog：入库 6，精读 0，前排 0 —— 建议停用" in out
    assert not (config.CONFIG_DIR / "sources.yaml").exists()  # tune 只读
