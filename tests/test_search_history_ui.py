from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from docseek import app as app_module
from docseek.search_presets import SearchState
from docseek.search_sort import SORT_FILENAME, SORT_MODIFIED


class SearchHistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def _window(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "docseek.db"
        patcher = patch.object(app_module, "DB_PATH", db_path)
        patcher.start()
        window = app_module.MainWindow()
        return temp_dir, patcher, window

    def test_current_search_can_be_saved_and_unsaved(self) -> None:
        temp_dir, patcher, window = self._window()
        try:
            window.search_input.setText("信贷制度")
            window.type_filter.setCurrentIndex(window.type_filter.findData(".pdf"))
            window.sort_filter.setCurrentIndex(window.sort_filter.findData(SORT_MODIFIED))
            QApplication.processEvents()

            window._toggle_saved_search()
            saved = window.search_state_store.saved()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].query, "信贷制度")
            self.assertEqual(saved[0].extension, ".pdf")
            self.assertEqual(saved[0].sort_mode, SORT_MODIFIED)
            self.assertEqual(window.favorite_button.text(), "★ 已收藏")

            window._toggle_saved_search()
            self.assertEqual(window.search_state_store.saved(), [])
            self.assertEqual(window.favorite_button.text(), "☆ 收藏")
        finally:
            window.close()
            patcher.stop()
            temp_dir.cleanup()

    def test_apply_saved_state_restores_query_type_and_sort_atomically(self) -> None:
        temp_dir, patcher, window = self._window()
        try:
            state = SearchState('客户 path:"制度 文件"', ".xlsx", SORT_FILENAME)
            with patch.object(window, "_perform_search") as perform_search:
                window._apply_search_state(state)

            self.assertEqual(window.search_input.text(), state.query)
            self.assertEqual(window.type_filter.currentData(), ".xlsx")
            self.assertEqual(window.sort_filter.currentData(), SORT_FILENAME)
            perform_search.assert_called_once_with()
        finally:
            window.close()
            patcher.stop()
            temp_dir.cleanup()

    def test_empty_state_cannot_be_saved(self) -> None:
        temp_dir, patcher, window = self._window()
        try:
            self.assertFalse(window.favorite_button.isEnabled())
            window._toggle_saved_search()
            self.assertEqual(window.search_state_store.saved(), [])
        finally:
            window.close()
            patcher.stop()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
