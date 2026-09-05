from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile

from .index_maintenance import apply_pending_restore


APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"
RESTORE_ERROR_PATH = APP_DIR / "restore-error.txt"
INSTANCE_LOCK_PATH = APP_DIR / "docseek.lock"


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


def _acquire_instance_lock() -> QLockFile | None:
    """Keep one DocSeek process per user index database.

    Portable and installed builds intentionally share ``~/.docseek``. Without
    a process-level guard, double-clicking twice can start two independent
    watchers/indexers that compete for the same SQLite writer lock.
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
        _apply_restore_before_startup()
        from .app import main as run_docseek

        run_docseek()
    finally:
        instance_lock.unlock()


if __name__ == "__main__":
    main()
