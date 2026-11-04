"""fake 后端的离线全链路测试，以及 profile / L0 / L1 / L2 的辅助函数。"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES

from secondbrain import config, db
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, L0Settings, L2Settings, SourceConfig
from secondbrain.pipeline.l0_rules import l0_reason
from secondbrain.pipeline.l1_screen import interleave_by_source
from secondbrain.pipeline.l2_deep import build_l2_text, paper_sections
from secondbrain.pipeline.run import run_pipeline, system_prompt
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.loader import load_profile, parse_profile, render_mastery
from secondbrain.urls import content_hash

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
TODAY = date(2026, 11, 4)

PAPER = """Speculative Decoding at Scale
Abstract
We study speculative decoding for serving.
1 Introduction
Serving LLM inference is memory bound. Draft models help.
2 Method
Lots of method details that should be dropped.
2.1 Draft model
More details.
5 Conclusion
Speculative decoding improves throughput by 2x.
References
[1] Some paper.
"""


# ---------- profile ----------


def test_parse_profile_fixture():
    profile = load_profile(FIXTURES / "profile.md")
    assert [d.mastery for d in profile.domains] == ["想回炉", "陌生", "接触过", None]
    assert profile.domains[2].raw_mastery == "接触过（偶尔写）"
    assert profile.domains[0].priority == "最高"
    assert len(profile.warnings) == 1 and "精通" in profile.warnings[0]
    rendered = render_mastery(profile)
    assert "Inference serving（batching、KV cache）：想回炉；优先级 最高" in rendered
    assert "未识别（原文：精通）" in rendered


def test_profile_without_table_falls_back_to_full_text():
    profile = parse_profile("# 画像\n\n只写了一段话，没有表格。\n")
    assert not profile.parsed
    assert "未找到领域掌握度表" in profile.warnings[0]
    assert "未能解析掌握度表" in render_mastery(profile)


def test_example_profile_parses():
    assert load_profile(config.CONFIG_DIR / "profile.example.md").parsed


def test_ensure_profile_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    path, created = config.ensure_profile()
    assert created and path.read_text(encoding="utf-8").startswith("# 我的兴趣 Profile")
    path.write_text("custom", encoding="utf-8")
    assert config.ensure_profile() == (path, False)
    assert path.read_text(encoding="utf-8") == "custom"


def test_system_prompt_contains_profile_and_json():
    profile = load_profile(FIXTURES / "profile.md")
    for name in ("l1_system", "l2_system"):
        text = system_prompt(name, profile)
        assert "<profile>\n# 测试用画像（虚构）" in text
        assert "领域掌握度（解析结果）" in text
        assert "json" in text


# ---------- L0 / L1 / L2 辅助 ----------


def _row(**kw) -> dict:
    base = {
        "id": 1,
        "title": "Paged attention explained",
        "summary": "",
        "content": "x" * 1000,
        "content_len": 1000,
        "published_at": "2026-11-03T00:00:00Z",
        "fetched_at": "2026-11-04T00:00:00Z",
        "source_id": "a",
        "trust": 3,
        "url": "https://example.com/a",
        "extract_method": "feed",
        "duration_seconds": None,
    }
    return base | kw


def test_l0_rules():
    s = L0Settings(max_age_days=7, min_content_chars=300, exclude_keywords=["Funding Round"])
    assert l0_reason(_row(), s, NOW) is None
    assert l0_reason(_row(published_at="2026-10-01T00:00:00Z"), s, NOW) == "超出时间窗"
    assert l0_reason(_row(published_at=None), s, NOW) is None  # 退回 fetched_at
    assert l0_reason(_row(content_len=100), s, NOW) == "正文过短"
    assert l0_reason(_row(title="Acme closes funding round"), s, NOW) == "排除关键词：Funding Round"
    include = L0Settings(include_keywords=["kv cache"])
    assert l0_reason(_row(), include, NOW) == "未命中 include 关键词"
    assert l0_reason(_row(summary="About the KV cache"), include, NOW) is None


def test_interleave_by_source():
    rows = [
        _row(id=1, source_id="low", trust=2),
        _row(id=2, source_id="high", trust=5),
        _row(id=3, source_id="high", trust=5),
        _row(id=4, source_id="low", trust=2),
        _row(id=5, source_id="high", trust=5),
    ]
    assert [r["id"] for r in interleave_by_source(rows, None)] == [2, 1, 3, 4, 5]
    assert [r["id"] for r in interleave_by_source(rows, 3)] == [2, 1, 3]


def test_paper_sections_and_l2_text():
    sections = paper_sections(PAPER)
    assert sections["abstract"] == "We study speculative decoding for serving."
    assert "memory bound" in sections["introduction"]
    assert "2x" in sections["conclusion"]
    text = build_l2_text(PAPER, None, True, L2Settings())
    assert "[Introduction]" in text and "[Conclusion]" in text
    assert "method details" not in text and "Some paper" not in text
    # 找不到章节时取开头；非论文直接截断
    s = L2Settings(max_input_chars=1000)
    assert build_l2_text("no headings " * 200, None, True, s) == ("no headings " * 200)[:1000]
    assert build_l2_text(PAPER, None, False, s) == PAPER[:1000]


# ---------- 全链路（fake 后端） ----------

TITLES = [
    ("blog", "Continuous batching and KV cache scheduling for LLM inference serving"),
    ("blog", "Understanding attention: an explainer on transformer kernels"),
    ("blog", "A survey of speculative decoding for LLM serving"),
    ("blog", "Quantization kernels on GPU with Triton"),
    ("blog", "Distributed consensus for inference schedulers"),
    ("blog", "Company news roundup"),
    ("papers", "Expert parallel serving with CUDA kernels"),
    ("papers", "Agent harness evaluation"),
    ("essays", "On craftsmanship and tools"),
    ("essays", "Notes on writing"),
    ("blog", "Acme closes funding round"),
]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    db.upsert_sources(
        c,
        [
            SourceConfig(id="blog", type="rss", url="https://b", trust=5),
            SourceConfig(id="papers", type="arxiv", query="q", trust=4),
            SourceConfig(id="essays", type="rss", url="https://e", trust=3, role="expansion"),
        ],
    )
    for n, (source, title) in enumerate(TITLES):
        body = f"{title}. " + "This article discusses the topic in depth. " * (20 + 40 * n)
        item = db.NewItem(
            source_id=source,
            url=f"https://example.com/{n}",
            url_canonical=f"https://example.com/{n}",
            content_hash=content_hash(title, body),
            title=title,
            author=None,
            summary=title,
            content=body,
            extract_method="feed",
            published_at=NOW - timedelta(hours=n),
        )
        assert db.insert_item(c, item, NOW)
    c.commit()
    yield c
    c.close()


def _run(conn, fs=None, today=TODAY, limit=None, backend=None):
    return run_pipeline(
        conn,
        fs or FilterSettings.model_validate({"l0": {"exclude_keywords": ["funding round"]}}),
        backend or FakeBackend(),
        load_profile(FIXTURES / "profile.md"),
        limit=limit,
        now=NOW,
        today=today,
    )


def test_full_pipeline_with_fake_backend(conn):
    report = _run(conn)
    assert report.l0_rejected == {"排除关键词": 1}
    assert report.l1_assessed == len(TITLES) - 1
    assert report.l2_assessed == report.l2_selected > 0
    assert report.aborted is None and not report.errors
    assert report.cost_usd == 0  # fake 模型不在价格表中

    rows = db.digest_rows(conn, TODAY.isoformat())
    lanes = {r["lane"] for r in rows}
    assert "must_read" in lanes
    assert sum(r["est_minutes"] for r in rows if r["lane"] in TOP_LANES) <= 105
    assert len(rows) == len(TITLES) - 1
    statuses = {r[0] for r in conn.execute("SELECT status FROM items")}
    assert statuses <= {"tiered", "deferred", "l0_rejected"}

    calls = conn.execute("SELECT stage, COUNT(*) FROM llm_calls GROUP BY stage").fetchall()
    assert dict(map(tuple, calls)) == {"L1": 1, "L2": report.l2_assessed}  # 10 条正好一批

    # expansion 信源的条目不会进入必读 / 值得读 / 温故
    essays = conn.execute(
        "SELECT d.lane FROM digests d JOIN items i ON i.id = d.item_id WHERE i.source_id = 'essays'"
    ).fetchall()
    assert {r[0] for r in essays} <= {"serendipity", "fyi", "archived"}


def test_rerun_without_new_items_changes_nothing(conn):
    _run(conn)
    before = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    report = _run(conn)
    assert (report.l0_checked, report.l1_selected, report.l2_selected) == (0, 0, 0)
    assert report.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == before


def test_limit_controls_l1_candidates(conn):
    report = _run(conn, limit=3)
    assert report.l1_selected == 3
    remaining = conn.execute("SELECT COUNT(*) FROM items WHERE status = 'new'").fetchone()[0]
    assert remaining == len(TITLES) - 1 - 3


def test_backlog_is_demoted_after_max_days(conn):
    _run(conn)
    must_reads = conn.execute("SELECT COUNT(*) FROM digests WHERE lane = 'must_read'").fetchone()[0]
    assert must_reads > 0
    report = _run(conn, today=TODAY + timedelta(days=2))
    assert (report.backlog_kept, report.backlog_demoted) == (must_reads, 0)
    report = _run(conn, today=TODAY + timedelta(days=4))
    assert (report.backlog_kept, report.backlog_demoted) == (0, must_reads)
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE lane = 'must_read'").fetchone()[0] == 0


class PricedFake(FakeBackend):
    """模型名映射到真实价格表，用来测试费用上限。"""

    def model_name(self, tier):
        return {"cheap": "deepseek-flash", "strong": "deepseek-v4-pro"}[tier]


def test_cost_guard_stops_before_l2(conn):
    fs = FilterSettings.model_validate(
        {
            "run": {"max_cost_usd": 0.000001},
            "pricing": {
                "deepseek-v4-pro": {"input_cache_hit": 1, "input_cache_miss": 1, "output": 1}
            },
        }
    )
    report = _run(conn, fs=fs, backend=PricedFake())
    assert report.aborted and "max_cost_usd" in report.aborted
    assert conn.execute("SELECT COUNT(*) FROM assessments WHERE stage = 'L2'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    # 未分级的条目保留 l1 状态，下次 run 继续
    assert conn.execute("SELECT COUNT(*) FROM items WHERE status = 'l1'").fetchone()[0] > 0


class DroppingFake(FakeBackend):
    """第一次 L1 调用漏掉最后一个条目，模拟模型偶发漏条。"""

    def __init__(self):
        self.l1_calls = []

    def complete_json(self, messages, schema, tier):
        obj, usage = super().complete_json(messages, schema, tier)
        if tier == "cheap":
            self.l1_calls.append([i.id for i in obj.items])
            if len(self.l1_calls) == 1:
                obj.items.pop()
        return obj, usage


def test_l1_retries_items_the_model_skipped(conn):
    backend = DroppingFake()
    report = _run(conn, backend=backend)
    assert report.l1_failed == 0
    assert report.l1_assessed == len(TITLES) - 1
    assert len(backend.l1_calls) == 2
    assert len(backend.l1_calls[1]) == 1  # 第二次只补评漏掉的那一条
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = 'L1'").fetchone()[0] == 2
