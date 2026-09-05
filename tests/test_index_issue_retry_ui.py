from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from docseek import app as app_module
from docseek.index_issues import IndexIssueStore
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.retryable_index_issues_dialog import RetryableIndexIssuesDialog


class IndexIssueRetryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_retry_dialog_returns_selected_and_all_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = IndexIssueStore(Path(temp_dir) / "docseek.db")
            first = str(Path(temp_dir) / "first.docx")
            second = str(Path(temp_dir) / "second.pdf")
            store.record(first, "os_error", "one")
            store.record(second, "BadZipFile", "two")

            selected_dialog = RetryableIndexIssuesDialog(store)
            try:
                selected_dialog.table.setCurrentCell(0, 0)
                QApplication.processEvents()
                self.assertTrue(selected_dialog.retry_selected_button.isEnabled())
                selected_path = selected_dialog._selected_path()
                selected_dialog._retry_selected()
                self.assertEqual(selected_dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(selected_dialog.retry_paths, [selected_path])
            finally:
                selected_dialog.close()

            all_dialog = RetryableIndexIssuesDialog(store)
            try:
                all_dialog._retry_all()
                self.assertEqual(all_dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(set(all_dialog.retry_paths), {first, second})
            finally:
                all_dialog.close()

    def test_saved_settings_route_retry_through_main_precise_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = Path(temp_dir) / "root"
            root.mkdir()
            target = root / "retry.docx"
            target.touch()

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window.database.add_index_root(str(root))
                    store = IndexIssueStore(db_path)
                    store.record(str(target.resolve()), "os_error", "temporary")
                    settings = PausableIndexSettingsDialog(window.database, window)
                    fake_retry_dialog = Mock()
                    fake_retry_dialog.exec.return_value = QDialog.DialogCode.Accepted
                    fake_retry_dialog.retry_paths = [str(target.resolve())]

                    try:
                        with (
                            patch(
                                "docseek.pausable_settings_dialog.RetryableIndexIssuesDialog",
                                return_value=fake_retry_dialog,
                            ),
                            patch(
                                "docseek.pausable_settings_dialog.QTimer.singleShot",
                                side_effect=lambda _delay, callback: callback(),
                            ),
                            patch.object(window, "_start_path_update") as start_update,
                        ):
                            settings._show_index_issues()

                        start_update.assert_called_once()
                        paths = start_update.call_args.args[0]
                        self.assertEqual(len(paths), 1)
                        self.assertTrue(os.path.samefile(paths[0], target))
                        self.assertEqual(settings.result(), QDialog.DialogCode.Rejected)
                    finally:
                        settings.close()
                finally:
                    window.close()
                    QApplication.processEvents()

    def test_unsaved_settings_keep_problem_viewer_but_do_not_offer_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            root = Path(temp_dir) / "root"
            root.mkdir()

            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window.database.add_index_root(str(root))
                    settings = PausableIndexSettingsDialog(window.database, window)
                    settings.max_size.setValue(settings.max_size.value() + 1)
                    fake_viewer = Mock()
                    fake_viewer.layout.return_value = Mock()

                    try:
                        with (
                            patch(
                                "docseek.pausable_settings_dialog.IndexIssuesDialog",
                                return_value=fake_viewer,
                            ),
                            patch(
                                "docseek.pausable_settings_dialog.RetryableIndexIssuesDialog"
                            ) as retry_dialog,
                        ):
                            settings._show_index_issues()

                        fake_viewer.exec.assert_called_once()
                        retry_dialog.assert_not_called()
                    finally:
                        settings.close()
                finally:
                    window.close()
                    QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
