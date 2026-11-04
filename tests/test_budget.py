"""M8：深读条目按速读时间计入预算（ADR 0015）——migration、同日共享、页面与 CLI 显示。"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import db
from secondbrain.cli import app as cli_app
from secondbrain.models import SourceConfig
from secondbrain.pipeline.tiering import Placement
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
DAY = "2026-11-04"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    c = db.connect()
    db.upsert_sources(c, [SourceConfig(id="arxiv", type="rss", url="https://a")])
    yield c
    c.close()


def add_item(conn, n: int, title: str) -> int:
    body = f"{title}. " + "Long paper body. " * 100
    db.insert_item(
        conn,
        db.NewItem(
            "arxiv",
            f"https://a/{n}",
            f"https://a/{n}",
            content_hash(title, body),
            title,
            None,
            None,
            body,
            "pdf",
            NOW - timedelta(hours=1),
        ),
        NOW,
    )
    return conn.execute("SELECT MAX(id) FROM items").fetchone()[0]


def test_migration_v7_fills_budget_from_est(tmp_path: Path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    for script in db.MIGRATIONS[:6]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 6")
    old.execute(
        "INSERT INTO sources VALUES ('s', 'rss', 'u', NULL, 3, 'core', '[]', 7, 1, 0, 0, 'x',"
        " NULL, NULL, NULL, NULL)"
    )
    old.execute(
        "INSERT INTO items (id, source_id, url, url_canonical, content_hash, title,"
        " extract_method, fetched_at) VALUES (1, 's', 'u', 'u', 'h', 't', 'feed', 'x')"
    )
    old.execute(
        "INSERT INTO digests (date, item_id, lane, rank, est_minutes, is_deep_read, created_at)"
        " VALUES ('2026-11-03', 1, 'must_read', 1, 90, 1, 'x')"
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("SELECT budget_minutes FROM digests").fetchone()[0] == 90
    conn.close()


def test_day_usage_and_pages_use_budget_minutes(conn):
    deep = add_item(conn, 1, "A very long paper")
    short = add_item(conn, 2, "A short post")
    db.upsert_digest(conn, DAY, Placement(deep, "must_read", 1, 90, True, None, 15), NOW)
    db.upsert_digest(conn, DAY, Placement(short, "must_read", 2, 20, False), NOW)
    conn.commit()
    counts, minutes = db.day_usage(conn, DAY, frozenset({"must_read"}), set())
    assert (counts, minutes) == ({"must_read": 2}, 35)  # 15（速读）+ 20

    html = TestClient(create_app()).get(f"/?date={DAY}").text
    assert 'data-budget="35/105"' in html
    assert "速读约 15 分钟 · 全文约 90 分钟" in html
    assert "arxiv · 20 分钟" in html

    out = CliRunner().invoke(cli_app, ["digest", "show", "--date", DAY]).output
    assert "速读约 15 分钟 · 全文约 90 分钟" in out and "总耗时 35 / 预算 105 分钟" in out


def test_pipeline_places_long_papers_with_summary_minutes(conn, monkeypatch):
    """fake L2 的 est 最多 45 分钟：把预算调小，让 est > 预算 × 0.6 成为深读。"""
    from secondbrain.llm.fake import FakeBackend
    from secondbrain.models import FilterSettings
    from secondbrain.pipeline.run import run_pipeline
    from secondbrain.profile.loader import parse_profile

    for n in range(3):
        body = "LLM inference serving kv cache attention. " * 2000
        title = f"Inference serving paper {n}"
        db.insert_item(
            conn,
            db.NewItem(
                "arxiv",
                f"https://p/{n}",
                f"https://p/{n}",
                content_hash(title, body),
                title,
                None,
                None,
                body,
                "pdf",
                NOW - timedelta(hours=n),
            ),
            NOW,
        )
    conn.commit()
    fs = FilterSettings(daily_budget_minutes=40, deep_summary_minutes=10, lane_budget_reserve={})
    report = run_pipeline(
        conn, fs, FakeBackend(), parse_profile(""), limit=None, now=NOW, today=date(2026, 11, 4)
    )
    rows = conn.execute(
        "SELECT lane, est_minutes, budget_minutes, is_deep_read FROM digests WHERE date = ?",
        (DAY,),
    ).fetchall()
    top = ("must_read", "worth_it", "refresher", "serendipity")
    deep = [r for r in rows if r["is_deep_read"] and r["lane"] in top]
    assert deep and all(r["budget_minutes"] == 10 < r["est_minutes"] for r in deep)
    assert report.minutes_used == sum(r["budget_minutes"] for r in rows if r["lane"] in top)
