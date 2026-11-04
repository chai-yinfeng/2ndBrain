"""M6 知识 wiki：初始化、ingest、slug 与路径安全、lint、SM-2 复习、闭环、Web。全部离线。"""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db
from secondbrain.cli import app as cli_app
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import PageCreate, PageUpdate, WikiPlan
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.loader import load_profile
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app
from secondbrain.wiki.ingest import apply_plan
from secondbrain.wiki.lint import lint
from secondbrain.wiki.review import due_concepts, review, schedule
from secondbrain.wiki.store import Page, WikiError, new_meta, open_wiki, slugify

TODAY = date(2026, 11, 4)
PROFILE = load_profile(FIXTURES / "profile.md")
runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURES / "profile.md", data / "profile.md")
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    return data


def add_item(conn, n, title, *, l2=None, content=None, when=None):
    when = when or datetime.now(UTC)
    body = content or f"{title}. " + "Explaining the system in depth. " * 40
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
            published_at=when,
        ),
        when,
    )
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    if l2:
        db.add_assessment(conn, item_id, "L2", l2, when, "fake-strong", 8.0)
    return item_id


L2 = {
    "tldr": "PagedAttention 把 KV cache 分块管理。",
    "key_points": ["按块分配 KV cache", "prefix 共享"],
    "why_for_you": "w",
    "est_minutes": 20,
    "prerequisites": ["KV cache", "Continuous batching"],
    "scores": {"relevance": 8, "novelty": 7, "quality": 8, "refresher_value": 3},
}


@pytest.fixture
def conn(env):
    c = db.connect()
    db.upsert_sources(c, [SourceConfig(id="blog", type="rss", url="u", trust=4)])
    yield c
    c.close()


def _wiki(env):
    return open_wiki(env / "wiki", env / "raw")


# ---------- 存储与安全 ----------


def test_open_wiki_initializes_skeleton(env):
    wiki = _wiki(env)
    for folder in ("concepts", "entities", "papers"):
        assert (env / "wiki" / "pages" / folder).is_dir()
    assert "## Concepts" in wiki.index_path.read_text(encoding="utf-8")
    assert wiki.log_path.read_text(encoding="utf-8").startswith("# Wiki Log")


def test_slugify_and_path_safety(env):
    assert slugify("Paged Attention (vLLM)") == "paged-attention-vllm"
    assert slugify("../../etc/passwd") == "etc-passwd"
    with pytest.raises(WikiError):
        slugify("中文标题")
    wiki = _wiki(env)
    for bad in ("../evil", "a/b", "UPPER", ""):
        with pytest.raises(WikiError):
            wiki.page_path(bad, "concept")
    with pytest.raises(WikiError):
        wiki.page_path("ok", "secret")


def test_plan_cannot_escape_wiki_dir(env, conn):
    item = add_item(conn, 1, "Traversal attempt", l2=L2)
    row = db.item_detail(conn, item)
    wiki = _wiki(env)
    plan = WikiPlan(
        creates=[
            PageCreate(
                slug="../../../outside", type="concept", title="Evil", summary="s", body="x"
            ),
            PageCreate(slug="中文", type="concept", title="中文", summary="s", body="x"),
        ],
        updates=[PageUpdate(slug="no-such-page", action="add_links", links=["a"])],
    )
    result = apply_plan(wiki, plan, row, PROFILE, TODAY)
    assert result.created == ["outside"]  # 规范化为合法 slug，写在 wiki 内
    assert (env / "wiki" / "pages" / "concepts" / "outside.md").is_file()
    assert not (env.parent / "outside.md").exists()
    assert len(result.rejected) == 2
    files = [p for p in env.rglob("*.md")]
    assert all(p.is_relative_to(env) for p in files)


# ---------- ingest ----------


