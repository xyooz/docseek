from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTextBrowser

from docseek import app as app_module
from docseek.search_help import SearchHelpDialog, search_help_html


class SearchHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_help_content_tracks_supported_search_surface(self) -> None:
        html = search_help_html()
        for expected in (
            "ext:pdf",
            "path:&quot;业务 文件&quot;",
            "after:2026-01-01",
            "size:&gt;10MB",
            "page:12",
            "slide:4",
            "sheet:&quot;客户 数据&quot;",
            "Ctrl+L",
            "Ctrl+Shift+O",
            "F1",
            "收藏",
        ):
            self.assertIn(expected, html)

    def test_help_dialog_renders_actionable_content(self) -> None:
        dialog = SearchHelpDialog()
        try:
            self.assertEqual(dialog.windowTitle(), "DocSeek 搜索帮助")
            browser = dialog.findChild(QTextBrowser)
            self.assertIsNotNone(browser)
            plain_text = browser.toPlainText()  # type: ignore[union-attr]
            self.assertIn("DocSeek 搜索帮助", plain_text)
            self.assertIn("结构定位", plain_text)
            self.assertIn("Ctrl+Shift+O", plain_text)
            self.assertIn("筛选、排序与常用搜索", plain_text)
        finally:
            dialog.close()

    def test_main_window_exposes_help_button_and_f1_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            with patch.object(app_module, "DB_PATH", db_path):
                window = app_module.MainWindow()
                try:
                    self.assertEqual(window.help_button.text(), "帮助")
                    self.assertEqual(window.search_help_action.shortcut().toString(), "F1")
                    self.assertTrue(window.help_button.toolTip())
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
