"""经典回补：fetch 时把时间窗外的历史条目放进回补池，每天按限额选入温故栏。"""

from datetime import date, timedelta

import httpx
import pytest
from conftest import FIXTURES, NOW, fixture_bytes

from secondbrain import db
from secondbrain.ingest import fetch_sources
from secondbrain.llm.fake import FakeBackend
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.run import backfill_priority, run_pipeline
from secondbrain.profile.loader import load_profile
from secondbrain.urls import content_hash

BLOG_FEED = "https://blog.example.com/feed.xml"
PROFILE = load_profile(FIXTURES / "profile.md")
TODAY = date(2026, 11, 4)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    yield c
    c.close()


def _routes():
    return {
        BLOG_FEED: httpx.Response(200, content=fixture_bytes("rss.xml")),
        "https://blog.example.com/posts/continuous-batching": httpx.Response(
            200, content=fixture_bytes("article.html")
        ),
        "https://blog.example.com/posts/old": httpx.Response(
            200, content=fixture_bytes("article.html").replace(b"Continuous", b"Classic")
        ),
    }


# ---------- fetch ----------


def test_backfill_source_keeps_old_entries_in_pool(conn, make_client):
    client, _ = make_client(_routes(), min_feed_content_chars=50)
    src = SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=7, backfill=True)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert (result.added, result.backfilled) == (3, 1)
    rows = conn.execute("SELECT title, is_backfill FROM items ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows][-1] == ("An old post", 1)
    # 再跑一次：全部重复
    (again,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert (again.added, again.duplicates) == (0, 3)


def test_regular_source_drops_old_entries(conn, make_client):
    client, _ = make_client(_routes(), min_feed_content_chars=50)
    src = SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=7)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert (result.added, result.backfilled) == (2, 0)


def test_backfill_respects_limit(conn, make_client):
    client, _ = make_client(_routes(), min_feed_content_chars=50)
    src = SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=2, backfill=True)
    (result,) = fetch_sources(conn, [src], client, client.settings, limit=1, now=NOW)
    # 时间窗内 1 条；窗外 2 条中回补最多 limit=1 条
    assert (result.added, result.backfilled) == (2, 1)


# ---------- 选择与分级 ----------


def _item(conn, n, title, *, backfill, topics_hint="", days_old=1):
    body = f"{title}. {topics_hint} " + "Explaining the idea in depth. " * 80
    db.insert_item(
        conn,
        db.NewItem(
            source_id="classic" if backfill else "blog",
            url=f"https://example.com/{n}",
            url_canonical=f"https://example.com/{n}",
            content_hash=content_hash(title, body),
            title=title,
            author=None,
            summary=title,
            content=body,
            extract_method="feed",
            published_at=NOW - timedelta(days=days_old),
            is_backfill=backfill,
        ),
        NOW,
    )


@pytest.fixture
def pool(conn):
    db.upsert_sources(
        conn,
        [
            SourceConfig(id="blog", type="rss", url="u", trust=4),
            SourceConfig(id="classic", type="rss", url="u", trust=5, backfill=True),
        ],
    )
    _item(conn, 1, "Inference serving news for LLM gpu kernels", backfill=False)
    # 回补池：老文章，远超常规时间窗
    _item(conn, 10, "Understanding attention: an explainer", backfill=True, days_old=900)
    _item(conn, 11, "Notes on my garden", backfill=True, days_old=1200)
    _item(conn, 12, "A survey of KV cache inference serving", backfill=True, days_old=800)
    conn.commit()
    return conn


def _run(conn, today=TODAY, fs=None):
    return run_pipeline(
        conn, fs or FilterSettings(), FakeBackend(), PROFILE, limit=None, now=NOW, today=today
    )


def test_backfill_items_are_not_in_regular_l1(pool):
    fs = FilterSettings.model_validate({"backfill": {"daily_limit": 0}})
    report = _run(pool, fs=fs)
    assert report.l1_selected == 1
    statuses = dict(pool.execute("SELECT id, status FROM items").fetchall())
    assert {statuses[i] for i in (2, 3, 4)} == {"new"}  # 回补池未动


def test_one_backfill_item_per_day_goes_to_refresher(pool):
    report = _run(pool)
    assert (report.backfill_screened, report.backfill_picked, report.backfill_placed) == (3, 1, 1)
    rows = pool.execute(
        "SELECT i.title, d.lane FROM digests d JOIN items i ON i.id = d.item_id"
        " WHERE i.is_backfill = 1"
    ).fetchall()
    # survey 与 explainer 都是温故类型；survey 的 topics 命中 profile 中"想回炉"的领域，优先
    assert [tuple(r) for r in rows] == [("A survey of KV cache inference serving", "refresher")]
    # 其余回补条目留在池中（已粗筛）
    left = pool.execute(
        "SELECT COUNT(*) FROM items WHERE is_backfill = 1 AND status = 'backfill_l1'"
    )
    assert left.fetchone()[0] == 2

    # 同一天再跑：名额已用完，不再选
    again = _run(pool)
    assert again.backfill_picked == 0
    # 第二天：从已粗筛的池中再选一条，不重复粗筛
    next_day = _run(pool, today=TODAY + timedelta(days=1))
    assert (next_day.backfill_screened, next_day.backfill_picked) == (0, 1)
    titles = [
        r[0]
        for r in pool.execute(
            "SELECT i.title FROM digests d JOIN items i ON i.id = d.item_id"
            " WHERE i.is_backfill = 1 ORDER BY d.date"
        )
    ]
    assert titles[1] == "Understanding attention: an explainer"


