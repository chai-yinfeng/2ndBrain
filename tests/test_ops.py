"""M7：运维（ADR 0013）——run 锁、后台任务、/ops、首页"编进 wiki"、/wiki 体检。全部离线。"""

import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.models import SourceConfig
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app
from secondbrain.web.tasks import TaskRegistry, TaskRunning

runner = CliRunner()
L2 = {
    "tldr": "PagedAttention 把 KV cache 分页管理。",
    "key_points": ["KV cache 按块分配"],
    "why_for_you": "r",
    "est_minutes": 10,
    "prerequisites": ["KV cache"],
    "scores": {"relevance": 8, "novelty": 6, "quality": 8, "refresher_value": 3},
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 data 与 config 目录；sources.yaml 为空列表（不发网络请求），fake LLM。"""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURES / "profile.md", data / "profile.md")
    conf = tmp_path / "config"
    conf.mkdir()
    for name in ("settings.example.yaml", "sources.example.yaml", "profile.example.md"):
        shutil.copy(config.CONFIG_DIR / name, conf / name)
    (conf / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setattr(config, "CONFIG_DIR", conf)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return data


def wait(task, timeout: float = 20) -> None:
    task.thread.join(timeout)
    assert not task.running, "后台任务没有在时限内结束"


# ---------- run 锁 ----------


def test_run_lock_is_exclusive(env):
    assert not ops.is_running()
    with ops.run_lock():
        assert ops.is_running()
        with pytest.raises(ops.RunLocked):
            with ops.run_lock():
                pass
    assert not ops.is_running()


def test_lock_is_released_when_holder_crashes(env):
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from secondbrain import ops\n"
        "with ops.run_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(60)\n"
    )
    path = env / "run.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert ops.is_running(path)
    finally:
        proc.kill()  # 模拟崩溃：不走 finally 释放
        proc.wait()
    assert not ops.is_running(path)


def test_cli_run_refuses_while_locked(env):
    with ops.run_lock():
        result = runner.invoke(cli_app, ["run", "--no-fetch"])
    assert result.exit_code == 1
    assert "已有 sb run 在运行" in result.output
    result = runner.invoke(cli_app, ["run"])  # 锁释放后正常运行（sources 为空，不发请求）
    assert result.exit_code == 0, result.output
    assert "抓取：新增 0 条" in result.output


# ---------- 后台任务 ----------


def test_registry_allows_one_task_per_kind():
    registry = TaskRegistry()
    gate = threading.Event()

    def slow(task):
        task.log("working")
        gate.wait(5)
        return {"ok": 1}

    task = registry.start("x", "慢任务", slow)
    with pytest.raises(TaskRunning):
        registry.start("x", "慢任务", slow)
    other = registry.start("y", "另一个", lambda t: None)  # 不同类型互不影响
    gate.set()
    wait(task)
    wait(other)
    assert (task.status, task.result, task.lines) == ("done", {"ok": 1}, ["working"])

    def boom(task):
        raise RuntimeError("出错了")

    failed = registry.start("x", "会失败", boom)
    wait(failed)
    assert (failed.status, failed.error) == ("failed", "出错了")


def test_web_run_task(env):
    client = TestClient(create_app())
    with ops.run_lock():
        refused = client.post("/tasks/run")
    assert "已有 sb run 在运行" in refused.text and "data-task-status" not in refused.text

    resp = client.post("/tasks/run")
    assert resp.status_code == 200 and 'id="task-run"' in resp.text
    wait(client.app.state.tasks.get("run"))
    status = client.get("/tasks/run").text
    assert 'data-task-status="done"' in status and "hx-trigger" not in status  # 结束后停止轮询
    assert "LLM 后端：fake" in status and "估算费用" in status
    assert client.get("/tasks/nope").status_code == 404


def test_running_task_fragment_polls(env):
    app = create_app()
    gate = threading.Event()
    task = app.state.tasks.start("run", "立即运行（与 sb run 相同）", lambda t: gate.wait(5))
    client = TestClient(app)
    html = client.get("/tasks/run").text
    assert 'hx-trigger="every 2s"' in html and "运行中" in html
    again = client.post("/tasks/run")
    assert "正在运行" in again.text
    gate.set()
    wait(task)


def _gained_item(n: int, title: str) -> int:
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="blog", type="rss", url="https://b")])
    body = f"{title}. " + "Paged KV cache details. " * 40
    db.insert_item(
        conn,
        db.NewItem(
            "blog",
            f"https://e.com/{n}",
            f"https://e.com/{n}",
            content_hash(title, body),
            title,
            None,
            title,
            body,
            "feed",
            datetime.now(UTC) - timedelta(hours=1),
        ),
        datetime.now(UTC),
    )
    item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    db.add_assessment(conn, item, "L2", L2, datetime.now(UTC), "fake-strong", 7.0)
    db.add_feedback(conn, item, "gained", None, datetime.now(UTC))
    conn.close()
    return item


def _seed_digest(item: int) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO digests (date, item_id, lane, rank, est_minutes, is_deep_read, created_at)"
        " VALUES (?, ?, 'must_read', 0, 10, 0, ?)",
        (datetime.now().date().isoformat(), item, db.format_time(datetime.now(UTC))),
    )
    conn.commit()
    conn.close()


def test_home_ingest_button_runs_in_background(env):
    item = _gained_item(1, "PagedAttention explained")
    _seed_digest(item)
    client = TestClient(create_app())
    home = client.get("/").text
    assert 'data-to-ingest="1"' in home and 'hx-post="/tasks/wiki_ingest"' in home
    assert "每条约 1 分钟" in home  # 按钮旁的说明
    client.post("/tasks/wiki_ingest")
    wait(client.app.state.tasks.get("wiki_ingest"))
    status = client.get("/tasks/wiki_ingest").text
    assert 'data-task-status="done"' in status
    assert '<a href="/wiki/page/pagedattention-explained">' in status
    assert '<a href="/wiki/page/kv-cache">' in status and "估算费用" in status
    assert 'data-to-ingest="0"' in client.get("/").text


# ---------- /ops ----------


def test_ops_page_usage_peak_and_tune(env, monkeypatch: pytest.MonkeyPatch):
    assert runner.invoke(cli_app, ["run", "--no-fetch"]).exit_code == 0
    conn = db.connect()
    sources = [SourceConfig(id="vllm-blog", type="rss", url="u", trust=4)]
    db.upsert_sources(conn, sources)
    for n in range(5):
        body = f"post {n} " * 30
        db.insert_item(
            conn,
            db.NewItem(
                "vllm-blog",
                f"https://v/{n}",
                f"https://v/{n}",
                content_hash(f"p{n}", body),
                f"p{n}",
                None,
                None,
                body,
                "feed",
                None,
            ),
            datetime.now(UTC),
        )
        item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
        db.add_feedback(conn, item, "gained", None, datetime.now(UTC))
    conn.close()
    (config.CONFIG_DIR / "sources.yaml").unlink()  # 退回 example（含 vllm-blog，trust 5）
    monkeypatch.setattr("secondbrain.web.app.is_peak", lambda *a: True)
    client = TestClient(create_app())
    html = client.get("/ops").text
    assert "今天" in html and "近 7 天" in html
    assert 'data-peak="true"' in html and "高峰时段" in html
    assert 'hx-post="/tasks/run"' in html and "与 <code>sb run</code> 相同" in html
    assert "vllm-blog" in html and 'href="/profile"' in html
    assert 'href="/ops"' in client.get("/").text

    # trust 已是 5：没有建议；把 example 中的 vllm-blog 改为 trust 3 后再看
    example = config.CONFIG_DIR / "sources.example.yaml"
    text = example.read_text(encoding="utf-8")
    example.write_text(text.replace("trust: 5", "trust: 3", 1), encoding="utf-8")
    html = client.get("/ops").text
    assert "3 → 4" in html and 'hx-post="/ops/tune"' in html
    result = client.post("/ops/tune").text
    assert "已更新" in result
    written = (config.CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")
    assert written.split("- id: vllm-blog", 1)[1].split("- id:", 1)[0].count("trust: 4") == 1


# ---------- /wiki 体检 ----------


def test_wiki_lint_button(env):
    from secondbrain.wiki.store import Page, new_meta, open_wiki

    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    wiki.save_page(Page("a", new_meta("A", "concept", datetime.now().date()), "# A\n\n[[ghost]]"))
    client = TestClient(create_app())
    page = client.get("/wiki").text
    assert 'hx-post="/wiki/lint"' in page and "不调用 LLM" in page
    result = client.post("/wiki/lint").text
    assert "断链（指向不存在的页面）" in result and "[[a]] → ghost" in result
