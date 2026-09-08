from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek.index_issues import IndexIssueStore
from docseek.index_root_state import IndexRootStateStore
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase


class IndexHealthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_show_verified_index_health_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            db = SearchDatabase(db_path)
            active_root = Path(temp_dir) / "active"
            paused_root = Path(temp_dir) / "paused"
            active_root.mkdir()
            paused_root.mkdir()
            db.add_index_root(str(active_root))
            db.add_index_root(str(paused_root))
            IndexRootStateStore(db).set_paused(paused_root, True)

            db.upsert_document(
                path=str((active_root / "guide.txt").resolve()),
                filename="guide.txt",
                extension=".txt",
                modified_time=1.0,
                size=10,
                content="客户服务",
            )
            IndexIssueStore(db_path).record(
                str((active_root / "broken.pdf").resolve()),
                "os_error",
                "temporary",
            )

            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertEqual(dialog.settings_tabs.count(), 3)
                self.assertEqual(
                    [dialog.settings_tabs.tabText(index) for index in range(3)],
                    ["索引范围", "文件规则", "存储与维护"],
                )
                self.assertIn("已索引 1 个文件", dialog.root_list.item(0).text())
                text = dialog.health_summary_label.text()
                self.assertIn("1 个文件", text)
                self.assertIn("目录 2（监测 1 / 暂停 1）", text)
                self.assertIn("问题 1", text)
                self.assertIn("SQLite 主库", dialog.health_hint_label.text())
                self.assertIn("暂停目录", dialog.health_hint_label.text())
            finally:
                dialog.close()

    def test_health_summary_refreshes_when_issue_summary_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            db = SearchDatabase(db_path)
            root = Path(temp_dir) / "root"
            root.mkdir()
            db.add_index_root(str(root))

            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertIn("问题 0", dialog.health_summary_label.text())
                dialog.issue_store.record(
                    str((root / "broken.docx").resolve()),
                    "os_error",
                    "temporary",
                )
                dialog._refresh_issue_summary()
                self.assertIn("问题 1", dialog.health_summary_label.text())
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
