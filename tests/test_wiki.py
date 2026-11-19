"""知识 wiki：初始化、ingest、slug 与路径安全、lint、内容上下文、Web。全部离线。"""

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
    return open_wiki(env / "wiki", env / "wiki" / "raw")


# ---------- 存储与安全 ----------


def test_open_wiki_initializes_skeleton(env):
    wiki = _wiki(env)
    for folder in ("concepts", "entities", "papers", "materials"):
        assert (env / "wiki" / "pages" / folder).is_dir()
    assert "## 概念" not in wiki.index_path.read_text(encoding="utf-8")
    assert wiki.log_path.read_text(encoding="utf-8").startswith("# Wiki Log")


def test_slugify_and_path_safety(env):
    assert slugify("Paged Attention (vLLM)") == "paged-attention-vllm"
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("中文标题") == "中文标题"
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
    assert result.created == [f"material-{item}", "outside", "中文"]
    assert (env / "wiki" / "pages" / "concepts" / "outside.md").is_file()
    assert (env / "wiki" / "pages" / "concepts" / "中文.md").is_file()
    assert not (env.parent / "outside.md").exists()
    assert len(result.rejected) == 1
    files = [p for p in env.rglob("*.md")]
    assert all(p.is_relative_to(env) for p in files)


# ---------- ingest ----------


def test_ingest_pending_creates_pages_index_log(env, conn):
    item = add_item(
        conn, 1, "PagedAttention explained", l2=L2,
        content="PagedAttention manages KV cache. Continuous batching shares resources.",
    )
    db.add_feedback(conn, item, "gained", None, datetime.now(UTC))
    conn.commit()
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0, result.output
    assert f"新建：material-{item}, kv-cache, continuous-batching" in result.output

    wiki = _wiki(env)
    material = wiki.pages[f"material-{item}"]
    assert material.type == "material" and material.meta["sources"] == [item]
    assert "[[kv-cache]]" in material.body
    assert f"(../../raw/{item}.md)" in material.body
    raw = env / "wiki" / "raw" / f"{item}.md"
    assert raw.read_text(encoding="utf-8").startswith("# PagedAttention")
    assert not (env / "raw").exists()
    kv = wiki.pages["kv-cache"]
    assert "mastery" not in kv.meta and "interval_days" not in kv.meta
    assert "last_reviewed" not in kv.meta
    assert "mastery" not in material.meta
    index = wiki.index_path.read_text(encoding="utf-8")
    assert "[[kv-cache]]" in index and "（fake）KV cache" in index
    log = wiki.log_path.read_text(encoding="utf-8")
    assert "] ingest | PagedAttention explained" in log
    assert "- 新建 [[kv-cache]]" in log

    # 幂等：再次 --pending 没有待编入条目；显式指定也提示已编入
    calls_before = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    again = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert again.exit_code == 0, again.output
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls_before
    explicit = runner.invoke(cli_app, ["wiki", "ingest", str(item)])
    assert "已编入 wiki" in explicit.output
    assert len(_wiki(env).pages) == 3


def test_second_item_updates_existing_concepts(env, conn):
    first = add_item(
        conn, 1, "PagedAttention explained", l2=L2,
        content="PagedAttention manages KV cache. Continuous batching shares resources.",
    )
    second = add_item(conn, 2, "KV cache quantization", l2={**L2, "prerequisites": ["KV cache"]})
    for item in (first, second):
        db.add_feedback(conn, item, "gained", None, datetime.now(UTC))
    conn.commit()
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0, result.output
    kv = _wiki(env).pages["kv-cache"]
    assert sorted(kv.meta["sources"]) == [first, second]
    assert f"material-{second}" in kv.links()
    assert db.ingested_ids(conn) == {first, second}


def test_ingest_without_pending_is_reported(env):
    result = runner.invoke(cli_app, ["wiki", "ingest", "--pending"])
    assert result.exit_code == 0
    assert (env / "wiki" / "index.md").is_file()