def test_ingest_pending_creates_pages_index_log(env, conn):
    item = add_item(conn, 1, "PagedAttention explained", l2=L2)
    db.add_feedback(conn, item, "learned", None, datetime.now(UTC))
    conn.commit()
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0, result.output
    assert "新建：pagedattention-explained, kv-cache, continuous-batching" in result.output

    wiki = _wiki(env)
    paper = wiki.pages["pagedattention-explained"]
    assert paper.type == "paper" and paper.meta["sources"] == [item]
    assert "[[kv-cache]]" in paper.body
    assert f"(../../../raw/{item}.md)" in paper.body  # 链接回原文
    assert (env / "raw" / f"{item}.md").read_text(encoding="utf-8").startswith("# PagedAttention")
    kv = wiki.pages["kv-cache"]
    assert kv.meta["mastery"] == "想回炉"  # profile 中 "Inference serving（batching、KV cache）"
    assert wiki.pages["continuous-batching"].meta["mastery"] == "接触过"  # 匹配不到时的默认值
    index = wiki.index_path.read_text(encoding="utf-8")
    assert "- [[kv-cache]] — （fake）KV cache" in index
    log = wiki.log_path.read_text(encoding="utf-8")
    assert "] ingest | PagedAttention explained" in log
    assert "- 新建 [[kv-cache]]" in log

    # 幂等：再次 --pending 没有待编入条目；显式指定也提示已编入
    again = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert "0 条待 ingest" in again.output
    explicit = runner.invoke(cli_app, ["wiki", "ingest", str(item)])
    assert "已编入 wiki" in explicit.output
    assert len(_wiki(env).pages) == 3


def test_second_item_updates_existing_concepts(env, conn):
    first = add_item(conn, 1, "PagedAttention explained", l2=L2)
    second = add_item(conn, 2, "KV cache quantization", l2={**L2, "prerequisites": ["KV cache"]})
    for item in (first, second):
        db.add_feedback(conn, item, "learned", None, datetime.now(UTC))
    conn.commit()
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0, result.output
    kv = _wiki(env).pages["kv-cache"]
    assert sorted(kv.meta["sources"]) == [first, second]
    assert "[[kv-cache-quantization]]" in kv.body  # 第二条通过 add_links 更新
    assert db.ingested_ids(conn) == {first, second}


def test_ingest_without_pending_is_reported(env):
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0
    assert "0 条待 ingest" in result.output
    assert (env / "wiki" / "index.md").is_file()


def test_long_items_send_key_points_not_full_text(env, conn):
    from secondbrain.wiki.ingest import item_payload

    item = add_item(conn, 1, "Very long", l2=L2, content="x" * 50_000)
    payload = item_payload(db.item_detail(conn, item), L2, FilterSettings())
    assert "text" not in payload and payload["key_points"] == L2["key_points"]
    short = add_item(conn, 2, "Short one", l2=L2, content="short body " * 10)
    assert "text" in item_payload(db.item_detail(conn, short), L2, FilterSettings())


# ---------- lint ----------


def test_lint_finds_broken_links_orphans_and_missing_fields(env):
    wiki = _wiki(env)
    wiki.save_page(Page("a", new_meta("A", "concept", TODAY), "# A\n\n见 [[b]] 与 [[ghost]]"))
    wiki.save_page(Page("b", new_meta("B", "concept", TODAY), "# B"))
    wiki.save_page(Page("lonely", {"title": "Lonely", "type": "entity"}, "# Lonely"))
    wiki.rebuild_index()
    (env / "wiki" / "index.md").write_text(
        wiki.index_path.read_text(encoding="utf-8") + "- [[removed]]\n", encoding="utf-8"
    )
    report = lint(_wiki(env))
    assert report.broken_links == {"a": ["ghost"]}
    assert report.missing_pages == {"ghost": 1}
    assert report.orphans == ["a", "lonely"]
    assert "sources" in report.missing_fields["lonely"]
    assert report.index_dangling == ["removed"]
    out = runner.invoke(cli_app, ["wiki", "lint"])
    assert out.exit_code == 0 and "[[a]] → ghost" in out.output


# ---------- SM-2 复习 ----------


def test_schedule_rules():
    assert schedule(1, 2.5, "想回炉", "remembered") == (2, 2.6, "想回炉")
    assert schedule(10, 2.5, "想回炉", "remembered") == (25, 2.6, "扎实")  # 间隔 ≥ 21 天
    assert schedule(4, 2.5, "陌生", "remembered")[2] == "接触过"
    assert schedule(10, 2.5, "扎实", "fuzzy") == (12, 2.35, "想回炉")
    assert schedule(10, 2.5, "想回炉", "forgot") == (1, 2.3, "接触过")
    assert schedule(3, 1.35, "陌生", "forgot") == (1, 1.3, "陌生")  # ease 下限、陌生不再降
    assert schedule(30, 2.95, "扎实", "remembered")[1] == 3.0  # ease 上限
    with pytest.raises(ValueError):
        schedule(1, 2.5, None, "maybe")


