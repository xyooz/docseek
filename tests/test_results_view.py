from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableView

from docseek import app as app_module
from docseek.chunk_store import ChunkSearchResult


class ResultsViewSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_main_window_uses_model_backed_table_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    self.assertIsInstance(window.results, QTableView)
                    self.assertIs(window.results.model(), window.results_model)
                    self.assertEqual(window.results_model.rowCount(), 0)

                    result = ChunkSearchResult(
                        path=r"C:\docs\manual.pdf",
                        filename="manual.pdf",
                        extension=".pdf",
                        modified_time=1.0,
                        size=1024,
                        location="第 2 页",
                        snippet="客户经理 [[HIT]]信贷[[/HIT]] 业务",
                        score=1.0,
                    )
                    window.results_model.append_items([result])
                    index = window.results_model.index(0, 0)
                    window.results.setCurrentIndex(index)
                    window.results.selectRow(0)
                    QApplication.processEvents()

                    self.assertEqual(window._selected_path(), result.path)
                    preview = window.preview.toPlainText()
                    self.assertIn("manual.pdf", preview)
                    self.assertIn("第 2 页", preview)
                    self.assertIn("信贷", preview)
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
