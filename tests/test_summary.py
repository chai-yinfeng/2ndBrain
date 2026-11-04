"""M8：深度速读与补前置（ADR 0015）。fake 后端，全部离线。"""

import shutil
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.summary import estimate_summary_cost, plan_summary, summary_minutes
from secondbrain.pipeline.tiering import Placement
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app
from secondbrain.wiki.lint import lint
from secondbrain.wiki.store import Page, new_meta, open_wiki

runner = CliRunner()
NOW = datetime.now(UTC)
TODAY = datetime.now().date().isoformat()
PAPER = (
    "Abstract\nWe study KV cache offloading.\n1 Introduction\nServing needs memory. "
    + "Method details about hierarchical caching and prefetch. " * 40
    + "\n5 Conclusion\nIt works.\n"
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    shutil.copy(FIXTURES / "profile.md", tmp_path / "profile.md")
    conn = db.connect()
    db.upsert_sources(
        conn,
        [
            SourceConfig(id="arxiv-cs-dc", type="arxiv", query="q"),
            SourceConfig(id="yt", type="youtube", url="https://y"),
        ],
    )
    conn.close()
    return tmp_path


def add_item(
    title: str,
    content: str,
    *,
    source: str = "arxiv-cs-dc",
    method: str = "arxiv_html",
    url: str | None = None,
    l1: dict | None = None,
    l2: dict | None = None,
) -> int:
    conn = db.connect()
    url = url or f"https://arxiv.org/abs/{abs(hash(title)) % 10**8}"
    db.insert_item(
        conn,
        db.NewItem(
            source,
            url,
            url,
            content_hash(title, content),
            title,
            None,
            None,
            content,
            method,
            NOW - timedelta(hours=1),
        ),
        NOW,
    )
    item = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    if l1:
        db.add_assessment(conn, item, "L1", l1, NOW, "fake-cheap", 7.0)
    if l2:
        db.add_assessment(conn, item, "L2", l2, NOW, "fake-strong", 7.0)
    conn.commit()
    conn.close()
    return item


def l2_with(prereqs: list[str]) -> dict:
    return {
        "tldr": "t",
        "key_points": ["k"],
        "why_for_you": "w",
        "est_minutes": 90,
        "prerequisites": prereqs,
        "scores": {"relevance": 8, "novelty": 7, "quality": 8, "refresher_value": 3},
    }


def calls(stage: str) -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = ?", (stage,)).fetchone()[
            0
        ]
    finally:
        conn.close()


# ---------- 计划与预估 ----------


def test_plan_reads_full_text_not_paper_sections(env):
    fs = FilterSettings()
    item = add_item("Short paper", PAPER)
    conn = db.connect()
    row = db.item_detail(conn, item)
    plan = plan_summary(row, fs)
    assert plan.mode == "single" and plan.text_kind == "full" and plan.text == PAPER
    long_fs = FilterSettings(summary={"single_max_chars": 1000, "max_total_chars": 2000})
    plan = plan_summary(row, long_fs)
    assert plan.mode == "mapreduce" and sum(len(c) for c in plan.chunks) <= 2100
    conn.close()


def test_estimates_use_configured_models_and_prices():
    raw = yaml.safe_load((config.CONFIG_DIR / "settings.example.yaml").read_text(encoding="utf-8"))
    fs = config.pipeline_settings(raw)
    short, long = estimate_summary_cost(20_000, fs), estimate_summary_cost(300_000, fs)
    assert 0 < short < long < 0.5  # flash 价格：几分钱以内
    assert summary_minutes(20_000, fs) == 1 and summary_minutes(300_000, fs) > 1


# ---------- 深度速读 ----------


