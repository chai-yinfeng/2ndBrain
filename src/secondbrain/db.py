"""SQLite 存储：连接、schema 迁移（PRAGMA user_version）、sources / items 读写。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from secondbrain import config
from secondbrain.models import SourceConfig

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# 每个元素是一个版本的迁移脚本；只能追加，不能修改已发布的脚本。
MIGRATIONS: list[str] = [
    """
    CREATE TABLE sources (
        id            TEXT PRIMARY KEY,
        type          TEXT NOT NULL,
        url           TEXT,
        query         TEXT,
        trust         INTEGER NOT NULL,
        role          TEXT NOT NULL,
        tags          TEXT NOT NULL,          -- JSON 数组
        max_age_days  INTEGER NOT NULL,
        enabled       INTEGER NOT NULL,
        backfill      INTEGER NOT NULL,
        needs_scraper INTEGER NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE TABLE items (
        id             INTEGER PRIMARY KEY,
        source_id      TEXT NOT NULL REFERENCES sources(id),
        url            TEXT NOT NULL,
        url_canonical  TEXT NOT NULL UNIQUE,
        content_hash   TEXT NOT NULL,
        title          TEXT NOT NULL,
        author         TEXT,
        summary        TEXT,
        content        TEXT NOT NULL DEFAULT '',
        extract_method TEXT NOT NULL,         -- feed | html | arxiv_html | pdf | summary | none
        published_at   TEXT,
        fetched_at     TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'new'
    );
    CREATE INDEX idx_items_content_hash ON items(content_hash);
    CREATE INDEX idx_items_published_at ON items(published_at);
    CREATE INDEX idx_items_source_id ON items(source_id);
    """,
    # v2（M2）：评估、分级、LLM 调用记录。items.status 新增取值见 ITEM_STATUSES。
    """
    CREATE TABLE assessments (
        id         INTEGER PRIMARY KEY,
        item_id    INTEGER NOT NULL REFERENCES items(id),
        stage      TEXT NOT NULL,             -- L0 | L1 | L2
        model      TEXT,
        data       TEXT NOT NULL,             -- JSON：该阶段的全部输出字段
        score      REAL,                      -- 该阶段的排序分（L0 为空）
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_assessments_item_stage ON assessments(item_id, stage);
    CREATE TABLE digests (
        id            INTEGER PRIMARY KEY,
        date          TEXT NOT NULL,          -- 本地日期 YYYY-MM-DD
        item_id       INTEGER NOT NULL REFERENCES items(id),
        lane          TEXT NOT NULL,          -- must_read | worth_it | refresher | serendipity
                                              -- | fyi | archived | deferred
        rank          INTEGER NOT NULL,
        est_minutes   INTEGER NOT NULL,
        is_deep_read  INTEGER NOT NULL,
        deferred_from TEXT,                   -- 首次被顺延的日期
        created_at    TEXT NOT NULL,
        UNIQUE (date, item_id)
    );
    CREATE INDEX idx_digests_lane ON digests(lane);
    CREATE TABLE llm_calls (
        id                 INTEGER PRIMARY KEY,
        ts                 TEXT NOT NULL,
        backend            TEXT NOT NULL,
        stage              TEXT NOT NULL,
        model              TEXT NOT NULL,
        prompt_tokens      INTEGER NOT NULL,
        cache_hit_tokens   INTEGER NOT NULL,
        cache_miss_tokens  INTEGER NOT NULL,
        completion_tokens  INTEGER NOT NULL,
        cost_usd           REAL NOT NULL,
        is_peak            INTEGER NOT NULL
    );
    CREATE INDEX idx_llm_calls_ts ON llm_calls(ts);
    """,
]

# items.status：new（待处理）→ l0_rejected | l1（已粗筛）→ l2（已精读）→ tiered | deferred
ITEM_STATUSES = ("new", "l0_rejected", "l1", "l2", "tiered", "deferred")


def format_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TIME_FORMAT)


def db_path() -> Path:
    return config.data_dir() / "brain.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {number}")
        conn.commit()


def upsert_sources(conn: sqlite3.Connection, sources: list[SourceConfig]) -> None:
    now = format_time(datetime.now(UTC))
    conn.executemany(
        """
        INSERT INTO sources (id, type, url, query, trust, role, tags, max_age_days,
                             enabled, backfill, needs_scraper, updated_at)
        VALUES (:id, :type, :url, :query, :trust, :role, :tags, :max_age_days,
                :enabled, :backfill, :needs_scraper, :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            type = excluded.type, url = excluded.url, query = excluded.query,
            trust = excluded.trust, role = excluded.role, tags = excluded.tags,
            max_age_days = excluded.max_age_days, enabled = excluded.enabled,
            backfill = excluded.backfill, needs_scraper = excluded.needs_scraper,
            updated_at = excluded.updated_at
        """,
        [
            {**s.model_dump(), "tags": json.dumps(s.tags, ensure_ascii=False), "updated_at": now}
            for s in sources
        ],
    )
    conn.commit()


def known_urls(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(r[0] for r in conn.execute("SELECT url_canonical FROM items"))


@dataclass
class NewItem:
    source_id: str
    url: str
    url_canonical: str
    content_hash: str
    title: str
    author: str | None
    summary: str | None
    content: str
    extract_method: str
    published_at: datetime | None


def insert_item(conn: sqlite3.Connection, item: NewItem, fetched_at: datetime) -> bool:
    """插入一条；url_canonical 或 content_hash 已存在时不插入并返回 False。"""
    dup = conn.execute(
        "SELECT 1 FROM items WHERE content_hash = ? LIMIT 1", (item.content_hash,)
    ).fetchone()
    if dup:
        return False
    cur = conn.execute(
        """
        INSERT INTO items (source_id, url, url_canonical, content_hash, title, author, summary,
                           content, extract_method, published_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url_canonical) DO NOTHING
        """,
        (
            item.source_id,
            item.url,
            item.url_canonical,
            item.content_hash,
            item.title,
            item.author,
            item.summary,
            item.content,
            item.extract_method,
            format_time(item.published_at) if item.published_at else None,
            format_time(fetched_at),
        ),
    )
    return cur.rowcount == 1


def list_items(
    conn: sqlite3.Connection, since: datetime | None = None, source_id: str | None = None
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if since is not None:
        clauses.append("COALESCE(published_at, fetched_at) >= ?")
        params.append(format_time(since))
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT id, source_id, title, url, extract_method, length(content) AS content_len,
               COALESCE(published_at, fetched_at) AS ts
        FROM items {where}
        ORDER BY ts DESC, id DESC
    """
    return conn.execute(sql, params).fetchall()


# ---------- M2：评估与分级 ----------

ITEM_COLUMNS = """
    i.id, i.source_id, i.url, i.title, i.summary, i.content, i.extract_method,
    i.published_at, i.fetched_at, i.status, length(i.content) AS content_len,
    s.trust, s.role
"""


def items_by_status(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"""
        SELECT {ITEM_COLUMNS} FROM items i JOIN sources s ON s.id = i.source_id
        WHERE i.status IN ({marks})
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
        """,
        statuses,
    ).fetchall()


def set_status(conn: sqlite3.Connection, item_ids: list[int], status: str) -> None:
    assert status in ITEM_STATUSES
    conn.executemany("UPDATE items SET status = ? WHERE id = ?", [(status, i) for i in item_ids])


def add_assessment(
    conn: sqlite3.Connection,
    item_id: int,
    stage: str,
    data: dict,
    ts: datetime,
    model: str | None = None,
    score: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO assessments (item_id, stage, model, data, score, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, stage, model, json.dumps(data, ensure_ascii=False), score, format_time(ts)),
    )


def latest_assessments(conn: sqlite3.Connection, stage: str) -> dict[int, dict]:
    """每个条目在该阶段最新的一条评估：item_id → {**data, "_score", "_model"}。"""
    rows = conn.execute(
        """
        SELECT a.item_id, a.data, a.score, a.model FROM assessments a
        JOIN (SELECT item_id, MAX(id) AS id FROM assessments WHERE stage = ? GROUP BY item_id) m
          ON m.id = a.id
        """,
        (stage,),
    ).fetchall()
    return {
        r["item_id"]: {**json.loads(r["data"]), "_score": r["score"], "_model": r["model"]}
        for r in rows
    }


def deferred_since(conn: sqlite3.Connection) -> dict[int, str]:
    """当前处于顺延状态的条目 → 首次顺延日期。"""
    rows = conn.execute(
        """
        SELECT d.item_id, COALESCE(d.deferred_from, d.date) AS since FROM digests d
        JOIN items i ON i.id = d.item_id
        WHERE i.status = 'deferred' AND d.lane = 'deferred'
        """
    ).fetchall()
    result: dict[int, str] = {}
    for r in rows:
        if r["item_id"] not in result or r["since"] < result[r["item_id"]]:
            result[r["item_id"]] = r["since"]
    return result


def day_usage(
    conn: sqlite3.Connection, day: str, lanes: frozenset[str], exclude: set[int]
) -> tuple[dict[str, int], int]:
    """某天前排 lane 已占用的 (各 lane 条数, 总分钟数)，不含 exclude 中的条目。"""
    counts: dict[str, int] = {}
    minutes = 0
    for r in conn.execute("SELECT item_id, lane, est_minutes FROM digests WHERE date = ?", (day,)):
        if r["lane"] in lanes and r["item_id"] not in exclude:
            counts[r["lane"]] = counts.get(r["lane"], 0) + 1
            minutes += r["est_minutes"]
    return counts, minutes


def upsert_digest(conn: sqlite3.Connection, day: str, placement, ts: datetime) -> None:
    conn.execute(
        """
        INSERT INTO digests (date, item_id, lane, rank, est_minutes, is_deep_read, deferred_from,
                             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, item_id) DO UPDATE SET
            lane = excluded.lane, rank = excluded.rank, est_minutes = excluded.est_minutes,
            is_deep_read = excluded.is_deep_read, deferred_from = excluded.deferred_from,
            created_at = excluded.created_at
        """,
        (
            day,
            placement.item_id,
            placement.lane,
            placement.rank,
            placement.est_minutes,
            int(placement.is_deep_read),
            placement.deferred_from,
            format_time(ts),
        ),
    )


def past_must_reads(conn: sqlite3.Connection, before_day: str) -> list[tuple[int, str]]:
    """此前各日的必读条目（M2 尚无反馈，全部视为未读）。"""
    rows = conn.execute(
        "SELECT item_id, date FROM digests WHERE lane = 'must_read' AND date < ?", (before_day,)
    ).fetchall()
    return [(r["item_id"], r["date"]) for r in rows]


def demote_must_reads(conn: sqlite3.Connection, item_ids: list[int], before_day: str) -> None:
    conn.executemany(
        "UPDATE digests SET lane = 'fyi' WHERE item_id = ? AND lane = 'must_read' AND date < ?",
        [(i, before_day) for i in item_ids],
    )


def digest_rows(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*, i.title, i.url, i.source_id FROM digests d JOIN items i ON i.id = d.item_id
        WHERE d.date = ? ORDER BY d.lane, d.rank
        """,
        (day,),
    ).fetchall()


def latest_digest_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM digests").fetchone()
    return row[0] if row else None
