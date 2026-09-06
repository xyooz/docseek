from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek import app as app_module


class IndexShutdownUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_close_requests_index_cancellation_and_retries_after_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    worker = Mock()
                    window.current_worker = worker
                    event = Mock()

                    window.closeEvent(event)

                    worker.cancel.assert_called_once_with()
                    event.ignore.assert_called_once_with()
                    self.assertTrue(window._close_when_index_stops)
                    self.assertIn("安全停止索引", window.statusBar().currentMessage())

                    with patch.object(app_module.app_base.QTimer, "singleShot") as timer:
                        window._finish_index_ui()
                    timer.assert_called_once()
                    self.assertEqual(timer.call_args.args[0], 0)
                finally:
                    window.current_worker = None
                    window._close_when_index_stops = False
                    window.close()

    def test_automatic_full_scan_still_exposes_stop_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    worker = app_module.IndexWorker(db_path=db_path, roots=[])
                    with patch.object(window.thread_pool, "start"):
                        window._launch_worker(worker, automatic=True)

                    self.assertFalse(window.cancel_button.isHidden())
                finally:
                    window.current_worker = None
                    window.close()

    def test_known_candidate_total_drives_percent_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = str(Path(temp_dir) / "docs")
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window._index_plan(root, 120, 1, 1)
                    window._index_progress_detailed(
                        str(Path(root) / "制度.docx"), 48, 45, 120, root
                    )

                    self.assertEqual(window.index_progress_bar.maximum(), 120)
                    self.assertEqual(window.index_progress_bar.value(), 48)
                    self.assertTrue(window.index_progress_bar.isTextVisible())
                    self.assertIn("48/120", window.index_counts_label.text())
                    self.assertEqual(window.index_root_progress[root], (48, 120))
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
