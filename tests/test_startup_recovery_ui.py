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


class StartupRecoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_preconfigured_active_roots_schedule_startup_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = Path(temp_dir) / "docs"
            root.mkdir()
            SearchDatabase(db_path).add_index_root(str(root))

            with (
                patch.object(app_module, "DB_PATH", db_path),
                patch.object(app_module.QTimer, "singleShot") as single_shot,
            ):
                window = app_module.MainWindow()
                try:
                    single_shot.assert_called_once()
                    delay, callback = single_shot.call_args.args
                    self.assertEqual(delay, 0)

                    with patch.object(window, "_start_index") as start_index:
                        callback()
                        start_index.assert_called_once_with(
                            [root.resolve()], automatic=True
                        )
                    self.assertIn(
                        "关闭期间的文件变化",
                        window.statusBar().currentMessage(),
                    )
                finally:
                    window.close()
                    QApplication.processEvents()

    def test_adding_root_persists_before_first_scan_and_restarts_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = Path(temp_dir) / "docs"
            root.mkdir()
            resolved = root.resolve()

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    def assert_persisted_before_scan(roots, *, automatic=False):
                        self.assertFalse(automatic)
                        self.assertEqual(roots, [resolved])
                        self.assertEqual(
                            window.database.get_index_roots(),
                            [str(resolved)],
                        )

                    with (
                        patch.object(
                            app_module.QFileDialog,
                            "getExistingDirectory",
                            return_value=str(root),
                        ),
                        patch.object(window, "_restart_watcher") as restart_watcher,
                        patch.object(
                            window,
                            "_start_index",
                            side_effect=assert_persisted_before_scan,
                        ) as start_index,
                    ):
                        window._choose_directory()

                    restart_watcher.assert_called_once_with()
                    start_index.assert_called_once()
                    self.assertEqual(
                        window.database.get_index_roots(),
                        [str(resolved)],
                    )
                finally:
                    window.close()
                    QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
