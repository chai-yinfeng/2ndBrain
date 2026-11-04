"""长内容 map-reduce、按需精读（sb deep / Web 精读按钮）。全部使用 fake 后端与临时目录。"""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db
from secondbrain.cli import app as cli_app
from secondbrain.extract.transcript import Segment, format_transcript
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, L2Settings, ModelPrice, PricingSchedule, SourceConfig
from secondbrain.pipeline.l2_deep import chunk_text, deep_read, estimate_deep_cost, plan_deep
from secondbrain.pipeline.run import run_pipeline, system_prompt
from secondbrain.profile.loader import load_profile
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PROFILE = load_profile(FIXTURES / "profile.md")


def video_text(minutes: int) -> str:
    segs = [
        Segment(i * 10, 10, f"In this part we discuss kv cache detail {i}.")
        for i in range(minutes * 6)
    ]
    return format_transcript(segs, 30)


def make_row(conn, n, title, content, url=None, method="feed", duration=None, source="blog"):
    db.insert_item(
        conn,
        db.NewItem(
            source_id=source,
            url=url or f"https://example.com/{n}",
            url_canonical=url or f"https://example.com/{n}",
            content_hash=content_hash(title, content or title),
            title=title,
            author=None,
            summary=title,
            content=content,
            extract_method=method,
            published_at=NOW - timedelta(hours=n),
            duration_seconds=duration,
        ),
        NOW,
    )
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    return db.item_detail(conn, item_id)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    db.upsert_sources(c, [SourceConfig(id="blog", type="rss", url="u", trust=5)])
    yield c
    c.close()


# ---------- 分块 ----------


def test_chunk_text_size_and_overlap():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(100))
    chunks = chunk_text(text, size=500, overlap=120)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) > 10
    # 相邻块有整行重叠
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.splitlines()[-1] == b.splitlines()[0] or a.splitlines()[-2] in b
    # 合起来覆盖全部行
    assert {ln for c in chunks for ln in c.splitlines()} == set(text.splitlines())
    # 单行超长时硬切
    assert all(len(c) <= 100 for c in chunk_text("y" * 450, size=100, overlap=0))


def test_plan_deep_single_vs_mapreduce(conn):
    s = L2Settings(mapreduce_threshold_chars=5000, chunk_chars=2000, max_chunks=3)
    short = make_row(conn, 1, "Short", "a" * 3000)
    assert (plan_deep(short, s).mode, plan_deep(short, s).text_kind) == ("single", "full")
    long = make_row(conn, 2, "Long", "\n".join("b" * 100 for _ in range(200)))
    plan = plan_deep(long, s)
    assert plan.mode == "mapreduce" and len(plan.chunks) == 3  # 受 max_chunks 限制
    truncated = make_row(conn, 3, "Mid", "c" * 4000)
    s2 = L2Settings(mapreduce_threshold_chars=5000, max_input_chars=1000)
    assert plan_deep(truncated, s2).text_kind == "truncated"


def test_mapreduce_video_has_timestamped_key_points(conn):
    row = make_row(
        conn,
        1,
        "3-hour lecture",
        video_text(180),
        url="https://www.youtube.com/watch?v=vid00000001",
        method="transcript",
        duration=10800,
    )
    s = L2Settings(mapreduce_threshold_chars=20_000, chunk_chars=8000)
    result = deep_read(FakeBackend(), system_prompt("l2_system", PROFILE), row, s)
    stages = [stage for stage, _ in result.calls]
    assert stages.count("L2_MAP") == len(plan_deep(row, s).chunks) > 1
    assert stages[-1] == "L2"
    assert result.response is not None
    assert all(p.startswith("[") for p in result.response.key_points)


def test_estimate_deep_cost(conn):
    pricing = {
        "cheap": ModelPrice(input_cache_hit=0, input_cache_miss=1, output=1),
        "strong": ModelPrice(input_cache_hit=0, input_cache_miss=10, output=10),
    }
    s = L2Settings(mapreduce_threshold_chars=20_000, chunk_chars=8000)
    sched = PricingSchedule()
    small = make_row(conn, 1, "small", "x" * 3000)
    big = make_row(conn, 2, "big", video_text(180), method="transcript")
    c_small = estimate_deep_cost(small, "sys", s, ("cheap", "strong"), pricing, sched, NOW)
    c_big = estimate_deep_cost(big, "sys", s, ("cheap", "strong"), pricing, sched, NOW)
    assert 0 < c_small < c_big


