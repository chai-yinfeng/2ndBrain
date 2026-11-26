"""显式离线工作区快照；不启动服务、加载环境文件或执行数据库迁移。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from secondbrain import config

_MANIFEST = "manifest.json"
_FORMAT = "secondbrain-workspace-v1"
_ROOTS = ("data", "config")
_DATABASES = ("brain.db", "web_tasks.db")
_SIDECARS = ("-wal", "-shm", "-journal")
_LOCKS = {"data/run.lock", "data/wiki_rewrite/operation.lock", "config/sources.lock"}
_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class WorkspaceError(Exception):
    """无法安全完成备份或恢复；消息可以显示在 CLI。"""


def _require_offline(offline: bool) -> None:
    if offline is not True:
        raise WorkspaceError("请先停止 Web、定时任务、CLI 和 vault 写入，再明确确认离线操作")


def _directory(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise WorkspaceError("工作区或目标父目录不能是符号链接")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise WorkspaceError("工作区和目标父目录必须是已存在的目录")
    return path


def _target(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.name in {"", ".", ".."}:
        raise WorkspaceError("请指定新的目标目录名称")
    path = _directory(path.parent) / path.name
    if os.path.lexists(path):
        raise WorkspaceError("目标目录已存在；不会覆盖")
    return path


def _contains(root: Path, path: Path) -> bool:
    # samefile 也识别大小写不敏感文件系统或其他路径别名。
    return any(root.samefile(part) for part in (path, *path.parents))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _excluded(name: str) -> bool:
    name = name.casefold()
    return (
        name in {"backups", ".backups", "migration-backups", ".git", "secrets", "credentials"}
        or name == ".env"
        or name.startswith(".env.")
        or name.startswith(("credentials.", "secrets.", "id_rsa", "id_ed25519"))
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
    )


@contextmanager
def _fd(path: str | Path, flags: int, *, parent: int | None = None):
    fd = os.open(path, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _hold_lock(stack: ExitStack, directory: int, name: str, info: os.stat_result) -> None:
    _check_file(info)
    fd = stack.enter_context(_fd(name, _FILE_FLAGS, parent=directory))
    if _stamp(os.fstat(fd)) != _stamp(info):
        raise WorkspaceError("离线检查期间锁文件有变化")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise WorkspaceError("仍有工作区写入任务运行；请完全停服后再试") from exc


def _check_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspaceError("工作区含符号链接、硬链接或非常规文件；拒绝复制")


def _copy_file(directory: int, name: str, info: os.stat_result, destination: Path) -> None:
    with _fd(name, _FILE_FLAGS, parent=directory) as fd:
        if _stamp(os.fstat(fd)) != _stamp(info):
            raise WorkspaceError("复制前文件已变化；请完全停服后再试")
        with os.fdopen(fd, "rb", closefd=False) as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
        if _stamp(os.fstat(fd)) != _stamp(info):
            raise WorkspaceError("复制期间文件已变化；请完全停服后再试")


def _scan(
    source: Path,
    prefix: str,
    destination: Path | None = None,
    *,
    backup: bool = False,
    env_identity: tuple[int, int] | None = None,
    locks: ExitStack | None = None,
    db_stage: Path | None = None,
    forbidden: set[tuple[int, int]] | None = None,
) -> dict[str, tuple[int, ...]]:
    """沿打开的目录描述符遍历，不跟随树内链接；复制前后比较来源元数据。"""
    result: dict[str, tuple[int, ...]] = {}
    visited: set[tuple[int, int]] = set()

    def walk(directory: int, rel: str, output: Path | None, device: int) -> None:
        current = os.fstat(directory)
        key = _identity(current)
        if current.st_dev != device or key in visited or (forbidden and key in forbidden):
            raise WorkspaceError("工作区目录跨越物理边界或存在重复目录")
        visited.add(key)
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        names = {entry.name for entry in children}
        if rel != prefix and names.issuperset({_MANIFEST, *_ROOTS}):
            if backup:
                return
            raise WorkspaceError("备份内部包含另一个备份")
        if output is not None:
            output.mkdir(mode=0o700)
        result[rel] = _stamp(current)
        for entry in children:
            child = f"{rel}/{entry.name}"
            info = entry.stat(follow_symlinks=False)
            if _excluded(entry.name) or _identity(info) == env_identity:
                if backup:
                    continue
                raise WorkspaceError("备份包含被排除的密钥或备份路径")
            if child in _LOCKS:
                if not backup:
                    raise WorkspaceError("备份包含运行锁文件")
                if locks is not None:
                    _hold_lock(locks, directory, entry.name, info)
                continue
            is_database = rel == "data" and entry.name in _DATABASES
            is_sidecar = rel == "data" and any(
                entry.name == db + suffix for db in _DATABASES for suffix in _SIDECARS
            )
            if is_sidecar and not backup:
                raise WorkspaceError("备份含 SQLite 辅助文件，不能独立恢复")
            if stat.S_ISDIR(info.st_mode) and not (is_database or is_sidecar):
                with _fd(entry.name, _DIR_FLAGS, parent=directory) as fd:
                    if _stamp(os.fstat(fd)) != _stamp(info):
                        raise WorkspaceError("复制前目录已变化；请完全停服后再试")
                    walk(fd, child, output / entry.name if output is not None else None, device)
                continue
            _check_file(info)
            if info.st_dev != device:
                raise WorkspaceError("工作区文件跨越物理边界")
            result[child] = _stamp(info)
            if output is not None:
                if backup and (is_database or is_sidecar):
                    if not entry.name.endswith("-shm"):
                        assert db_stage is not None
                        _copy_file(directory, entry.name, info, db_stage / entry.name)
                else:
                    _copy_file(directory, entry.name, info, output / entry.name)

    with _fd(source, _DIR_FLAGS) as root:
        walk(root, prefix, destination, os.fstat(root).st_dev)
    return result


def _sqlite_check(path: Path) -> int:
    # 备份数据库必须自含；immutable 不在验证目录创建 WAL/SHM。
    if path.stat().st_size < 100:
        raise WorkspaceError(f"{path.name} 不是完整的 SQLite 数据库")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise WorkspaceError(f"{path.name} 未通过 SQLite 完整性检查")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkspaceError(f"{path.name} 未通过 SQLite 外键一致性检查")
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _snapshot_databases(stage: Path, dest: Path) -> None:
    for name in _DATABASES:
        source = stage / name
        if not source.exists():
            if name == "brain.db" or any((stage / (name + s)).exists() for s in _SIDECARS):
                raise WorkspaceError(f"工作区缺少 {name}")
            continue
        if source.stat().st_size < 100:
            raise WorkspaceError(f"{name} 不是完整的 SQLite 数据库")
        # 只在隔离副本上做 SQLite 恢复和备份；源 workspace 不会创建 WAL/SHM。
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=rw", uri=True)) as incoming,
            closing(sqlite3.connect(dest / name)) as outgoing,
        ):
            incoming.backup(outgoing)
            if outgoing.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                raise WorkspaceError(f"无法创建自含的 SQLite 备份：{name}")


def _manifest(root: Path) -> dict:
    files, directories, versions = {}, [], {}
    for name in _ROOTS:
        for rel, info in sorted(_scan(root / name, name).items()):
            if stat.S_ISDIR(info[-1]):
                directories.append(rel)
                continue
            with (root / rel).open("rb") as handle:
                files[rel] = {
                    "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
                    "size": info[2],
                }
    for name in _DATABASES:
        rel = f"data/{name}"
        if rel in files:
            versions[rel] = _sqlite_check(root / rel)
        elif name == "brain.db":
            raise WorkspaceError("备份缺少 data/brain.db")
    return {
        "format": _FORMAT,
        "directories": directories,
        "files": files,
        "sqlite_user_versions": versions,
    }


@contextmanager
def _new_directory(path: Path):
    # mkdir 是保留目标路径的排他操作，绝不 rename/replace 覆盖已有目录。
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise WorkspaceError("目标目录已存在；不会覆盖") from exc
    created = path.stat()
    try:
        yield
    except BaseException:
        try:
            if path.resolve() == path and _identity(path.lstat()) == _identity(created):
                shutil.rmtree(path)
        except FileNotFoundError:
            pass
        raise


def _env_identity() -> tuple[int, int] | None:
    # 只比较 inode，从不读取自定义 SB_ENV_FILE 的内容。
    try:
        return _identity(config.env_file().stat())
    except FileNotFoundError:
        return None


def backup_workspace(
    data_dir: Path, config_dir: Path, destination: Path, *, offline: bool = False
) -> Path:
    """复制离线 data/config 至新目录；manifest 最后写入。"""
    _require_offline(offline)
    try:
        data, settings, target = _directory(data_dir), _directory(config_dir), _target(destination)
        if (
            _contains(data, settings)
            or _contains(settings, data)
            or any(_contains(root, target.parent) for root in (data, settings))
        ):
            raise WorkspaceError("data/config 必须独立，备份目标不能位于源目录内")
        env_id = _env_identity()
        with _new_directory(target), ExitStack() as locks:
            with tempfile.TemporaryDirectory(prefix=".sqlite-", dir=target) as staging:
                stage = Path(staging)
                roots = {"data": data, "config": settings}
                forbidden = {_identity(target.stat())}
                original = {}
                for name, path in roots.items():
                    original[name] = _scan(
                        path,
                        name,
                        target / name,
                        backup=True,
                        env_identity=env_id,
                        locks=locks,
                        db_stage=stage,
                        forbidden=forbidden,
                    )
                _snapshot_databases(stage, target / "data")
                for name, path in roots.items():
                    if original[name] != _scan(
                        path,
                        name,
                        backup=True,
                        env_identity=env_id,
                        forbidden=forbidden,
                    ):
                        raise WorkspaceError("备份期间工作区已变化；请完全停服后再试")
            with (target / _MANIFEST).open("x", encoding="utf-8") as handle:
                json.dump(_manifest(target), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
        return target
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise WorkspaceError(f"备份失败：{exc}") from exc


def restore_workspace(backup_dir: Path, destination: Path, *, offline: bool = False) -> Path:
    """严格校验清单与两套目录，恢复到不存在的目标；不覆盖、不迁移数据库。"""
    _require_offline(offline)
    try:
        source, target = _directory(backup_dir), _target(destination)
        if any(source.samefile(part) for part in (target.parent, *target.parent.parents)):
            raise WorkspaceError("恢复目标不能位于备份目录内")
        with _fd(source, _DIR_FLAGS) as root:
            if set(os.listdir(root)) != {_MANIFEST, *_ROOTS}:
                raise WorkspaceError("无效备份目录：必须恰含 data、config、manifest.json")
            with _fd(_MANIFEST, _FILE_FLAGS, parent=root) as manifest_fd:
                _check_file(os.fstat(manifest_fd))
                with os.fdopen(manifest_fd, encoding="utf-8", closefd=False) as handle:
                    expected = json.load(handle)
        if not isinstance(expected, dict) or expected.get("format") != _FORMAT:
            raise WorkspaceError("不支持的备份清单格式")
        with _new_directory(target):
            for name in _ROOTS:
                _scan(source / name, name, target / name, forbidden={_identity(target.stat())})
            actual = _manifest(target)
            if json.dumps(expected, sort_keys=True) != json.dumps(actual, sort_keys=True):
                raise WorkspaceError("备份与完整性清单不符；未恢复数据")
        return target
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        raise WorkspaceError(f"恢复失败：{exc}") from exc