def test_review_updates_frontmatter_and_due(env):
    wiki = _wiki(env)
    meta = new_meta("KV cache", "concept", TODAY - timedelta(days=3), mastery="想回炉")
    wiki.save_page(Page("kv-cache", meta, "# KV cache"))
    wiki.save_page(Page("vllm", new_meta("vLLM", "entity", TODAY - timedelta(days=9)), "# vLLM"))
    assert [p.slug for p in due_concepts(wiki, TODAY)] == ["kv-cache"]  # 实体不参与复习
    out = review(wiki, wiki.pages["kv-cache"], "remembered", TODAY)
    assert (out.interval_after, out.next_due) == (2, TODAY + timedelta(days=2))
    reloaded = _wiki(env).pages["kv-cache"]
    assert (
        reloaded.meta["interval_days"] == 2 and reloaded.meta["last_reviewed"] == TODAY.isoformat()
    )
    assert due_concepts(_wiki(env), TODAY) == []
    assert [p.slug for p in due_concepts(_wiki(env), TODAY + timedelta(days=2))] == ["kv-cache"]
    assert "] review | KV cache" in wiki.log_path.read_text(encoding="utf-8")
    due = runner.invoke(cli_app, ["wiki", "due", "--date", (TODAY + timedelta(days=2)).isoformat()])
    assert "[[kv-cache]]" in due.output
    cli = runner.invoke(cli_app, ["wiki", "review", "kv-cache", "forgot"])
    assert cli.exit_code == 0 and "掌握度 想回炉 → 接触过" in cli.output


# ---------- 闭环 ----------


class RecordingFake(FakeBackend):
    def __init__(self):
        self.l1 = []

    def complete_json(self, messages, schema, tier):
        if tier == "cheap":
            self.l1.append(messages)
        return super().complete_json(messages, schema, tier)


def _run(conn, wiki, backend=None, today=TODAY):
    return run_pipeline(
        conn,
        FilterSettings(),
        backend or FakeBackend(),
        PROFILE,
        limit=None,
        now=datetime.now(UTC),
        today=today,
        wiki=wiki,
    )


def _concept(wiki, slug, title, mastery, days_ago=5, aliases=()):
    meta = new_meta(
        title, "concept", TODAY - timedelta(days=days_ago), mastery=mastery, aliases=list(aliases)
    )
    meta["summary"] = f"{title} 的摘要"
    wiki.save_page(Page(slug, meta, f"# {title}"))


def test_l1_gets_known_knowledge_without_changing_system_prefix(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "kv-cache", "KV cache", "扎实")
    add_item(conn, 1, "A new KV cache eviction policy for LLM inference")
    conn.commit()
    with_wiki = RecordingFake()
    _run(conn, wiki, with_wiki)
    user = with_wiki.l1[0][1]["content"]
    assert user.startswith("## 已知知识（来自用户的 wiki）")
    assert "KV cache（concept；掌握度：扎实）— KV cache 的摘要" in user

    add_item(conn, 2, "Another kv cache post")
    conn.commit()
    without = RecordingFake()
    _run(conn, None, without)
    assert "已知知识" not in without.l1[0][1]["content"]
    assert with_wiki.l1[0][0]["content"] == without.l1[0][0]["content"]  # system 前缀不变


def test_due_concepts_go_to_refresher_with_related_item(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "continuous-batching", "Continuous batching", "想回炉")
    add_item(conn, 1, "Continuous batching in vLLM, revisited")
    conn.commit()
    report = _run(conn, wiki)
    assert report.concepts_placed == 1
    (row,) = db.digest_concepts(conn, TODAY.isoformat())
    assert (row["slug"], row["est_minutes"], row["mastery"]) == ("continuous-batching", 5, "想回炉")
    assert row["related_title"] == "Continuous batching in vLLM, revisited"
    assert report.minutes_used >= 5
    # 同一天再跑不重复
    assert _run(conn, wiki).concepts_placed == 0
    assert len(db.digest_concepts(conn, TODAY.isoformat())) == 1


def test_concepts_respect_budget(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "kv-cache", "KV cache", "想回炉")
    fs = FilterSettings.model_validate(
        {"daily_budget_minutes": 3, "lane_budget_reserve": {"refresher": 0}}
    )
    report = run_pipeline(
        conn, fs, FakeBackend(), PROFILE, limit=None, now=datetime.now(UTC), today=TODAY, wiki=wiki
    )
    assert report.concepts_placed == 0  # 5 分钟放不进 3 分钟预算


# ---------- Web ----------


