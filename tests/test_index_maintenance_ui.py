from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase
from docseek.settings_dialog import IndexSettingsDialog


class IndexMaintenanceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_expose_safe_backup_rebuild_reset_and_restore_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SearchDatabase(Path(temp_dir) / "docseek.db")
            ChunkStore(db.db_path)
            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertEqual(dialog.backup_button.text(), "备份索引…")
                self.assertEqual(dialog.rebuild_button.text(), "重建索引…")
                self.assertEqual(dialog.reset_button.text(), "清空索引…")
                self.assertEqual(dialog.restore_button.text(), "恢复备份…")
                hint = " ".join(
                    label.text()
                    for label in dialog.maintenance_group.findChildren(type(dialog.maintenance_status_label))
                )
                self.assertIn("绝不会删除源文件", hint)
                self.assertIn("下次启动", hint)
                self.assertTrue(hasattr(dialog, "button_box"))
            finally:
                dialog.close()

    def test_removing_root_purges_production_chunk_index_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            root = base / "docs"
            root.mkdir()
            source = root / "manual.txt"
            source.write_text("信贷业务操作说明", encoding="utf-8")

            db = SearchDatabase(db_path)
            db.add_index_root(str(root))
            store = ChunkStore(db_path)
            stat = source.stat()
            store.replace_document(
                path=str(source.resolve()),
                filename=source.name,
                extension=".txt",
                modified_time=stat.st_mtime,
                size=stat.st_size,
                chunks=[DocumentChunk(0, "文本行 1-1", "信贷业务操作说明")],
            )
            self.assertEqual([row.filename for row in store.search("信贷业务")], ["manual.txt"])

            dialog = IndexSettingsDialog(db)
            try:
                dialog.root_list.setCurrentRow(0)
                dialog._remove_selected_root()
                dialog.accept()
            finally:
                dialog.close()

            self.assertTrue(source.exists())
            self.assertEqual(SearchDatabase(db_path).get_index_roots(), [])
            self.assertEqual(ChunkStore(db_path).search("信贷业务"), [])


if __name__ == "__main__":
    unittest.main()
