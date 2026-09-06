from __future__ import annotations

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docseek import bootstrap
from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.index_maintenance import create_index_backup, stage_index_restore
from docseek.legacy_worker import iter_chunk_file
from docseek.search_db import SearchDatabase
from docseek.storage_location import DATABASE_FILENAME, stage_index_storage_move


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

    def test_product_startup_applies_staged_storage_move_before_app_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            control = base / "control"
            destination = base / "E-drive" / "DocSeekData"
            old_db = control / DATABASE_FILENAME
            config = control / "storage.json"
            pending = control / "storage-move-pending.json"

            database = SearchDatabase(old_db)
            store = ChunkStore(old_db)
            source = base / "policy.txt"
            source.write_text("产品启动后应继续搜索迁移索引", encoding="utf-8")
            stat = source.stat()
            database.add_index_root(str(base))
            store.replace_document(
                path=str(source.resolve()),
                filename=source.name,
                extension=".txt",
                modified_time=stat.st_mtime,
                size=stat.st_size,
                chunks=[DocumentChunk(0, "文本行 1-1", "产品启动后应继续搜索迁移索引")],
            )
            stage_index_storage_move(
                destination,
                config_path=config,
                pending_path=pending,
                default_dir=control,
            )

            old_app_dir = bootstrap.APP_DIR
            old_db_path = bootstrap.DB_PATH
            old_restore_error = bootstrap.RESTORE_ERROR_PATH
            old_storage_error = bootstrap.STORAGE_ERROR_PATH
            try:
                bootstrap.APP_DIR = control
                bootstrap.DB_PATH = old_db
                bootstrap.RESTORE_ERROR_PATH = control / "restore-error.txt"
                bootstrap.STORAGE_ERROR_PATH = control / "storage-error.txt"
                runtime_db = bootstrap._prepare_runtime_database()
            finally:
                bootstrap.APP_DIR = old_app_dir
                bootstrap.DB_PATH = old_db_path
                bootstrap.RESTORE_ERROR_PATH = old_restore_error
                bootstrap.STORAGE_ERROR_PATH = old_storage_error

            expected = destination.resolve() / DATABASE_FILENAME
            self.assertEqual(runtime_db, expected)
            self.assertFalse(old_db.exists())
            self.assertFalse(pending.exists())
            self.assertEqual(
                [row.filename for row in ChunkStore(expected).search("继续搜索迁移索引")],
                [source.name],
            )

    @staticmethod
    def _launcher_namespace():
        launcher = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "windows"
            / "docseek_launcher.py"
        )
        return runpy.run_path(str(launcher), run_name="docseek_launcher_test")

    def test_windows_launcher_smoke_branch_imports_bootstrap_and_maintenance(self) -> None:
        namespace = self._launcher_namespace()
        previous = os.environ.get("DOCSEEK_FROZEN_SMOKE")
        os.environ["DOCSEEK_FROZEN_SMOKE"] = "1"
        try:
            self.assertEqual(namespace["main"](), 0)
        finally:
            if previous is None:
                os.environ.pop("DOCSEEK_FROZEN_SMOKE", None)
            else:
                os.environ["DOCSEEK_FROZEN_SMOKE"] = previous

    def test_windows_launcher_can_run_hidden_extract_worker(self) -> None:
        namespace = self._launcher_namespace()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "sample.txt"
            output = Path(temp_dir) / "chunks.bin"
            source.write_text("隐藏 worker 正文", encoding="utf-8")
            argv = [
                "DocSeek.exe",
                "--docseek-extract-worker",
                str(source),
                str(output),
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(namespace["main"](), 0)
            self.assertTrue(output.exists())
            self.assertIn(
                "隐藏 worker 正文",
                "\n".join(chunk.content for chunk in iter_chunk_file(output)),
            )


if __name__ == "__main__":
    unittest.main()
