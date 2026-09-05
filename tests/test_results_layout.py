from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication

from docseek import app as app_module
from docseek.results_layout import (
    DEFAULT_COLUMN_WIDTHS,
    decode_header_state,
    encode_header_state,
)


class ResultsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_header_state_encoding_round_trip_and_invalid_values(self) -> None:
        original = QByteArray(b"docseek-header-state")
        encoded = encode_header_state(original)
        self.assertEqual(decode_header_state(encoded), original)
        self.assertIsNone(decode_header_state(None))
        self.assertIsNone(decode_header_state(""))
        self.assertIsNone(decode_header_state("%%%%"))

    def test_layout_survives_window_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                first = app_module.MainWindow()
                header = first.results.horizontalHeader()
                header.resizeSection(1, 333)
                header.moveSection(header.visualIndex(4), 1)
                first._set_result_column_visible(5, False)
                first.close()
                QApplication.processEvents()

                second = app_module.MainWindow()
                try:
                    restored = second.results.horizontalHeader()
                    self.assertEqual(restored.sectionSize(1), 333)
                    self.assertEqual(restored.visualIndex(4), 1)
                    self.assertTrue(restored.isSectionHidden(5))
                finally:
                    second.close()

    def test_filename_column_cannot_be_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    window._set_result_column_visible(0, False)
                    self.assertFalse(window.results.horizontalHeader().isSectionHidden(0))
                finally:
                    window.close()

    def test_reset_restores_default_order_widths_and_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    header = window.results.horizontalHeader()
                    header.resizeSection(2, 240)
                    header.moveSection(header.visualIndex(5), 1)
                    window._set_result_column_visible(3, False)

                    window._reset_results_layout()

                    for logical_index, width in enumerate(DEFAULT_COLUMN_WIDTHS):
                        self.assertEqual(header.visualIndex(logical_index), logical_index)
                        self.assertFalse(header.isSectionHidden(logical_index))
                        self.assertEqual(header.sectionSize(logical_index), width)
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
