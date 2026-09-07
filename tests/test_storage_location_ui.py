from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from docseek.search_db import SearchDatabase
from docseek.settings_dialog import IndexSettingsDialog


class StorageLocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_show_current_index_storage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "data" / "docseek.db"
            database = SearchDatabase(db_path)
            with patch(
                "docseek.settings_dialog.pending_index_storage_move",
                return_value=None,
            ):
                dialog = IndexSettingsDialog(database)
                try:
                    self.assertIn(
                        str(db_path.parent.resolve()),
                        dialog.storage_location_label.text(),
                    )
                    self.assertTrue(dialog.storage_move_button.isEnabled())
                    self.assertIn(
                        "没有待执行的迁移",
                        dialog.storage_pending_label.text(),
                    )
                finally:
                    dialog.close()

    def test_change_location_stages_restart_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "current" / "docseek.db"
            destination = base / "E-drive" / "DocSeekData"
            database = SearchDatabase(db_path)

            with patch(
                "docseek.settings_dialog.pending_index_storage_move",
                return_value=None,
            ):
                dialog = IndexSettingsDialog(database)
            try:
                with (
                    patch.object(
                        dialog,
                        "_refresh_storage_location",
                    ) as refresh,
                    patch(
                        "docseek.settings_dialog.QFileDialog.getExistingDirectory",
                        return_value=str(destination),
                    ),
                    patch(
                        "docseek.settings_dialog.QMessageBox.question",
                        return_value=QMessageBox.Yes,
                    ),
                    patch(
                        "docseek.settings_dialog.QMessageBox.information"
                    ) as information,
                    patch(
                        "docseek.settings_dialog.stage_index_storage_move",
                        return_value=destination.resolve(),
                    ) as stage,
                ):
                    dialog._change_storage_location()

                stage.assert_called_once_with(str(destination))
                refresh.assert_called_once_with()
                information.assert_called_once()
                self.assertIn(
                    "下次启动",
                    information.call_args.args[2],
                )
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