@pytest.fixture
def client(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "kv-cache", "KV cache", "想回炉", aliases=["KV-cache"])
    page = Page(
        "paged-attention",
        new_meta("PagedAttention", "paper", TODAY, summary="分块 KV cache"),
        "# PagedAttention\n\n依赖 [[kv-cache]] 与 [[missing-page]]。<script>alert(1)</script>\n\n"
        "## 来源\n\n- [原文](../../../raw/7.md)\n",
    )
    wiki.save_page(page)
    wiki.rebuild_index()
    item = add_item(
        conn, 1, "Blog post on attention", l2={**L2, "prerequisites": ["KV cache", "Rust"]}
    )
    db.upsert_digest(
        conn,
        TODAY.isoformat(),
        type(
            "P",
            (),
            {
                "item_id": item,
                "lane": "must_read",
                "rank": 1,
                "est_minutes": 20,
                "is_deep_read": False,
                "deferred_from": None,
            },
        )(),
        datetime.now(UTC),
    )
    db.upsert_digest_concept(
        conn, TODAY.isoformat(), "kv-cache", "KV cache", "想回炉", 5, item, datetime.now(UTC)
    )
    conn.commit()
    return TestClient(create_app())


def test_wiki_index_and_page(client):
    html = client.get("/wiki").text
    assert 'href="/wiki/page/kv-cache"' in html
    assert 'hx-post="/wiki/review"' in html and "还记得" in html
    page = client.get("/wiki/page/paged-attention").text
    assert '<a href="/wiki/page/kv-cache">KV cache</a>' in page
    assert 'class="missing"' in page
    assert "<script>" not in page  # 原始 HTML 被转义
    assert 'href="/item/7"' in page  # 原文链接指向条目详情
    assert client.get("/wiki/page/nope").status_code == 404


def test_review_button(client, env):
    resp = client.post("/wiki/review", data={"slug": "kv-cache", "grade": "fuzzy"})
    assert resp.status_code == 200 and "已记录：模糊" in resp.text
    assert _wiki(env).pages["kv-cache"].meta["last_reviewed"] == datetime.now().date().isoformat()
    assert client.post("/wiki/review", data={"slug": "kv-cache", "grade": "meh"}).status_code == 400
    assert (
        client.post("/wiki/review", data={"slug": "paged-attention", "grade": "fuzzy"}).status_code
        == 404
    )


def test_digest_shows_concepts_and_wiki_refresh_links(client):
    html = client.get(f"/?date={TODAY.isoformat()}").text
    assert 'data-concept="kv-cache"' in html and "概念复习" in html
    assert 'data-budget="25/105"' in html  # 20 分钟必读 + 5 分钟概念复习
    # 前置知识命中 wiki 中不扎实的概念 → 链接到 wiki 页面
    assert '<a href="/wiki/page/kv-cache">KV cache</a>' in html
    assert 'href="/wiki"' in html


# ---------- 宽容解析（ADR 0011） ----------


def test_wiki_plan_lenient_parsing_drops_only_bad_ops():
    raw = (FIXTURES / "wiki_plan_malformed.json").read_text(encoding="utf-8")
    plan = WikiPlan.model_validate_json(raw)
    assert [c.slug for c in plan.creates] == ["concept-a", "Concept B"]
    a, b = plan.creates
    assert (a.type, a.aliases) == ("concept", ["concept a"])  # 大小写 / 空白、字符串别名被修正
    assert b.summary == ""  # 缺一行摘要：留空
    assert len(plan.updates) == 1 and plan.updates[0].action == "add_links"
    assert plan.updates[0].links == ["concept-b"]
    assert len(plan.dropped) == 2
    assert any(d.startswith("creates[2]：body: Field required") for d in plan.dropped)
    assert any(d.startswith("creates[3]：type:") for d in plan.dropped)


def test_dropped_ops_are_reported_and_rest_applied(env, conn):
    item = add_item(conn, 1, "Example", l2=L2)
    plan = WikiPlan.model_validate_json(
        (FIXTURES / "wiki_plan_malformed.json").read_text(encoding="utf-8")
    )
    result = apply_plan(_wiki(env), plan, db.item_detail(conn, item), PROFILE, TODAY)
    assert result.created == ["concept-a", "concept-b"]
    assert result.updated == []  # 对新建页的 add_links 计入新建
    assert "[[concept-b]]" in _wiki(env).pages["concept-a"].body
    assert sum("解析时丢弃" in r for r in result.rejected) == 2


def test_truncated_plan_is_not_partially_applied():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WikiPlan.model_validate_json(
            (FIXTURES / "wiki_plan_truncated.json").read_text(encoding="utf-8")
        )
