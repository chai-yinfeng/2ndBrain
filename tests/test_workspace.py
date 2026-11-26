"""Offline workspace operations use only synthetic data/config trees."""

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest

from secondbrain import db
from secondbrain.workspace import WorkspaceError, backup_workspace, restore_workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path.resolve()
    data = root / "other location" / "state"
    config = root / "odd # configuration"
    (data / "wiki" / "resources").mkdir(parents=True)
    (data / "wiki" / "empty").mkdir()
    config.mkdir()
    (data / "wiki" / "resources" / "original.pdf").write_bytes(b"test-only bytes")
    (data / "wiki" / "note.md").write_text("# Keep", encoding="utf-8")
    (data / "profile.md").write_text("profile", encoding="utf-8")
    (config / "sources.yaml").write_text("sources: []", encoding="utf-8")
    (config / "settings.yaml").write_text("llm: {backend: fake}", encoding="utf-8")
    with closing(db.connect(data / "brain.db")):
        pass
    with closing(sqlite3.connect(data / "web_tasks.db")) as tasks:
        tasks.executescript("CREATE TABLE tasks (label); INSERT INTO tasks VALUES ('test');")
    return root, data, config


def test_backup_restore_preserves_both_roots_and_verifies_manifest(workspace, monkeypatch):
    root, data, config = workspace
    (config / ".env").write_text("TEST_KEY=placeholder", encoding="utf-8")
    (config / "custom-runtime").write_text("TEST_KEY=placeholder", encoding="utf-8")
    monkeypatch.setenv("SB_ENV_FILE", str(config / "custom-runtime"))
    (data / "wiki" / "test.pem").write_text("test placeholder", encoding="utf-8")
    (data / "migration-backups").mkdir()
    (data / "migration-backups" / "old").write_bytes(b"omit")
    nested = data / "arbitrary backup name"
    nested.mkdir()
    for entry in ("data", "config"):
        (nested / entry).mkdir()
    (nested / "manifest.json").write_text("{}", encoding="utf-8")
    backup = backup_workspace(data, config, root / "backup #1", offline=True)
    restored = restore_workspace(backup, root / "restored", offline=True)
    manifest = json.loads((backup / "manifest.json").read_bytes())
    assert manifest["sqlite_user_versions"] == {
        "data/brain.db": len(db.MIGRATIONS),
        "data/web_tasks.db": 0,
    }
    assert manifest["files"]["data/wiki/note.md"] == {
        "sha256": hashlib.sha256(b"# Keep").hexdigest(),
        "size": 6,
    }
    assert (restored / "data/wiki/empty").is_dir()
    assert (restored / "data/wiki/resources/original.pdf").read_bytes() == b"test-only bytes"
    assert (restored / "data/profile.md").read_text() == "profile"
    assert (restored / "config/sources.yaml").read_text() == "sources: []"
    assert {p.name for p in (restored / "config").iterdir()} == {"settings.yaml", "sources.yaml"}
    assert not (restored / "data/wiki/test.pem").exists()
    assert not (restored / "data/migration-backups").exists()
    assert not (restored / "data/arbitrary backup name").exists()
    with closing(sqlite3.connect(restored / "data/web_tasks.db")) as tasks:
        assert tasks.execute("SELECT label FROM tasks").fetchall() == [("test",)]


