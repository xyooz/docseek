from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek.index_issues import IndexIssueStore
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase


class DiagnosticExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_export_button_writes_privacy_safe_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            db = SearchDatabase(db_path)
            root = base / "SECRET_UI_ROOT"
            root.mkdir()
            db.add_index_root(str(root))
            IndexIssueStore(db_path).record(
                str((root / "SECRET_UI_FILE.pdf").resolve()),
                "os_error",
                "SECRET_UI_DETAIL",
            )
            destination = base / "diagnostics.json"

            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertEqual(dialog.diagnostic_button.text(), "导出脱敏诊断…")
                self.assertIn("不包含索引目录路径", dialog.diagnostic_hint_label.text())

                with (
                    patch(
                        "docseek.pausable_settings_dialog.QFileDialog.getSaveFileName",
                        return_value=(str(destination), "JSON 文件 (*.json)"),
                    ),
                    patch(
                        "docseek.pausable_settings_dialog.QMessageBox.information"
                    ) as info,
                ):
                    dialog._export_diagnostics()

                info.assert_called_once()
                payload = json.loads(destination.read_text(encoding="utf-8"))
                encoded = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("SECRET_UI_ROOT", encoded)
                self.assertNotIn("SECRET_UI_FILE", encoded)
                self.assertNotIn("SECRET_UI_DETAIL", encoded)
                self.assertEqual(payload["index"]["issues"]["total"], 1)
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