def test_run_pipeline_uses_mapreduce_for_long_items(conn):
    make_row(
        conn,
        1,
        "Long inference serving deep dive with kv cache",
        "\n".join(["kv cache serving " * 20] * 600),
    )
    fs = FilterSettings.model_validate(
        {"l2": {"mapreduce_threshold_chars": 50_000, "chunk_chars": 20_000}}
    )
    report = run_pipeline(
        conn, fs, FakeBackend(), PROFILE, limit=None, now=NOW, today=date(2026, 11, 4)
    )
    assert report.l2_assessed == 1
    stages = [r[0] for r in conn.execute("SELECT stage FROM llm_calls ORDER BY id")]
    assert "L2_MAP" in stages and stages[-1] == "L2"


def test_no_transcript_items_skip_l2(conn):
    make_row(
        conn,
        1,
        "Video about kv cache inference serving",
        "",
        url="https://www.youtube.com/watch?v=x",
        method="no_transcript",
    )
    report = run_pipeline(
        conn, FilterSettings(), FakeBackend(), PROFILE, limit=None, now=NOW, today=date(2026, 11, 4)
    )
    assert report.l1_assessed == 1 and report.l2_selected == 0
    lane = conn.execute("SELECT lane FROM digests").fetchone()[0]
    assert lane in ("fyi", "archived")


# ---------- sb deep / Web 精读 ----------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURES / "profile.md", data / "profile.md")
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    c = db.connect()
    db.upsert_sources(c, [SourceConfig(id="blog", type="rss", url="u", trust=5)])
    make_row(
        c,
        1,
        "Lecture on paged attention",
        video_text(20),
        url="https://www.youtube.com/watch?v=vid00000001",
        method="transcript",
        duration=1200,
    )
    make_row(
        c,
        2,
        "Silent video",
        "",
        url="https://www.youtube.com/watch?v=vid00000002",
        method="no_transcript",
    )
    c.commit()
    c.close()
    return data


runner = CliRunner()


def test_sb_deep_prints_timestamp_links(env):
    result = runner.invoke(cli_app, ["deep", "1"])
    assert result.exit_code == 0, result.output
    assert "TL;DR：（fake）" in result.output
    assert "https://www.youtube.com/watch?v=vid00000001&t=" in result.output
    assert "实际估算费用 $0.0000" in result.output
    again = runner.invoke(cli_app, ["deep", "1"])
    assert "已有精读结果" in again.output
    forced = runner.invoke(cli_app, ["deep", "1", "--force"])
    assert forced.exit_code == 0 and "已有精读结果" not in forced.output
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM assessments WHERE stage = 'L2'").fetchone()[0] == 2
    conn.close()


def test_sb_deep_errors(env):
    assert runner.invoke(cli_app, ["deep", "999"]).exit_code == 1
    result = runner.invoke(cli_app, ["deep", "2"])
    assert result.exit_code == 1
    assert "没有字幕" in result.output


def test_web_deep_button_and_endpoint(env):
    conn = db.connect()
    run_pipeline(
        conn,
        FilterSettings.model_validate({"l1": {"top_n": 0}}),
        FakeBackend(),
        PROFILE,
        limit=None,
        now=datetime.now(UTC),
        today=date.today(),
    )
    conn.close()
    client = TestClient(create_app())
    html = client.get("/").text
    assert 'hx-post="/deep"' in html and "将调用 strong 模型" in html
    assert "无字幕" in html
    resp = client.post("/deep", data={"item_id": "1"})
    assert resp.status_code == 200
    assert "TL;DR" in resp.text and "&amp;t=" in resp.text
    item = client.get("/item/1").text
    assert "watch?v=vid00000001&amp;t=" in item  # 详情页要点带时间戳链接
    assert client.post("/deep", data={"item_id": "999"}).status_code == 404
    assert "没有字幕" in client.post("/deep", data={"item_id": "2"}).text
    assert "将调用 strong 模型" in client.get("/profile").text
