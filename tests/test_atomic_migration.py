"""Atomic migration regressions with disposable databases only."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from secondbrain import db


def _v9(path):
    with closing(sqlite3.connect(path)) as conn:
        for script in db.MIGRATIONS[:9]:
            conn.executescript(script)
        conn.executescript("""
            PRAGMA user_version = 9;
            INSERT INTO sources
                (id,type,trust,role,tags,max_age_days,enabled,backfill,needs_scraper,updated_at)
            VALUES ('manual','manual',4,'core','[]',0,1,0,0,'now');
            INSERT INTO items (id,source_id,url,url_canonical,content_hash,title,content,
                               extract_method,fetched_at,read_at)
            VALUES (23,'manual','u','u','hash','title','text','html','now','now');
            INSERT INTO feedback(item_id,kind,ts,note) VALUES (23,'gained','now','preserve');
            INSERT INTO wiki_ingests(item_id,ts,pages) VALUES (23,'now','[]');
        """)


def test_v10_legacy_transaction_wrapper_preserves_references(tmp_path):
    path = tmp_path / "historical.db"
    _v9(path)
    with closing(db.connect(path)) as conn:
        row = conn.execute("""SELECT id, source_id, reading_state, processing_status, original_text
            FROM items""").fetchone()
        assert tuple(row) == (23, None, "read", "processed", "text")
        note = conn.execute("SELECT note FROM feedback WHERE item_id=23").fetchone()[0]
        assert note == "preserve"
        assert conn.execute("SELECT item_id FROM item_sources").fetchone()[0] == 23
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_error_after_v10_rolls_back_schema_data_and_version(tmp_path, monkeypatch):
    path = tmp_path / "broken.db"
    _v9(path)
    published = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", [*published, "DELETE FROM feedback; BAD SQL;"])
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.DatabaseError, match="不支持"):
            db.migrate(conn)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT source_id FROM items").fetchone()[0] == "manual"
        assert conn.execute("SELECT note FROM feedback").fetchone()[0] == "preserve"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='item_sources'").fetchall()
    monkeypatch.setattr(db, "MIGRATIONS", published)
    with closing(db.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(published)


def test_process_exit_before_commit_recovers_cleanly(tmp_path):
    path = tmp_path / "crash.db"
    _v9(path)
    code = """
import os, sqlite3, sys
from secondbrain import db
conn = sqlite3.connect(sys.argv[1])
conn.create_function('interrupt_process', 0, lambda: os._exit(47))
db.MIGRATIONS.append('CREATE TABLE unfinished (id INTEGER); INSERT INTO unfinished VALUES (1); '
                     'INSERT INTO unfinished VALUES (interrupt_process());')
db.migrate(conn)
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=15, check=False)
    assert result.returncode == 47
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT source_id FROM items").fetchone()[0] == "manual"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='unfinished'").fetchall()
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT item_id FROM item_sources").fetchone()[0] == 23


def test_concurrent_migrations_read_version_under_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    _v9(path)
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, "CREATE TABLE once_only (id INTEGER); "])
    barrier = Barrier(8)

    def upgrade(_):
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            barrier.wait(timeout=10)
            db.migrate(conn)
            if "BEGIN IMMEDIATE" in statements:
                lock_index = statements.index("BEGIN IMMEDIATE")
                assert "PRAGMA user_version" in statements[lock_index + 1 :]
            else:
                # Another connection can already have committed the current version.
                assert statements == ["PRAGMA user_version"]
            return conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(upgrade, range(8))) == [len(db.MIGRATIONS)] * 8


def test_newer_schema_is_rejected_without_mutating_database(tmp_path):
    path = tmp_path / "too-new.db"
    version = len(db.MIGRATIONS) + 1
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(f"CREATE TABLE newer (value); PRAGMA user_version = {version};")
        with pytest.raises(sqlite3.DatabaseError, match=f"v{version}"):
            db.migrate(conn)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("newer",)]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert not conn.in_transaction


def test_transaction_controls_rejected_but_trigger_body_semicolons_work(monkeypatch):
    migration = """
    CREATE TABLE x (value);
    CREATE TABLE y (value);
    CREATE TRIGGER log_insert AFTER INSERT ON x BEGIN
        INSERT INTO y VALUES ('semi;colon');
        INSERT INTO y VALUES (new.value);
    END;
    INSERT INTO x VALUES ('hello');
    -- comment containing ; and COMMIT;
    """
    monkeypatch.setattr(db, "MIGRATIONS", [migration, "/* no escaping */ COMMIT;"])
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="不支持"):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not conn.execute("SELECT name FROM sqlite_master").fetchall()
        monkeypatch.setattr(db, "MIGRATIONS", [migration])
        db.migrate(conn)
        assert conn.execute("SELECT value FROM y").fetchall() == [("semi;colon",), ("hello",)]