def test_backfill_priority_prefers_explainers_and_weak_domains():
    row = None
    explainer = {"content_type": "explainer", "topics": [], "refresher_value": 5, "_score": 5}
    weak_match = {
        "content_type": "frontier",
        "topics": ["kv-cache"],
        "refresher_value": 9,
        "_score": 9,
    }
    plain = {"content_type": "frontier", "topics": ["gardening"], "refresher_value": 9, "_score": 9}
    ranked = sorted(
        [plain, weak_match, explainer],
        key=lambda d: backfill_priority(row, d, PROFILE),
        reverse=True,
    )
    assert ranked == [explainer, weak_match, plain]


def test_backfill_item_that_does_not_fit_stays_in_pool(pool):
    # 预算 9 分钟且不预留：必读占 5 分钟后，回补条目（5 分钟，非深读）放不下 → 回到池中，L2 保留
    fs = FilterSettings.model_validate(
        {"daily_budget_minutes": 9, "lane_budget_reserve": {"refresher": 0, "serendipity": 0}}
    )
    report = _run(pool, fs=fs)
    assert report.backfill_picked == 1 and report.backfill_placed == 0
    survey_id, status = pool.execute(
        "SELECT id, status FROM items WHERE title = 'A survey of KV cache inference serving'"
    ).fetchone()
    assert status == "backfill_l1"
    l2_rows = "SELECT COUNT(*) FROM assessments WHERE stage = 'L2' AND item_id = ?"
    assert pool.execute(l2_rows, (survey_id,)).fetchone()[0] == 1
    # 下一天放宽预算：直接复用已有 L2，不再调用
    calls_before = pool.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = 'L2'").fetchone()[0]
    _run(pool, today=TODAY + timedelta(days=1))
    calls_after = pool.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = 'L2'").fetchone()[0]
    lane = pool.execute("SELECT lane FROM digests WHERE item_id = ?", (survey_id,)).fetchone()
    assert lane is not None and lane[0] in ("refresher", "deferred")
    assert calls_after == calls_before  # 常规条目已处理过，回补条目复用 L2


# ---------- 发布时间异常 / 非文章页（M5 遗留） ----------


def test_implausible_dates_are_unknown():
    from datetime import UTC, datetime

    from secondbrain.sources.base import parse_iso_datetime, plausible

    assert plausible(datetime(1, 1, 1, tzinfo=UTC)) is None
    assert plausible(datetime(1989, 12, 31, tzinfo=UTC)) is None
    assert plausible(datetime.now(UTC) + timedelta(days=3)) is None
    assert plausible(datetime(2024, 5, 1, tzinfo=UTC)) is not None
    assert parse_iso_datetime("0001-01-01T00:00:00Z") is None


UNDATED_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>FAQ</title><link>https://blog.example.com/faq</link>
<pubDate>Mon, 01 Jan 0001 00:00:00 +0000</pubDate><description>short faq</description></item>
<item><title>A classic undated essay</title><link>https://blog.example.com/essay</link>
<description>essay</description></item>
<item><title>Fresh post</title><link>https://blog.example.com/fresh</link>
<pubDate>Tue, 03 Nov 2026 08:00:00 +0000</pubDate><description>fresh</description></item>
</channel></rss>"""


def test_undated_non_article_pages_stay_out_of_backfill(conn, make_client):
    essay = (
        b"<html><body><article>"
        + b"<p>A long classic essay paragraph.</p>" * 120
        + b"</article></body></html>"
    )
    routes = {
        BLOG_FEED: httpx.Response(200, content=UNDATED_FEED),
        "https://blog.example.com/faq": httpx.Response(
            200, content=b"<html><body><p>Contact me.</p></body></html>"
        ),
        "https://blog.example.com/essay": httpx.Response(200, content=essay),
        "https://blog.example.com/fresh": httpx.Response(
            200, content=fixture_bytes("article.html")
        ),
    }
    client, _ = make_client(routes)
    src = SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=7, backfill=True)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    rows = dict(conn.execute("SELECT title, is_backfill FROM items").fetchall())
    assert rows == {"Fresh post": 0, "A classic undated essay": 1}
    assert any("疑似非文章页" in n and "FAQ" in n for n in result.notes)


def test_undated_items_of_regular_sources_are_kept(conn, make_client):
    routes = {BLOG_FEED: httpx.Response(200, content=UNDATED_FEED)}
    client, _ = make_client(routes)
    src = SourceConfig(id="blog", type="rss", url=BLOG_FEED, max_age_days=7)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert result.added == 3 and result.backfilled == 0
