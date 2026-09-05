from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek.index_health import get_last_successful_reconcile, record_successful_reconcile
from docseek.indexer import DirectoryIndexer
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase


class ReconcileTimestampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.root.mkdir()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_scan_records_completion_but_precise_update_does_not_overwrite_it(self) -> None:
        target = self.root / "guide.txt"
        target.write_text("客户经理信贷资料", encoding="utf-8")
        self.assertIsNone(get_last_successful_reconcile(self.db))

        with patch("docseek.index_health.time.time", return_value=1234.5):
            DirectoryIndexer(self.db).scan(self.root)
        self.assertEqual(get_last_successful_reconcile(self.db), 1234.5)

        target.write_text("客户经理风控资料", encoding="utf-8")
        with patch("docseek.index_health.time.time", return_value=9999.0):
            DirectoryIndexer(self.db).update_paths([target])
        self.assertEqual(get_last_successful_reconcile(self.db), 1234.5)

    def test_failed_full_scan_does_not_record_success(self) -> None:
        indexer = DirectoryIndexer(self.db)
        with patch.object(
            indexer,
            "_iter_supported_files",
            side_effect=RuntimeError("scan failed before completion"),
        ):
            with self.assertRaises(RuntimeError):
                indexer.scan(self.root)
        self.assertIsNone(get_last_successful_reconcile(self.db))

    def test_settings_show_last_successful_full_reconciliation(self) -> None:
        local_time = datetime(2026, 1, 2, 3, 4)
        record_successful_reconcile(self.db, completed_at=local_time.timestamp())

        dialog = PausableIndexSettingsDialog(self.db)
        try:
            self.assertEqual(
                dialog.reconcile_time_label.text(),
                "最近完整校准：2026-01-02 03:04",
            )
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