def test_long_items_keep_full_available_text(env, conn):
    from secondbrain.wiki.ingest import item_payload

    content = "x" * 50_000 + " final tail"
    item = add_item(conn, 1, "Very long", l2=L2, content=content)
    payload = item_payload(db.item_detail(conn, item), None, FilterSettings())
    assert payload["text"] == content
    assert payload["text_kind"] == "full"
    assert payload["full_length_chars"] == len(content)
    assert payload["key_points"] == []  # 不从 L2 偷换正文


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


def _concept(wiki, slug, title, days_ago=5, aliases=()):
    meta = new_meta(
        title, "concept", TODAY - timedelta(days=days_ago), aliases=list(aliases)
    )
    meta["summary"] = f"{title} 的摘要"
    wiki.save_page(Page(slug, meta, f"# {title}"))


def test_l1_gets_related_content_without_changing_system_prefix(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "kv-cache", "KV cache")
    add_item(conn, 1, "A new KV cache eviction policy for LLM inference")
    conn.commit()
    with_wiki = RecordingFake()
    _run(conn, wiki, with_wiki)
    user = with_wiki.l1[0][1]["content"]
    assert user.startswith("## 相关知识内容")
    assert "KV cache（concept）— KV cache 的摘要" in user

    add_item(conn, 2, "Another kv cache post")
    conn.commit()
    without = RecordingFake()
    _run(conn, None, without)
    assert "## 相关知识内容" not in without.l1[0][1]["content"]
    assert with_wiki.l1[0][0]["content"] == without.l1[0][0]["content"]  # system 前缀不变




# ---------- Web ----------


@pytest.fixture
def client(env, conn):
    wiki = _wiki(env)
    _concept(wiki, "kv-cache", "KV cache", aliases=["KV-cache"])
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
    conn.commit()
    return TestClient(create_app())


def test_wiki_index_and_page(client):
    html = client.get("/wiki").text
    assert 'href="/wiki/page/kv-cache"' in html
    assert 'hx-post="/wiki/review"' not in html
    page = client.get("/wiki/page/paged-attention").text
    assert '<a href="/wiki/page/kv-cache">KV cache</a>' in page
    assert 'class="missing"' in page
    assert "<script>" not in page  # 原始 HTML 被转义
    assert 'href="/item/7"' in page  # 原文链接指向条目详情
    assert client.get("/wiki/page/nope").status_code == 404


def test_review_route_is_removed(client):
    response = client.post("/wiki/review", data={"slug": "kv-cache", "grade": "fuzzy"})
    assert response.status_code == 404


def test_digest_links_related_wiki_without_review_budget(client):
    html = client.get(f"/recommendations?date={TODAY.isoformat()}").text
    assert "概念复习" not in html
    assert 'data-budget="20/105"' in html
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
    assert result.created == [f"material-{item}", "concept-a", "concept-b"]
    assert result.updated == []  # 对新建页的 add_links 计入新建
    assert "[[concept-b]]" in _wiki(env).pages["concept-a"].body
    assert sum("解析时丢弃" in r for r in result.rejected) == 2


def test_truncated_plan_is_not_partially_applied():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WikiPlan.model_validate_json(
            (FIXTURES / "wiki_plan_truncated.json").read_text(encoding="utf-8")
        )


def test_raw_lives_inside_wiki_and_is_ignored(env, conn):
    """原文在 data/wiki/raw（ADR 0013）：不是页面，lint / index 都不受影响。"""
    from secondbrain import config
    from secondbrain.wiki.lint import lint

    assert config.raw_dir() == env / "wiki" / "raw"
    wiki = _wiki(env)
    wiki.write_raw(5, "Some [[broken-link]] title", "https://x", "body [[nope]]")
    wiki.save_page(Page("kv-cache", new_meta("KV cache", "concept", TODAY), "# KV cache"))
    wiki.rebuild_index()
    reopened = _wiki(env)
    assert set(reopened.pages) == {"kv-cache"}
    assert reopened.raw_link(5, "concept") == "../../raw/5.md"
    assert "raw" not in reopened.index_path.read_text(encoding="utf-8")
    assert lint(reopened).broken_links == {}
