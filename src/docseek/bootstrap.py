from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile

from .index_maintenance import apply_pending_restore
from .storage_location import (
    StorageLocationError,
    apply_pending_index_storage_move,
    configured_database_path,
)


# Keep small control-plane files in the user's profile even when the large
# SQLite index is moved to another local disk. This gives every build a stable
# place to find the storage pointer and process lock without forcing the actual
# extracted-content index to stay on C:.
APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"  # legacy/default path; resolved again at startup
RESTORE_ERROR_PATH = APP_DIR / "restore-error.txt"
STORAGE_ERROR_PATH = APP_DIR / "storage-error.txt"
INSTANCE_LOCK_PATH = APP_DIR / "docseek.lock"


def _storage_config_path() -> Path:
    return APP_DIR / "storage.json"


def _pending_storage_move_path() -> Path:
    return APP_DIR / "storage-move-pending.json"


def _resolve_configured_database_path() -> Path:
    return configured_database_path(
        config_path=_storage_config_path(),
        default_dir=APP_DIR,
    )


def _write_storage_error(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_ERROR_PATH.write_text(message.rstrip() + "\n", encoding="utf-8")


def _notify_storage_problem(message: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "DocSeek 索引位置", 0x30)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _apply_restore_before_startup() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = apply_pending_restore(DB_PATH)
    except Exception as exc:
        # Do not make a failed staged restore prevent access to the current
        # index. Keep the pending file in place for diagnosis/retry and leave a
        # local text record because frozen/windowed builds may have no console.
        RESTORE_ERROR_PATH.write_text(
            "DocSeek 未能应用已暂存的索引恢复。\n"
            f"{type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return

    if result is not None:
        RESTORE_ERROR_PATH.unlink(missing_ok=True)


def _prepare_runtime_database() -> Path | None:
    """Resolve restore/migration work before the desktop opens SQLite.

    Order matters: an already-staged restore belongs to the currently configured
    database, so apply it first. A staged storage move then copies that known-good
    current index to the new location. The app is imported only after this method
    returns, ensuring no persistent reader/writer can race with either operation.
    """
    global DB_PATH

    try:
        DB_PATH = _resolve_configured_database_path()
    except StorageLocationError as exc:
        message = (
            "DocSeek 无法读取索引数据位置配置，为避免误建一个新的空索引，本次没有启动。\n\n"
            f"{exc}\n\n"
            f"请检查：{_storage_config_path()}"
        )
        _write_storage_error(message)
        _notify_storage_problem(message)
        return None

    _apply_restore_before_startup()

    move_warning: str | None = None
    try:
        move_result = apply_pending_index_storage_move(
            config_path=_storage_config_path(),
            pending_path=_pending_storage_move_path(),
            default_dir=APP_DIR,
        )
    except Exception as exc:
        # The migration backend switches the location config only after the new
        # DB has been validated. Therefore a failed move can safely continue on
        # the old configured database while leaving the pending request for a
        # later retry/diagnosis.
        move_warning = (
            "DocSeek 未能迁移索引数据位置，本次继续使用原位置。\n\n"
            f"{type(exc).__name__}: {exc}"
        )
        _write_storage_error(move_warning)
    else:
        if move_result is not None and (
            not move_result.old_files_removed
            or not move_result.pending_request_removed
        ):
            warnings: list[str] = []
            if not move_result.old_files_removed:
                warnings.append("旧位置的部分数据库文件未能自动删除")
            if not move_result.pending_request_removed:
                warnings.append("待迁移标记暂时无法清理，将在下次启动重试")
            move_warning = (
                "索引已成功切换到新位置，但" + "；".join(warnings) + "。\n"
                "新索引可以正常使用。"
            )
            _write_storage_error(move_warning)
        else:
            STORAGE_ERROR_PATH.unlink(missing_ok=True)

    try:
        DB_PATH = _resolve_configured_database_path()
    except StorageLocationError as exc:
        message = (
            "DocSeek 在准备索引位置后无法重新读取配置，为避免使用错误数据库，本次没有启动。\n\n"
            f"{exc}"
        )
        _write_storage_error(message)
        _notify_storage_problem(message)
        return None

    if move_warning:
        _notify_storage_problem(move_warning)
    return DB_PATH


def _acquire_instance_lock() -> QLockFile | None:
    """Keep one DocSeek process per user control profile.

    Portable and installed builds intentionally share the same user index and
    storage pointer. The lock stays in the small control directory even when the
    large SQLite database lives on another local drive.
    """

    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(INSTANCE_LOCK_PATH))
    lock.setStaleLockTime(30_000)
    if lock.tryLock(100):
        return lock
    return None


def _notify_already_running() -> None:
    message = "DocSeek 已在运行。请切换到已有窗口，不要同时启动多个实例。"
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "DocSeek", 0x40)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def main() -> None:
    instance_lock = _acquire_instance_lock()
    if instance_lock is None:
        _notify_already_running()
        return

    try:
        runtime_db = _prepare_runtime_database()
        if runtime_db is None:
            return

        # Import after restore/storage migration. app.py intentionally exposes a
        # patchable DB_PATH for tests and frozen builds; set that single runtime
        # value before MainWindow creates any SearchDatabase/ChunkStore handles.
        from . import app as app_module

        app_module.DB_PATH = runtime_db
        app_module.app_base.DB_PATH = runtime_db
        app_module.main()
    finally:
        instance_lock.unlock()


if __name__ == "__main__":
    main()