def test_cli_summary_is_cached_and_force_regenerates(env):
    item = add_item("Hierarchical KV cache offloading for LLM inference", PAPER)
    first = runner.invoke(cli_app, ["summary", str(item)])
    assert first.exit_code == 0, first.output
    for heading in (
        "问题与动机",
        "核心方法",
        "关键结果",
        "局限与争议",
        "与已有知识的关系",
        "值得精读的章节",
        "需要补的前置知识",
    ):
        assert f"## {heading}" in first.output
    assert calls("SUMMARY") == 1
    again = runner.invoke(cli_app, ["summary", str(item)])
    assert "已有深度速读" in again.output and "（缓存）" in again.output
    assert calls("SUMMARY") == 1
    forced = runner.invoke(cli_app, ["summary", str(item), "--force"])
    assert forced.exit_code == 0 and calls("SUMMARY") == 2
    assert runner.invoke(cli_app, ["summary", "99999"]).exit_code == 1


def test_long_content_uses_map_then_reduce(env, monkeypatch):
    monkeypatch.setattr(
        config,
        "pipeline_settings",
        lambda s, _orig=config.pipeline_settings: _orig(s).model_copy(
            update={
                "summary": FilterSettings().summary.model_copy(update={"single_max_chars": 1000})
            }
        ),
    )
    item = add_item("A long paper", PAPER * 15)  # 约 3.3 万字符 → 3 块
    result = runner.invoke(cli_app, ["summary", str(item)])
    assert result.exit_code == 0, result.output
    assert "分" in result.output and "块提要" in result.output
    assert calls("SUMMARY_MAP") > 1 and calls("SUMMARY") == 1


