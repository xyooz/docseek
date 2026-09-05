from __future__ import annotations

import os
import runpy
import tempfile
import unittest
from pathlib import Path

from docseek import bootstrap
from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.index_maintenance import create_index_backup, stage_index_restore
from docseek.search_db import SearchDatabase


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_applies_staged_restore_before_app_database_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            db = SearchDatabase(db_path)
            store = ChunkStore(db_path)
            root = base / "docs"
            root.mkdir()
            original = root / "original.txt"
            original.write_text("恢复前的制度正文", encoding="utf-8")
            stat = original.stat()
            db.add_index_root(str(root))
            store.replace_document(
                path=str(original.resolve()),
                filename=original.name,
                extension=".txt",
                modified_time=stat.st_mtime,
                size=stat.st_size,
                chunks=[DocumentChunk(0, "文本行 1-1", "恢复前的制度正文")],
            )
            backup = create_index_backup(db_path, base / "known-good.db")

            store.remove_document(str(original.resolve()))
            changed = root / "changed.txt"
            changed.write_text("临时索引内容", encoding="utf-8")
            changed_stat = changed.stat()
            store.replace_document(
                path=str(changed.resolve()),
                filename=changed.name,
                extension=".txt",
                modified_time=changed_stat.st_mtime,
                size=changed_stat.st_size,
                chunks=[DocumentChunk(0, "文本行 1-1", "临时索引内容")],
            )
            stage_index_restore(db_path, backup)

            old_app_dir = bootstrap.APP_DIR
            old_db_path = bootstrap.DB_PATH
            old_error_path = bootstrap.RESTORE_ERROR_PATH
            bootstrap.APP_DIR = base
            bootstrap.DB_PATH = db_path
            bootstrap.RESTORE_ERROR_PATH = base / "restore-error.txt"
            try:
                bootstrap._apply_restore_before_startup()
            finally:
                bootstrap.APP_DIR = old_app_dir
                bootstrap.DB_PATH = old_db_path
                bootstrap.RESTORE_ERROR_PATH = old_error_path

            restored = ChunkStore(db_path)
            self.assertEqual(
                [row.filename for row in restored.search("制度正文")],
                ["original.txt"],
            )
            self.assertEqual(restored.search("临时索引内容"), [])
            self.assertFalse((base / "restore-error.txt").exists())

    def test_windows_launcher_smoke_branch_imports_bootstrap_and_maintenance(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "windows"
            / "docseek_launcher.py"
        )
        namespace = runpy.run_path(str(launcher), run_name="docseek_launcher_test")
        previous = os.environ.get("DOCSEEK_FROZEN_SMOKE")
        os.environ["DOCSEEK_FROZEN_SMOKE"] = "1"
        try:
            self.assertEqual(namespace["main"](), 0)
        finally:
            if previous is None:
                os.environ.pop("DOCSEEK_FROZEN_SMOKE", None)
            else:
                os.environ["DOCSEEK_FROZEN_SMOKE"] = previous


if __name__ == "__main__":
    unittest.main()
