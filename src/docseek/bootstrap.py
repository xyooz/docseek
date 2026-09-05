from __future__ import annotations

from pathlib import Path

from .index_maintenance import apply_pending_restore


APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"
RESTORE_ERROR_PATH = APP_DIR / "restore-error.txt"


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


def main() -> None:
    _apply_restore_before_startup()
    from .app import main as run_docseek

    run_docseek()


if __name__ == "__main__":
    main()