def test_video_summary_keeps_timestamps(env):
    transcript = "\n".join(f"[{m:02d}:00] Segment {m} explains KV cache tricks." for m in range(8))
    item = add_item(
        "A lecture",
        transcript,
        source="yt",
        method="transcript",
        url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    ops.deep_summary(item, lambda line: None)
    page = TestClient(create_app()).get(f"/item/{item}").text
    assert "[01:00] Segment 1" in page and "watch?v=abcdefghijk&amp;t=60" in page


def test_web_summary_task_and_item_page(env):
    item = add_item("Hierarchical KV cache offloading for LLM inference", PAPER)
    client = TestClient(create_app())
    before = client.get(f"/item/{item}").text
    assert 'id="summary"' in before and f'hx-post="/summary/{item}"' in before
    assert "（非高峰）" in before and "约几美分" not in before  # 费用按当前模型价格估算
    client.post(f"/summary/{item}")
    task = client.app.state.tasks.get(f"summary-{item}")
    task.thread.join(10)
    status = client.get(f"/tasks/summary-{item}").text
    assert 'data-task-status="done"' in status and f'href="/item/{item}#summary"' in status
    page = client.get(f"/item/{item}").text
    assert "问题与动机" in page and "值得精读的章节" in page and "重新生成" in page
    assert "hx-confirm=" in page  # 重新生成需要确认
    assert "SUMMARY" in client.get("/ops").text  # 用量里能看到新阶段
    assert client.post("/summary/99999").status_code == 404
    assert client.get("/tasks/summary-abc").status_code == 404


def test_deep_read_cards_offer_summary(env):
    item = add_item("Hierarchical KV cache offloading", PAPER)
    conn = db.connect()
    db.upsert_digest(conn, TODAY, Placement(item, "must_read", 1, 90, True, None, 15), NOW)
    conn.commit()
    conn.close()
    home = TestClient(create_app()).get("/").text
    assert f'hx-post="/summary/{item}"' in home and "速读约 15 分钟 · 全文约 90 分钟" in home
    ops.deep_summary(item, lambda line: None)
    home = TestClient(create_app()).get("/").text
    assert f'href="/item/{item}#summary"' in home and f'hx-post="/summary/{item}"' not in home


# ---------- 补前置 ----------


@pytest.fixture
def prereq_item(env):
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    meta = new_meta("KV cache", "concept", date.today(), mastery="想回炉")
    wiki.save_page(Page("kv-cache", meta, "# KV cache"))
    wiki.rebuild_index()
    add_item(
        "Understanding the Roofline model",
        "An explainer. " * 50,
        l1={
            "content_type": "explainer",
            "topics": ["roofline-model"],
            "relevance": 7,
            "novelty": 3,
            "quality": 8,
            "refresher_value": 8,
            "one_line_reason": "r",
        },
    )
    item = add_item("A GPU serving paper", PAPER, l2=l2_with(["KV cache", "Roofline model"]))
    conn = db.connect()
    summary = {
        "problem": "p",
        "prerequisites": [
            {"name": "roofline model", "why": "算强度"},
            {"name": "PagedAttention", "why": "分页"},
        ],
    }
    db.set_ai(conn, item, "summary", summary, "fake-strong", NOW)
    conn.commit()
    conn.close()
    return item


def test_prereq_merges_sources_links_wiki_and_caches(prereq_item):
    out = runner.invoke(cli_app, ["prereq", str(prereq_item)])
    assert out.exit_code == 0, out.output
    # 深度速读与 L2 的前置合并去重（roofline model ≡ Roofline model）
    assert out.output.count("## ") == 3
    assert "✓ wiki 已有：[[kv-cache]] KV cache（掌握度 想回炉）" in out.output
    assert "AI 讲解（AI 生成）：（fake）PagedAttention" in out.output
    assert "讲解类条目 #1 Understanding the Roofline model" in out.output
    assert "本次新生成 2 条讲解" in out.output and calls("PREREQ") == 1
    again = runner.invoke(cli_app, ["prereq", str(prereq_item)])
    assert "本次新生成 0 条讲解" in again.output and calls("PREREQ") == 1  # 缓存
    runner.invoke(cli_app, ["prereq", str(prereq_item), "--force"])
    assert calls("PREREQ") == 2


def test_save_prereq_as_concept_page(prereq_item, env):
    client = TestClient(create_app())
    early = client.post(f"/prereq/{prereq_item}/save", data={"name": "PagedAttention"}).text
    assert "请先点「补前置」" in early
    html = client.post(f"/prereq/{prereq_item}").text
    assert "AI 生成" in html and "存为概念页" in html and 'href="/wiki/page/kv-cache"' in html
    saved = client.post(f"/prereq/{prereq_item}/save", data={"name": "PagedAttention"}).text
    assert "已存为概念页" in saved and 'href="/wiki/page/pagedattention"' in saved
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    page = wiki.pages["pagedattention"]
    assert page.meta["mastery"] == "陌生" and page.meta["sources"] == [prereq_item]
    assert page.meta["interval_days"] == 3 and "AI 生成" in page.body
    assert "[[pagedattention]]" in wiki.index_path.read_text(encoding="utf-8")
    assert "] prereq | PagedAttention" in wiki.log_path.read_text(encoding="utf-8")
    assert lint(wiki).missing_fields == {}
    again = client.post(f"/prereq/{prereq_item}/save", data={"name": "PagedAttention"}).text
    assert "wiki 已有该页面" in again
    # 再次补前置：已存为页面的知识点改为链接
    assert 'href="/wiki/page/pagedattention"' in client.post(f"/prereq/{prereq_item}").text


def test_non_ascii_prereq_gets_hashed_slug(env):
    item = add_item("一篇中文文章", "正文。" * 100, l2=l2_with(["注意力机制"]))
    ops.prerequisites(item, lambda line: None)
    slug, created = ops.save_prereq_page(item, "注意力机制")
    assert created and slug.startswith("concept-")
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    assert wiki.find("注意力机制").slug == slug


def test_prereq_without_any_prerequisites(env):
    item = add_item("Nothing needed", "Body. " * 20, l2=l2_with([]))
    report = ops.prerequisites(item, lambda line: None)
    assert report.entries == [] and calls("PREREQ") == 0
    assert "没有前置知识" in TestClient(create_app()).post(f"/prereq/{item}").text


def test_summary_task_runs_one_per_item(env):
    item = add_item("Paper", PAPER)
    app = create_app()
    gate = threading.Event()
    app.state.tasks.start(f"summary-{item}", "深度速读", lambda t: gate.wait(5))
    resp = TestClient(app).post(f"/summary/{item}")
    assert "正在运行" in resp.text
    gate.set()
