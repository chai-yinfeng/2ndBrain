"""Web UI（FastAPI TestClient）、定时任务（只测 dry-run 与纯函数）的离线测试。"""

import re
import shutil
import time as time_mod
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, schedule
from secondbrain.cli import app as cli_app
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, PricingSchedule, SourceConfig
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.loader import load_profile, weak_prerequisites
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
TODAY = date(2026, 11, 4)
TITLES = [
    "Continuous batching and KV cache scheduling for LLM inference serving",
    "Understanding attention: an explainer on transformer kernels",
    "A survey of speculative decoding for LLM serving",
    "Quantization kernels on GPU with Triton",
    "Distributed consensus for inference schedulers",
    "Company news roundup",
]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 data 目录：fake 后端跑出一份 digest，profile 用测试 fixture。"""
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    shutil.copy(FIXTURES / "profile.md", tmp_path / "profile.md")
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="https://b", trust=5)])
    for n, title in enumerate(TITLES):
        body = f"{title}. " + "Details about the system. " * (30 + 30 * n)
        db.insert_item(
            conn,
            db.NewItem(
                source_id="blog",
                url=f"https://example.com/{n}",
                url_canonical=f"https://example.com/{n}",
                content_hash=content_hash(title, body),
                title=title,
                author=None,
                summary=title,
                content=body,
                extract_method="feed",
                published_at=NOW - timedelta(hours=n),
            ),
            NOW,
        )
    conn.commit()
    run_pipeline(
        conn,
        FilterSettings(),
        FakeBackend(),
        load_profile(tmp_path / "profile.md"),
        limit=None,
        now=NOW,
        today=TODAY,
    )
    conn.close()
    return tmp_path


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app())


def _first_item(kind: str = "must_read") -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT item_id FROM digests WHERE lane = ?", (kind,)).fetchone()[0]
    finally:
        conn.close()


# ---------- 页面 ----------


def test_index_shows_digest_with_budget(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert html.count("data-item-id=") == len(TITLES)
    m = re.search(r'data-budget="(\d+)/(\d+)"', html)
    assert m and int(m.group(1)) <= int(m.group(2)) == 105
    assert "必读" in html and "温故" in html and "意外" in html and "速览" in html
    assert "积压" in html
    # 页面只引用本地 HTMX，不依赖外部 CDN
    assert 'src="/static/htmx.min.js"' in html
    assert "unpkg" not in html and "cdn" not in html.lower()


def test_index_with_date(client: TestClient):
    assert "Digest 2026-11-04" in client.get("/?date=2026-11-04").text
    other = client.get("/?date=2020-01-01")
    assert other.status_code == 200 and "data-item-id" not in other.text
    assert client.get("/?date=yesterday").status_code == 400


def test_index_without_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    resp = TestClient(create_app()).get("/")
    assert resp.status_code == 200
    assert "还没有 digest" in resp.text


def test_item_detail(client: TestClient):
    item_id = _first_item()
    resp = client.get(f"/item/{item_id}")
    assert resp.status_code == 200
    assert "评分" in resp.text and "content_type" in resp.text
    assert "精读（L2）" in resp.text
    assert client.get("/item/999999").status_code == 404


def test_history(client: TestClient):
    resp = client.get("/history")
    assert resp.status_code == 200
    assert 'href="/?date=2026-11-04"' in resp.text


def test_static_htmx_is_vendored(client: TestClient):
    resp = client.get("/static/htmx.min.js")
    assert resp.status_code == 200
    assert 'version:"2.0.4"' in resp.text


# ---------- 反馈 ----------


def test_feedback_is_recorded_and_returns_partial(client: TestClient):
    item_id = _first_item()
    resp = client.post("/feedback", data={"item_id": item_id, "kind": "up", "note": " 好文 "})
    assert resp.status_code == 200
    assert f'id="feedback-{item_id}"' in resp.text
    assert 'class="fb active"' in resp.text
    assert "已记录：👍" in resp.text
    conn = db.connect()
    rows = db.feedback_for(conn, item_id)
    conn.close()
    assert [(r["kind"], r["note"]) for r in rows] == [("up", "好文")]


def test_up_and_down_are_exclusive(client: TestClient):
    item_id = _first_item()
    client.post("/feedback", data={"item_id": item_id, "kind": "up"})
    resp = client.post("/feedback", data={"item_id": item_id, "kind": "down"})
    conn = db.connect()
    kinds = db.feedback_kinds(conn, [item_id])[item_id]
    conn.close()
    assert kinds == {"down"}
    assert resp.text.count('class="fb active"') == 1


def test_read_feedback_marks_item_read(client: TestClient):
    item_id = _first_item()
    assert client.post("/feedback", data={"item_id": item_id, "kind": "read"}).status_code == 200
    conn = db.connect()
    read_at = conn.execute("SELECT read_at FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    conn.close()
    assert read_at


def test_feedback_validation(client: TestClient):
    item_id = _first_item()
    assert client.post("/feedback", data={"item_id": item_id, "kind": "love"}).status_code == 400
    assert client.post("/feedback", data={"item_id": "abc", "kind": "up"}).status_code == 400
    assert client.post("/feedback", data={"item_id": 999999, "kind": "up"}).status_code == 404
    assert client.post("/feedback", data={"kind": "up"}).status_code == 422
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    conn.close()


def test_feedback_for_item_not_in_digest(client: TestClient):
    conn = db.connect()
    item_id = conn.execute(
        "SELECT id FROM items WHERE id NOT IN (SELECT item_id FROM digests)"
    ).fetchone()
    conn.close()
    if item_id is None:  # 所有条目都进了 digest：直接插一条新条目
        conn = db.connect()
        db.insert_item(
            conn,
            db.NewItem("blog", "https://x", "https://x", "h", "X", None, None, "", "none", None),
            NOW,
        )
        conn.commit()
        item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()
        conn.close()
    assert (
        client.post("/feedback", data={"item_id": item_id[0], "kind": "later"}).status_code == 200
    )


def test_backlog_only_counts_unread_must_reads(data_dir: Path):
    conn = db.connect()
    must_reads = [
        r[0] for r in conn.execute("SELECT item_id FROM digests WHERE lane = 'must_read'")
    ]
    assert must_reads
    db.add_feedback(conn, must_reads[0], "read", None, NOW)
    later = (TODAY + timedelta(days=2)).isoformat()
    assert [i for i, _ in db.past_must_reads(conn, later)] == must_reads[1:]
    conn.close()


# ---------- 前置知识提示 ----------


def test_weak_prerequisites():
    profile = load_profile(FIXTURES / "profile.md")
    prereqs = [
        "KV cache",
        "Consensus protocols",
        "Compilers",
        "Linear algebra",
        "GPU kernels (CUDA)",
    ]
    # 想回炉 / 陌生 / 接触过 的领域命中；"精通"无法识别，不算不扎实；未出现的概念不提示
    assert weak_prerequisites(profile, prereqs) == [
        "KV cache",
        "Consensus protocols",
        "GPU kernels (CUDA)",
    ]


# ---------- 定时任务 ----------


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time_mod.tzset()
    yield
    monkeypatch.undo()
    time_mod.tzset()


def test_default_time_is_off_peak_in_new_york(new_york):
    checks = schedule.utc_checks(
        schedule.parse_time(schedule.DEFAULT_TIME), PricingSchedule(), 2026
    )
    assert [(c.label, c.utc.hour, c.peak) for c in checks] == [
        ("冬令时", 12, False),
        ("夏令时", 11, False),
    ]


def test_peak_time_is_detected(new_york):
    checks = schedule.utc_checks(time(3, 30), PricingSchedule(), 2026)
    assert [(c.utc.hour, c.peak) for c in checks] == [(8, True), (7, True)]


def test_build_plist():
    import plistlib

    data = plistlib.loads(
        schedule.build_plist(time(7, 5), Path("/proj"), Path("/proj/data/logs"), "/bin/uv")
    )
    assert data["Label"] == "com.secondbrain.run"
    assert data["ProgramArguments"] == ["/bin/uv", "run", "sb", "run"]
    assert data["WorkingDirectory"] == "/proj"
    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    assert data["StandardOutPath"] == "/proj/data/logs/run.log"


def test_schedule_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(schedule, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run 不应调用 launchctl")

    monkeypatch.setattr("secondbrain.cli.subprocess.run", forbidden)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["schedule", "install", "--dry-run", "--time", "07:30"])
    assert result.exit_code == 0, result.output
    assert "<key>Label</key>" in result.output
    assert "launchctl load -w" in result.output
    assert "UTC" in result.output
    result = runner.invoke(cli_app, ["schedule", "uninstall", "--dry-run"])
    assert result.exit_code == 0
    assert "launchctl unload -w" in result.output
    assert not (tmp_path / "LaunchAgents").exists()
    assert not (tmp_path / "data" / "logs").exists()


def test_schedule_rejects_bad_time():
    result = CliRunner().invoke(cli_app, ["schedule", "install", "--dry-run", "--time", "7am"])
    assert result.exit_code == 2


def test_example_settings_still_valid():
    # lane_budget_reserve 等新字段能被解析
    import yaml

    raw = yaml.safe_load((config.CONFIG_DIR / "settings.example.yaml").read_text(encoding="utf-8"))
    assert config.pipeline_settings(raw).lane_budget_reserve == {"refresher": 15, "serendipity": 10}