def test_offline_confirmation_and_existing_operational_locks(workspace):
    root, data, config = workspace
    with pytest.raises(WorkspaceError, match="离线"):
        backup_workspace(data, config, root / "backup")
    with pytest.raises(WorkspaceError, match="离线"):
        restore_workspace(root / "backup", root / "restored")
    with (data / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(WorkspaceError, match="任务运行"):
            backup_workspace(data, config, root / "backup", offline=True)
    assert not (root / "backup").exists()
    result = backup_workspace(data, config, root / "backup", offline=True)
    assert not (result / "data/run.lock").exists()


def test_no_existing_target_and_no_physical_overlap(workspace):
    root, data, config = workspace
    existing = root / "existing"
    existing.mkdir()
    (existing / "keep").write_bytes(b"untouched")
    for destination in (existing, root / "dangling"):
        if destination.name == "dangling":
            destination.symlink_to(root / "no such directory", target_is_directory=True)
        with pytest.raises(WorkspaceError, match="不会覆盖"):
            backup_workspace(data, config, destination, offline=True)
    assert (existing / "keep").read_bytes() == b"untouched"
    alias = root / "alias"
    alias.symlink_to(data, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="源目录内"):
        backup_workspace(data, config, alias / "wiki" / "backup", offline=True)
    with pytest.raises(WorkspaceError, match="独立"):
        backup_workspace(data, data / "wiki", root / "backup", offline=True)
    backup = backup_workspace(data, config, root / "backup", offline=True)
    with pytest.raises(WorkspaceError, match="不会覆盖"):
        restore_workspace(backup, existing, offline=True)
    with pytest.raises(WorkspaceError, match="不能位于备份目录"):
        restore_workspace(backup, backup / "data" / "child", offline=True)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_rejects_file_indirections_and_special_entries(workspace, kind):
    root, data, config = workspace
    endpoint = data / "wiki" / "unexpected"
    outside = root / "outside"
    outside.write_bytes(b"external")
    if kind == "symlink":
        endpoint.symlink_to(outside)
    elif kind == "hardlink":
        endpoint.hardlink_to(outside)
    else:
        os.mkfifo(endpoint)
    with pytest.raises(WorkspaceError, match="非常规文件"):
        backup_workspace(data, config, root / "backup", offline=True)
    assert not (root / "backup").exists()


def test_wal_only_commit_survives_and_source_files_are_unchanged(workspace):
    root, data, config = workspace
    # Preserve a WAL left by a former process; no live SQLite writer is needed during backup.
    source = data / "web_tasks.db"
    wal = data / "web_tasks.db-wal"
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("INSERT INTO tasks VALUES ('only-in-wal')")
        conn.commit()
        saved_main, saved_wal = source.read_bytes(), wal.read_bytes()
    source.write_bytes(saved_main)
    wal.write_bytes(saved_wal)
    backup = backup_workspace(data, config, root / "backup", offline=True)
    assert source.read_bytes() == saved_main
    assert wal.read_bytes() == saved_wal
    assert not (data / "web_tasks.db-shm").exists()
    assert not (backup / "data/web_tasks.db-wal").exists()
    restored = restore_workspace(backup, root / "restored", offline=True)
    with closing(sqlite3.connect(restored / "data/web_tasks.db")) as conn:
        assert conn.execute("SELECT label FROM tasks").fetchall() == [("test",), ("only-in-wal",)]


@pytest.mark.parametrize("tamper", ["edit", "missing", "extra", "link", "manifest-link", "path"])
def test_restore_rejects_tampering_without_leaving_partial_target(workspace, tamper):
    root, data, config = workspace
    backup = backup_workspace(data, config, root / "backup", offline=True)
    file = backup / "data/wiki/note.md"
    if tamper == "edit":
        file.write_bytes(b"# Fake")  # Same number of bytes; file hash must differ.
    elif tamper == "missing":
        file.unlink()
    elif tamper == "extra":
        file.with_suffix(".extra").write_bytes(b"extra")
    elif tamper == "link":
        file.unlink()
        file.symlink_to(data / "wiki/note.md")
    elif tamper == "manifest-link":
        manifest = backup / "manifest.json"
        manifest.rename(root / "outside.json")
        manifest.symlink_to(root / "outside.json")
    else:
        manifest = backup / "manifest.json"
        expected = json.loads(manifest.read_bytes())
        expected["files"]["../escape"] = expected["files"].pop("data/wiki/note.md")
        manifest.write_text(json.dumps(expected))
    with pytest.raises(WorkspaceError):
        restore_workspace(backup, root / "restored", offline=True)
    assert not (root / "restored").exists()
    assert not (root / "escape").exists()


@pytest.mark.parametrize("db_name", ["brain.db", "web_tasks.db"])
def test_sqlite_integrity_is_checked_during_backup_and_restore(workspace, db_name):
    root, data, config = workspace
    backup = backup_workspace(data, config, root / "backup", offline=True)
    rel = f"data/{db_name}"
    corrupt = b"SQLite format 3\x00" + b"x" * 1024
    (backup / rel).write_bytes(corrupt)
    manifest = backup / "manifest.json"
    expected = json.loads(manifest.read_bytes())
    expected["files"][rel] = {"size": len(corrupt), "sha256": hashlib.sha256(corrupt).hexdigest()}
    manifest.write_text(json.dumps(expected))
    with pytest.raises(WorkspaceError):
        restore_workspace(backup, root / "restored", offline=True)
    (data / db_name).write_bytes(corrupt)
    with pytest.raises(WorkspaceError):
        backup_workspace(data, config, root / "bad", offline=True)
    assert not (root / "restored").exists()
    assert not (root / "bad").exists()


def test_sqlite_foreign_keys_and_required_storage_are_checked(workspace):
    root, data, config = workspace
    with closing(sqlite3.connect(data / "brain.db")) as conn:
        conn.executescript("INSERT INTO feedback (item_id,kind,ts) VALUES (999,'later','now');")
    with pytest.raises(WorkspaceError, match="外键不一致|外键一致性"):
        backup_workspace(data, config, root / "bad-fk", offline=True)
    (data / "brain.db").unlink()
    with pytest.raises(WorkspaceError, match="缺少 brain.db"):
        backup_workspace(data, config, root / "missing", offline=True)
