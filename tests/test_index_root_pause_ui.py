from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from docseek import app as app_module
from docseek.index_root_state import IndexRootStateStore
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase


class IndexRootPauseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_dialog_round_trips_pause_checkboxes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SearchDatabase(Path(temp_dir) / "docseek.db")
            root_a = Path(temp_dir) / "A"
            root_b = Path(temp_dir) / "B"
            root_a.mkdir()
            root_b.mkdir()
            db.add_index_root(str(root_a))
            db.add_index_root(str(root_b))
            state = IndexRootStateStore(db)
            state.set_paused(root_b, True)

            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertEqual(dialog.root_list.item(0).checkState(), Qt.CheckState.Checked)
                self.assertEqual(dialog.root_list.item(1).checkState(), Qt.CheckState.Unchecked)

                dialog.root_list.item(0).setCheckState(Qt.CheckState.Unchecked)
                dialog.root_list.item(1).setCheckState(Qt.CheckState.Checked)
                dialog.accept()

                self.assertEqual(state.paused_roots(), [str(root_a.resolve())])
                self.assertEqual(state.active_roots(), [str(root_b.resolve())])
            finally:
                dialog.close()

    def test_desktop_refresh_watcher_and_pending_events_ignore_paused_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root_a = Path(temp_dir) / "A"
            root_b = Path(temp_dir) / "B"
            root_a.mkdir()
            root_b.mkdir()
            active_file = root_a / "active.docx"
            paused_file = root_b / "paused.docx"

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window.database.add_index_root(str(root_a))
                    window.database.add_index_root(str(root_b))
                    state = IndexRootStateStore(window.database)
                    state.set_paused(root_b, True)

                    window._refresh_scope()
                    self.assertIn("暂停 1 个", window.scope_label.text())

                    with patch.object(window, "_start_index") as start_index:
                        window._refresh_all_roots()
                        start_index.assert_called_once_with([root_a.resolve()])

                    with patch.object(window.watch_manager, "start") as watcher_start:
                        window._restart_watcher()
                        watcher_start.assert_called_once_with(
                            [str(root_a.resolve())],
                            [],
                        )

                    window.pending_watch_paths = {str(active_file), str(paused_file)}
                    with patch.object(window, "_start_path_update") as update_paths:
                        window._drain_watch_queue()
                        update_paths.assert_called_once()
                        paths = update_paths.call_args.args[0]
                        self.assertEqual(paths, [active_file.resolve()])
                finally:
                    window.close()
                    QApplication.processEvents()

    def test_all_paused_roots_keep_search_scope_but_block_manual_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = Path(temp_dir) / "A"
            root.mkdir()

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window.database.add_index_root(str(root))
                    IndexRootStateStore(window.database).set_paused(root, True)
                    window._refresh_scope()
                    self.assertIn("已暂停更新", window.scope_label.text())
                    self.assertIn("现有索引仍可搜索", window.scope_label.text())

                    with patch.object(window, "_start_index") as start_index:
                        window._refresh_all_roots()
                        start_index.assert_not_called()
                    self.assertIn("所有索引目录已暂停", window.statusBar().currentMessage())
                finally:
                    window.close()
                    QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
