from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek import app as app_module
from docseek.search_db import SearchDatabase


class FirstRunUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_empty_profile_focuses_on_choose_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    self.assertFalse(window.first_run_panel.isHidden())
                    self.assertTrue(window.content_splitter.isHidden())
                    self.assertFalse(window.search_input.isEnabled())
                    self.assertTrue(window.search_bar_panel.isHidden())
                    self.assertTrue(window.workspace_bar_panel.isHidden())
                    self.assertTrue(window.type_filter.isHidden())
                    self.assertTrue(window.sort_filter.isHidden())
                    self.assertTrue(window.history_button.isHidden())
                    self.assertTrue(window.favorite_button.isHidden())
                    self.assertTrue(window.refresh_button.isHidden())
                    self.assertTrue(window.settings_button.isHidden())
                    self.assertIn("文件名或正文关键词", window.search_input.placeholderText())
                    self.assertEqual(window.first_run_button.text(), "选择资料目录")
                    self.assertEqual(
                        window.first_run_storage_button.text(),
                        "先设置索引保存位置…",
                    )
                finally:
                    window.close()

    def test_existing_profile_skips_first_run_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            root = base / "docs"
            root.mkdir()
            SearchDatabase(db_path).add_index_root(str(root.resolve()))

            with (
                patch.object(app_module, "DB_PATH", db_path),
                patch.object(app_module.QTimer, "singleShot"),
            ):
                window = app_module.MainWindow()
                try:
                    self.assertTrue(window.first_run_panel.isHidden())
                    self.assertFalse(window.content_splitter.isHidden())
                    self.assertTrue(window.search_input.isEnabled())
                    self.assertFalse(window.search_bar_panel.isHidden())
                    self.assertFalse(window.workspace_bar_panel.isHidden())
                    self.assertFalse(window.type_filter.isHidden())
                    self.assertFalse(window.settings_button.isHidden())
                    for extension in (".wps", ".et", ".dps"):
                        self.assertGreaterEqual(window.type_filter.findData(extension), 0)
                finally:
                    window.close()

    def test_selecting_first_root_switches_to_search_workspace_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            root = base / "docs"
            root.mkdir()

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    with (
                        patch.object(
                            app_module.QFileDialog,
                            "getExistingDirectory",
                            return_value=str(root),
                        ),
                        patch.object(window, "_restart_watcher"),
                        patch.object(window, "_start_index") as start_index,
                    ):
                        window._choose_directory()

                    self.assertEqual(
                        window.database.get_index_roots(),
                        [str(root.resolve())],
                    )
                    self.assertTrue(window.first_run_panel.isHidden())
                    self.assertFalse(window.content_splitter.isHidden())
                    self.assertTrue(window.search_input.isEnabled())
                    self.assertFalse(window.type_filter.isHidden())
                    start_index.assert_called_once_with([root.resolve()])
                finally:
                    window.close()

    def test_pending_storage_move_blocks_first_index_until_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            destination = base / "other-drive"
            with (
                patch.object(app_module, "DB_PATH", db_path),
                patch.object(
                    app_module,
                    "pending_index_storage_move",
                    return_value=destination,
                ),
            ):
                window = app_module.MainWindow()
                try:
                    self.assertFalse(window.first_run_button.isEnabled())
                    self.assertIn(str(destination), window.first_run_storage_status.text())
                    self.assertIn("重新启动", window.first_run_storage_status.text())
                finally:
                    window.close()

    def test_first_run_can_stage_non_system_disk_before_choosing_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "current" / "docseek.db"
            destination = base / "E-drive" / "DocSeekData"
            pending_values = iter((None, destination))
            with (
                patch.object(app_module, "DB_PATH", db_path),
                patch.object(
                    app_module,
                    "pending_index_storage_move",
                    side_effect=lambda: next(pending_values),
                ),
            ):
                window = app_module.MainWindow()
                try:
                    with (
                        patch.object(
                            app_module.QFileDialog,
                            "getExistingDirectory",
                            return_value=str(destination),
                        ),
                        patch.object(
                            app_module,
                            "stage_index_storage_move",
                            return_value=destination,
                        ) as stage,
                        patch.object(app_module.QMessageBox, "information") as info,
                    ):
                        window._choose_first_run_storage_location()

                    stage.assert_called_once_with(str(destination))
                    info.assert_called_once()
                    self.assertFalse(window.first_run_button.isEnabled())
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
