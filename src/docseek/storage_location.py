from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .index_maintenance import create_index_backup, validate_index_backup


CONTROL_DIR = Path.home() / ".docseek"
DEFAULT_INDEX_DATA_DIR = CONTROL_DIR
STORAGE_CONFIG_PATH = CONTROL_DIR / "storage.json"
PENDING_STORAGE_MOVE_PATH = CONTROL_DIR / "storage-move-pending.json"
DATABASE_FILENAME = "docseek.db"


class StorageLocationError(RuntimeError):
    """Raised when DocSeek cannot safely resolve or migrate index storage."""


@dataclass(frozen=True, slots=True)
class StorageMoveResult:
    previous_db_path: Path
    current_db_path: Path
    moved_existing_index: bool
    old_files_removed: bool
    pending_request_removed: bool


def _normalized_directory(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageLocationError(f"无法读取 DocSeek 索引位置配置：{exc}") from exc
    if not isinstance(value, dict):
        raise StorageLocationError("DocSeek 索引位置配置格式无效。")
    return value


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def configured_index_data_dir(
    *,
    config_path: str | Path = STORAGE_CONFIG_PATH,
    default_dir: str | Path = DEFAULT_INDEX_DATA_DIR,
) -> Path:
    """Resolve the configured data directory without silently losing a bad config."""
    config = Path(config_path)
    default = _normalized_directory(default_dir)
    if not config.exists():
        return default

    payload = _read_json_object(config)
    raw = payload.get("index_data_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise StorageLocationError("DocSeek 索引位置配置缺少 index_data_dir。")
    return _normalized_directory(raw)


def configured_database_path(
    *,
    config_path: str | Path = STORAGE_CONFIG_PATH,
    default_dir: str | Path = DEFAULT_INDEX_DATA_DIR,
) -> Path:
    return configured_index_data_dir(
        config_path=config_path,
        default_dir=default_dir,
    ) / DATABASE_FILENAME


def save_index_data_dir(
    directory: str | Path,
    *,
    config_path: str | Path = STORAGE_CONFIG_PATH,
) -> Path:
    destination = _normalized_directory(directory)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageLocationError(f"无法创建索引数据目录：{exc}") from exc

    try:
        _atomic_write_json(
            Path(config_path),
            {"version": 1, "index_data_dir": str(destination)},
        )
    except OSError as exc:
        raise StorageLocationError(f"无法保存索引位置配置：{exc}") from exc
    return destination


def stage_index_storage_move(
    destination_dir: str | Path,
    *,
    config_path: str | Path = STORAGE_CONFIG_PATH,
    pending_path: str | Path = PENDING_STORAGE_MOVE_PATH,
    default_dir: str | Path = DEFAULT_INDEX_DATA_DIR,
) -> Path:
    """Record a requested move for next startup, before the database is opened."""
    destination = _normalized_directory(destination_dir)
    current = configured_index_data_dir(
        config_path=config_path,
        default_dir=default_dir,
    )
    pending = Path(pending_path)

    if destination == current:
        pending.unlink(missing_ok=True)
        return destination

    try:
        destination.mkdir(parents=True, exist_ok=True)
        probe = destination / f".docseek-write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise StorageLocationError(f"所选索引位置不可写：{exc}") from exc

    target_db = destination / DATABASE_FILENAME
    if target_db.exists():
        raise StorageLocationError(
            f"目标目录已存在 {DATABASE_FILENAME}，为避免覆盖未知索引，未安排迁移。"
        )

    try:
        _atomic_write_json(
            pending,
            {"version": 1, "destination_dir": str(destination)},
        )
    except OSError as exc:
        raise StorageLocationError(f"无法记录待迁移索引位置：{exc}") from exc
    return destination


def pending_index_storage_move(
    *,
    pending_path: str | Path = PENDING_STORAGE_MOVE_PATH,
) -> Path | None:
    pending = Path(pending_path)
    if not pending.exists():
        return None
    payload = _read_json_object(pending)
    raw = payload.get("destination_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise StorageLocationError("待迁移索引位置配置缺少 destination_dir。")
    return _normalized_directory(raw)


def _remove_database_family(db_path: Path) -> bool:
    complete = True
    for candidate in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            complete = False
    return complete


def apply_pending_index_storage_move(
    *,
    config_path: str | Path = STORAGE_CONFIG_PATH,
    pending_path: str | Path = PENDING_STORAGE_MOVE_PATH,
    default_dir: str | Path = DEFAULT_INDEX_DATA_DIR,
) -> StorageMoveResult | None:
    """Apply a staged move before the desktop app opens SQLite.

    Existing indexes are copied with SQLite's backup API into a temporary target,
    validated, atomically promoted, and only then made current in the small
    location config. If anything before the config switch fails, the old index
    remains authoritative and the pending request is kept for diagnosis/retry.
    """
    pending = Path(pending_path)
    destination = pending_index_storage_move(pending_path=pending)
    if destination is None:
        return None

    current_dir = configured_index_data_dir(
        config_path=config_path,
        default_dir=default_dir,
    )
    previous_db = current_dir / DATABASE_FILENAME
    current_db = destination / DATABASE_FILENAME

    if destination == current_dir:
        try:
            pending.unlink(missing_ok=True)
            pending_removed = True
        except OSError:
            pending_removed = False
        return StorageMoveResult(
            previous_db_path=previous_db,
            current_db_path=previous_db,
            moved_existing_index=False,
            old_files_removed=True,
            pending_request_removed=pending_removed,
        )

    destination.mkdir(parents=True, exist_ok=True)
    if current_db.exists():
        raise StorageLocationError(
            f"目标索引数据库已存在：{current_db}。未覆盖现有文件。"
        )

    moved_existing = previous_db.exists()
    staged_db: Path | None = None
    promoted = False
    try:
        if moved_existing:
            staged_db = destination / f".{DATABASE_FILENAME}.migrating-{uuid.uuid4().hex}"
            create_index_backup(previous_db, staged_db, label="storage-move")
            validate_index_backup(staged_db)
            os.replace(staged_db, current_db)
            promoted = True

        # Persist the new location only after the destination database is known
        # good. If there was no database yet, this simply changes where the first
        # index will be created.
        save_index_data_dir(destination, config_path=config_path)
    except Exception:
        if staged_db is not None:
            try:
                staged_db.unlink(missing_ok=True)
            except OSError:
                pass
        if promoted:
            try:
                current_db.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    # Saving the location config is the migration commit point. From here on,
    # the new database is authoritative and must never be removed merely
    # because cleanup of the retry marker fails (for example due to antivirus
    # or a transient Windows sharing violation). A leftover marker is harmless:
    # the next startup sees that the destination is already current and retries
    # removing it without copying or deleting either database.
    try:
        pending.unlink(missing_ok=True)
        pending_removed = True
    except OSError:
        pending_removed = False

    old_removed = True
    if moved_existing and previous_db != current_db:
        old_removed = _remove_database_family(previous_db)

    return StorageMoveResult(
        previous_db_path=previous_db,
        current_db_path=current_db,
        moved_existing_index=moved_existing,
        old_files_removed=old_removed,
        pending_request_removed=pending_removed,
    )
