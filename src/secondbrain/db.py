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
]


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
