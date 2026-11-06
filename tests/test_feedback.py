"""M7：文章反馈的两组语义（ADR 0013）——当前状态与撤销、稍后再看、积压、L1 样例与前置知识、
信源调整、wiki 队列，以及概念复习的"需要回炉 / 已扎实"。全部离线。"""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient

from secondbrain import config, db
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.run import run_pipeline
from secondbrain.profile.feedback import (
    few_shot_block,
    recent_feedback,
    too_hard_prerequisites,
)
from secondbrain.profile.loader import load_profile
from secondbrain.profile.tune import feedback_distribution
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app
from secondbrain.wiki.ingest import pending_items
from secondbrain.wiki.review import GRADES, due_concepts, review, schedule
from secondbrain.wiki.store import Page, new_meta, open_wiki

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
TODAY = date(2026, 11, 4)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    shutil.copy(FIXTURES / "profile.md", tmp_path / "profile.md")
    return tmp_path


@pytest.fixture
def conn(env: Path):
    c = db.connect()
    db.upsert_sources(
        c,
        [
            SourceConfig(id="blog", type="rss", url="https://b", trust=3),
            SourceConfig(id="news", type="rss", url="https://n", trust=3),
        ],
    )
    yield c
    c.close()


def add_item(conn, n: int, title: str, source: str = "blog", l2: dict | None = None) -> int:
    body = f"{title}. " + "Body text about inference systems. " * 50
    db.insert_item(
        conn,
        db.NewItem(
            source_id=source,
            url=f"https://example.com/{source}/{n}",
            url_canonical=f"https://example.com/{source}/{n}",
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
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    if l2 is not None:
        db.add_assessment(conn, item_id, "L2", l2, NOW, "fake-strong", 7.0)
    conn.commit()
    return item_id


def read_at(conn, item_id: int) -> str | None:
    return conn.execute("SELECT read_at FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# ---------- 当前状态与撤销 ----------


def test_toggle_same_kind_undoes(conn):
    item = add_item(conn, 1, "A post")
    assert db.toggle_feedback(conn, item, "gained", None, NOW) == "gained"
    assert read_at(conn, item)
    assert db.toggle_feedback(conn, item, "gained", None, NOW) is None  # 再点一次 = 撤销
    assert db.feedback_state(conn, [item]) == {item: None}
    assert read_at(conn, item) is None
    kinds = [r["kind"] for r in db.feedback_for(conn, item)]
    assert kinds == ["gained", "clear"]  # 历史保留


def test_switching_keeps_first_read_time(conn):
    item = add_item(conn, 1, "A post")
    db.toggle_feedback(conn, item, "too_easy", None, NOW)
    db.toggle_feedback(conn, item, "gained", None, NOW + timedelta(hours=1))
    assert db.feedback_state(conn, [item])[item] == "gained"
    assert read_at(conn, item) == db.format_time(NOW)
    db.toggle_feedback(conn, item, "later", None, NOW + timedelta(hours=2))
    assert read_at(conn, item) is None  # 读之前的状态清空 read_at


def test_overridden_feedback_is_not_a_signal(conn):
    item = add_item(conn, 1, "Flip")
    db.add_feedback(conn, item, "gained", None, NOW - timedelta(hours=2))
    db.add_feedback(conn, item, "clear", None, NOW - timedelta(hours=1))
    assert recent_feedback(conn, NOW, 30, 50) == []
    assert pending_items(conn) == []  # 撤销后不再待编入 wiki


# ---------- 稍后再看与积压 ----------


@pytest.fixture
def digest_env(env: Path, conn):
    """3 条必读的 digest（直接写 digests 表，与流水线无关）。"""
    for n in range(3):
        item = add_item(conn, n, f"Must read {n}")
        conn.execute(
            "INSERT INTO digests (date, item_id, lane, rank, est_minutes, is_deep_read, created_at)"
            " VALUES (?, ?, 'must_read', ?, 10, 0, ?)",
            (TODAY.isoformat(), item, n, db.format_time(NOW)),
        )
    conn.commit()
    return env


def test_later_and_dismiss_leave_the_backlog(digest_env, conn):
    must = [r[0] for r in conn.execute("SELECT item_id FROM digests WHERE lane = 'must_read'")]
    assert len(must) == 3
    later_day = (TODAY + timedelta(days=1)).isoformat()
    assert len(db.past_must_reads(conn, later_day)) == len(must)
    db.add_feedback(conn, must[0], "later", None, NOW)
    db.add_feedback(conn, must[1], "dismiss", None, NOW)
    assert [i for i, _ in db.past_must_reads(conn, later_day)] == must[2:]
    db.add_feedback(conn, must[0], "clear", None, NOW)  # 撤销稍后读：回到积压
    assert must[0] in [i for i, _ in db.past_must_reads(conn, later_day)]


def test_later_page_and_home_link(digest_env, conn):
    client = TestClient(create_app())
    item = conn.execute("SELECT item_id FROM digests LIMIT 1").fetchone()[0]
    home = client.get("/").text
    assert 'href="/later"' in home and "稍后再看：0 条" in home
    assert "读之前" in home and "读完后" in home
    assert 'title="读完了，有收获' in home  # 按钮有说明
    resp = client.post("/feedback", data={"item_id": item, "kind": "later"})
    assert "已记录：稍后读" in resp.text
    assert "稍后再看：1 条" in client.get("/").text
    page = client.get("/later").text
    assert f'data-item-id="{item}"' in page and 'class="fb active"' in page
    undo = client.post("/feedback", data={"item_id": item, "kind": "later"})
    assert "已撤销：稍后读" in undo.text and 'class="fb active"' not in undo.text
    assert f'data-item-id="{item}"' not in client.get("/later").text


# ---------- L1：样例与"太难"的前置知识 ----------


class RecordingFake(FakeBackend):
    def __init__(self):
        self.messages = []

    def complete_json(self, messages, schema, tier):
        if tier == "cheap":
            self.messages.append(messages)
        return super().complete_json(messages, schema, tier)


def test_few_shot_groups(conn):
    ids = {}
    for n, kind in enumerate(["gained", "dismiss", "worthless", "too_easy", "too_hard", "later"]):
        ids[kind] = add_item(conn, n, f"Post {kind}")
        db.add_feedback(conn, ids[kind], kind, None, NOW - timedelta(hours=n + 1))
    block = few_shot_block(recent_feedback(conn, NOW, 30, 50), per_polarity=5)
    assert "用户有收获（正例）：\n- [有收获] Post gained" in block
    assert "- [不感兴趣] Post dismiss" in block and "- [没价值] Post worthless" in block
    assert "用户觉得太浅（已掌握，同类内容 novelty 应低）：\n- [太浅] Post too_easy" in block
    assert "用户觉得太难（缺前置知识，不代表不感兴趣）：\n- [太难] Post too_hard" in block
    assert "Post later" not in block


def test_too_hard_prerequisites_are_injected_into_l1_user_message(env, conn):
    hard = add_item(conn, 1, "Hard one", l2={"prerequisites": ["Linear algebra", "CUDA warps"]})
    hard2 = add_item(conn, 2, "Hard two", l2={"prerequisites": ["cuda warps", "Roofline model"]})
    for item in (hard, hard2):
        db.add_feedback(conn, item, "too_hard", None, NOW - timedelta(hours=1))
    block = too_hard_prerequisites(conn, NOW, 30, max_chars=600)
    # 最近标记的在前；不区分大小写去重
    assert "cuda warps、Roofline model、Linear algebra" in block
    assert "提高 refresher_value" in block
    assert too_hard_prerequisites(conn, NOW, 30, max_chars=0) == ""
    short = too_hard_prerequisites(conn, NOW, 30, max_chars=95)
    assert len(short) <= 95 and "Linear algebra" not in short

    add_item(conn, 3, "A new post to screen")
    backend = RecordingFake()
    run_pipeline(
        conn,
        FilterSettings(),
        backend,
        load_profile(env / "profile.md"),
        limit=None,
        now=NOW,
        today=TODAY,
    )
    assert backend.messages
    user = backend.messages[0][1]["content"]
    assert user.startswith("## 用户近期觉得太难的内容所需前置知识")
    assert "Roofline model" not in backend.messages[0][0]["content"]  # 不进 system 前缀


# ---------- 信源调整 ----------


def test_feedback_distribution_counts_every_kind(conn):
    kinds = ["gained", "gained", "dismiss", "worthless", "too_easy", "too_hard", "later"]
    for n, kind in enumerate(kinds):
        db.add_feedback(conn, add_item(conn, n, f"p{n}"), kind, None, NOW - timedelta(hours=1))
    assert feedback_distribution(conn)["blog"] == {
        "gained": 2,
        "too_easy": 1,
        "too_hard": 1,
        "worthless": 1,
        "dismiss": 1,
        "later": 1,
    }


# ---------- 概念复习：需要回炉 / 已扎实 ----------


def test_schedule_new_grades():
    assert schedule(10, 2.5, "扎实", "needs_refresh") == (0, 2.5, "想回炉")
    assert schedule(4, 2.5, "接触过", "mastered") == (30, 2.5, "扎实")
    assert schedule(20, 2.5, "想回炉", "mastered") == (50, 2.5, "扎实")
    assert schedule(4, 2.5, "接触过", "mastered", mastered_days=45)[0] == 45


def test_review_needs_refresh_is_due_immediately(env):
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    meta = new_meta("KV cache", "concept", TODAY - timedelta(days=1), mastery="扎实")
    meta["interval_days"] = 20
    wiki.save_page(Page("kv-cache", meta, "# KV cache"))
    assert due_concepts(wiki, TODAY) == []
    out = review(wiki, wiki.pages["kv-cache"], "needs_refresh", TODAY)
    assert (out.mastery_after, out.interval_after, out.next_due) == ("想回炉", 0, TODAY)
    reloaded = open_wiki(config.wiki_dir(), config.raw_dir())
    assert [p.slug for p in due_concepts(reloaded, TODAY)] == ["kv-cache"]
    out = review(reloaded, reloaded.pages["kv-cache"], "mastered", TODAY, mastered_days=40)
    assert (out.mastery_after, out.interval_after) == ("扎实", 40)


def test_review_buttons_include_new_grades(env):
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    wiki.save_page(Page("kv-cache", new_meta("KV cache", "concept", TODAY), "# KV cache"))
    client = TestClient(create_app())
    page = client.get("/wiki/page/kv-cache").text
    for label in GRADES.values():
        assert label in page
    assert 'title="想系统回炉' in page
    resp = client.post("/wiki/review", data={"slug": "kv-cache", "grade": "mastered"})
    assert resp.status_code == 200 and "已记录：已扎实" in resp.text
    assert "掌握度 — → 扎实" in resp.text and "间隔 1 → 30 天" in resp.text
