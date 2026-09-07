from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .chunk_store import ChunkStore
from .file_exclusions import FILE_EXCLUSION_PATTERNS_KEY
from .index_health import LAST_SUCCESSFUL_RECONCILE_AT_KEY
from .index_root_state import PAUSED_INDEX_ROOTS_KEY
from .schema import CURRENT_SCHEMA_VERSION, ensure_schema_compatible, get_schema_version


ACTION_BACKUP = "backup"
ACTION_REBUILD = "rebuild"
ACTION_RESET = "reset"
ACTION_STAGE_RESTORE = "stage_restore"

_PENDING_RESTORE_SUFFIX = ".restore-pending"
_REQUIRED_BASE_TABLES = {"files", "settings"}


class IndexMaintenanceError(RuntimeError):
    """Raised when a maintenance operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class BackupValidation:
    path: Path
    schema_version: int
    indexed_files: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    action: str
    backup_path: Path | None = None
    staged_restore_path: Path | None = None
    cleared_files: int = 0


@dataclass(frozen=True, slots=True)
class RestoreApplyResult:
    restored_path: Path
    safety_backup_path: Path | None
    schema_version: int


def default_backup_directory(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve().parent / "backups"


def pending_restore_path(db_path: str | Path) -> Path:
    db = Path(db_path).expanduser().resolve()
    return db.with_name(db.name + _PENDING_RESTORE_SUFFIX)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise IndexMaintenanceError("无法为索引备份生成唯一文件名。")


def _default_backup_path(db_path: Path, *, label: str) -> Path:
    safe_label = "".join(ch for ch in label if ch.isalnum() or ch in {"-", "_"}) or "backup"
    return _unique_path(
        default_backup_directory(db_path),
        f"docseek-{safe_label}-{_timestamp()}",
        ".db",
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def validate_index_backup(path: str | Path) -> BackupValidation:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        raise IndexMaintenanceError(f"索引备份不存在：{candidate}")

    try:
        uri = candidate.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except (OSError, sqlite3.Error) as exc:
        raise IndexMaintenanceError(f"无法打开索引备份：{exc}") from exc

    try:
        quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            raise IndexMaintenanceError(
                "索引备份未通过 SQLite 完整性检查：" + "; ".join(quick_check[:5])
            )

        version = get_schema_version(conn)
        if version <= 0:
            raise IndexMaintenanceError("所选文件不是可识别的 DocSeek 索引备份。")
        ensure_schema_compatible(conn)

        tables = _table_names(conn)
        if not _REQUIRED_BASE_TABLES.issubset(tables):
            raise IndexMaintenanceError("备份缺少 DocSeek 基础索引表。")
        if "chunks" not in tables and "chunk_fts" not in tables:
            raise IndexMaintenanceError("备份缺少 DocSeek 正文索引结构。")

        indexed_files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    except sqlite3.Error as exc:
        raise IndexMaintenanceError(f"索引备份校验失败：{exc}") from exc
    finally:
        conn.close()

    return BackupValidation(
        path=candidate,
        schema_version=version,
        indexed_files=indexed_files,
        size_bytes=candidate.stat().st_size,
    )


def create_index_backup(
    db_path: str | Path,
    destination: str | Path | None = None,
    *,
    label: str = "backup",
) -> Path:
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.exists():
        raise IndexMaintenanceError("当前还没有可备份的 DocSeek 索引数据库。")

    target = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else _default_backup_path(source_path, label=label)
    )
    if target == source_path:
        raise IndexMaintenanceError("备份文件不能覆盖正在使用的索引数据库。")
    target.parent.mkdir(parents=True, exist_ok=True)

    temp_target = target.with_name(target.name + f".tmp-{uuid.uuid4().hex}")
    try:
        source = sqlite3.connect(source_path, timeout=10)
        destination_conn = sqlite3.connect(temp_target, timeout=10)
        try:
            source.execute("PRAGMA busy_timeout=10000")
            source.backup(destination_conn)
            destination_conn.commit()
            # Make the backup a single self-contained file rather than leaving
            # a WAL dependency beside it.
            destination_conn.execute("PRAGMA journal_mode=DELETE")
            destination_conn.commit()
        finally:
            destination_conn.close()
            source.close()

        validate_index_backup(temp_target)
        os.replace(temp_target, target)
    except Exception:
        try:
            temp_target.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return target


def _path_under_root(path: str, root: Path) -> bool:
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        candidate = Path(path).absolute()
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _delete_legacy_fts_rows(
    conn: sqlite3.Connection,
    paths: list[str],
) -> None:
    if not paths:
        return
    tables = _table_names(conn)
    for table in ("file_fts", "file_fts_cjk2", "file_fts_tri"):
        if table not in tables:
            continue
        conn.executemany(
            f"DELETE FROM {table} WHERE path = ?",
            [(path,) for path in paths],
        )


def purge_root_index(db_path: str | Path, root: str | Path) -> int:
    """Remove one configured root from the production chunk index only.

    Source files are never touched. The operation intentionally reuses
    ChunkStore's canonical contentless-FTS deletion primitive so row tokens are
    removed correctly rather than leaving unreachable FTS entries behind.
    """

    db = Path(db_path).expanduser().resolve()
    root_path = Path(root).expanduser().resolve()
    store = ChunkStore(db)

    with store.connect() as conn:
        conn.execute("PRAGMA secure_delete=ON")
        rows = conn.execute("SELECT id, path FROM files ORDER BY id").fetchall()
        selected = [
            (int(row["id"]), str(row["path"]))
            for row in rows
            if _path_under_root(str(row["path"]), root_path)
        ]
        if not selected:
            return 0

        for file_id, path in selected:
            store._delete_chunks(conn, path, file_id=file_id)
        paths = [path for _file_id, path in selected]
        conn.executemany("DELETE FROM extraction_state WHERE path = ?", [(p,) for p in paths])
        conn.executemany("DELETE FROM files WHERE id = ?", [(file_id,) for file_id, _p in selected])
        _delete_legacy_fts_rows(conn, paths)

        tables = _table_names(conn)
        if "index_issues" in tables:
            issue_rows = conn.execute("SELECT path FROM index_issues").fetchall()
            issue_paths = [
                str(row["path"])
                for row in issue_rows
                if _path_under_root(str(row["path"]), root_path)
            ]
            conn.executemany(
                "DELETE FROM index_issues WHERE path = ?",
                [(path,) for path in issue_paths],
            )

    return len(selected)


def _clear_all_index_rows(db_path: Path) -> int:
    store = ChunkStore(db_path)
    with store.connect() as conn:
        conn.execute("PRAGMA secure_delete=ON")
        tables = _table_names(conn)
        count = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

        # Production FTS tables are contentless. FTS5's delete-all command is
        # the correct bulk operation and avoids decoding every stored chunk.
        for table in ("chunk_index", "chunk_index_cjk2"):
            if table in tables:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('delete-all')")

        if "chunks" in tables:
            conn.execute("DELETE FROM chunks")
        if "extraction_state" in tables:
            conn.execute("DELETE FROM extraction_state")
        if "files" in tables:
            conn.execute("DELETE FROM files")
        if "index_issues" in tables:
            conn.execute("DELETE FROM index_issues")

        for table in ("file_fts", "file_fts_cjk2", "file_fts_tri"):
            if table in tables:
                conn.execute(f"DELETE FROM {table}")

        conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (LAST_SUCCESSFUL_RECONCILE_AT_KEY,),
        )
    return count


def clear_index_content(db_path: str | Path) -> int:
    """Clear indexed documents while preserving configured scope/preferences."""

    return _clear_all_index_rows(Path(db_path).expanduser().resolve())


def reset_index_scope(db_path: str | Path) -> int:
    """Clear local index content and scope settings, never source documents."""

    db = Path(db_path).expanduser().resolve()
    cleared = _clear_all_index_rows(db)
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        conn.executemany(
            "DELETE FROM settings WHERE key = ?",
            [
                ("index_roots",),
                ("index_root",),
                ("excluded_paths",),
                (PAUSED_INDEX_ROOTS_KEY,),
                (FILE_EXCLUSION_PATTERNS_KEY,),
                (LAST_SUCCESSFUL_RECONCILE_AT_KEY,),
            ],
        )
        conn.commit()
    finally:
        # sqlite3.Connection's context manager only commits/rolls back; it does
        # not close the native handle. Explicit close is required on Windows so
        # reset does not leave docseek.db locked until garbage collection.
        conn.close()
    return cleared


def stage_index_restore(db_path: str | Path, backup_path: str | Path) -> Path:
    """Validate and stage a backup for replacement before the next app start."""

    db = Path(db_path).expanduser().resolve()
    source = validate_index_backup(backup_path).path
    staged = pending_restore_path(db)
    staged.parent.mkdir(parents=True, exist_ok=True)
    temp = staged.with_name(staged.name + f".tmp-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temp)
        validate_index_backup(temp)
        os.replace(temp, staged)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return staged


def _preserve_raw_database(db_path: Path) -> Path:
    backup_dir = default_backup_directory(db_path)
    target = _unique_path(
        backup_dir,
        f"docseek-pre-restore-raw-{_timestamp()}",
        ".db",
    )
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        source = Path(f"{db_path}{suffix}")
        if source.exists():
            shutil.copy2(source, Path(f"{target}{suffix}"))
    return target


def apply_pending_restore(db_path: str | Path) -> RestoreApplyResult | None:
    """Apply a staged restore before any normal DocSeek SQLite handle opens."""

    db = Path(db_path).expanduser().resolve()
    staged = pending_restore_path(db)
    if not staged.exists():
        return None

    validation = validate_index_backup(staged)
    safety_backup: Path | None = None
    if db.exists():
        try:
            safety_backup = create_index_backup(db, label="pre-restore")
        except Exception:
            # A corrupt current index is exactly when restore can be most useful.
            # Preserve the raw database set instead of blocking recovery.
            safety_backup = _preserve_raw_database(db)

    db.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, db)
    for suffix in ("-wal", "-shm"):
        try:
            Path(f"{db}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass

    return RestoreApplyResult(
        restored_path=db,
        safety_backup_path=safety_backup,
        schema_version=validation.schema_version,
    )


def perform_maintenance(
    action: str,
    db_path: str | Path,
    *,
    path: str | Path | None = None,
) -> MaintenanceResult:
    db = Path(db_path).expanduser().resolve()

    if action == ACTION_BACKUP:
        if path is None:
            raise IndexMaintenanceError("未指定备份保存位置。")
        backup = create_index_backup(db, path, label="manual")
        return MaintenanceResult(action=action, backup_path=backup)

    if action == ACTION_REBUILD:
        backup = create_index_backup(db, label="pre-rebuild")
        cleared = clear_index_content(db)
        return MaintenanceResult(
            action=action,
            backup_path=backup,
            cleared_files=cleared,
        )

    if action == ACTION_RESET:
        backup = create_index_backup(db, label="pre-reset")
        cleared = reset_index_scope(db)
        return MaintenanceResult(
            action=action,
            backup_path=backup,
            cleared_files=cleared,
        )

    if action == ACTION_STAGE_RESTORE:
        if path is None:
            raise IndexMaintenanceError("未选择需要恢复的索引备份。")
        staged = stage_index_restore(db, path)
        return MaintenanceResult(action=action, staged_restore_path=staged)

    raise IndexMaintenanceError(f"未知索引维护操作：{action}")
