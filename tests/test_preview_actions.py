from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PySide6.QtCore import QUrl, QProcess
from PySide6.QtWidgets import QApplication
from docseek import app as app_module
from docseek.chunk_store import ChunkSearchResult
from docseek.preview_dialog import LocationPreviewDialog
from docseek.document_hits_dialog import DocumentHitsDialog
from docseek.chunks import DocumentChunk
from docseek.chunk_store import ChunkStore
from docseek.search_db import SearchDatabase
from docseek.exact_search import ExactGroupedSearchEngine


class PreviewActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_relax_keeps_quoted_phrase_and_structure_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(app_module, "DB_PATH", Path(folder) / "index.db"):
                window = app_module.MainWindow()
            try:
                window.search_input.setText('"customer manager" page:2 ext:pdf path:"a b"')
                window.type_filter.setCurrentIndex(1)
                with patch.object(window, "_perform_search") as search:
                    window._preview_action(QUrl("docseek:relax"))
                self.assertEqual(window.search_input.text(), '"customer manager" page:2')
                self.assertIsNone(window.type_filter.currentData())
                search.assert_called_once_with()
                with patch.object(window, "_open_index_settings") as settings:
                    window._preview_action(QUrl("docseek:settings"))
                    settings.assert_called_once_with()
                    window._preview_action(QUrl("https://example.org"))
                    settings.assert_called_once_with()
            finally:
                window.close()

    def test_actual_pdf_dialog_and_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((30, 30), "preview")
                doc.save(source)
            stat = source.stat()
            row = ChunkSearchResult(str(source), source.name, ".pdf", stat.st_mtime,
                                    stat.st_size, "第 1 页", "preview", 0)
            dialog = LocationPreviewDialog(row)
            try:
                deadline = time.monotonic() + 15
                while dialog.timer.isActive() and time.monotonic() < deadline:
                    self.qt.processEvents()
                    time.sleep(0.01)
                self.assertIn("原文件页面预览", dialog.browser.toPlainText())
                image = dialog.browser.document().resource(2, QUrl("preview:page"))
                self.assertFalse(image.isNull())
            finally:
                dialog.reject()
            dialog = LocationPreviewDialog(row)
            dialog._timeout()
            self.assertIn("超过 15 秒", dialog.browser.toPlainText())
            dialog.reject()
            self.assertEqual(dialog.process.state(), QProcess.NotRunning)

    def test_document_hits_dialog_paginates_and_cancels(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "index.db"
            SearchDatabase(db)
            store = ChunkStore(db)
            store.replace_document(path="many.pdf", filename="many.pdf", extension=".pdf",
                                   modified_time=1, size=1, chunks=[
                                       DocumentChunk(i, f"第 {i + 1} 页", "客户经理 <unsafe>")
                                       for i in range(35)])
            row = ExactGroupedSearchEngine(store).search_page("客户经理").items[0]
            dialog = DocumentHitsDialog(store, row, "客户经理")
            def wait_loaded():
                deadline = time.monotonic() + 6
                while "正在加载" in dialog.browser.toPlainText() and time.monotonic() < deadline:
                    self.qt.processEvents()
                    time.sleep(0.01)
            try:
                wait_loaded()
                self.assertEqual(len(dialog.items), 30)
                self.assertTrue(dialog.next.isEnabled())
                self.assertFalse(dialog.previous.isEnabled())
                self.assertIn("<unsafe>", dialog.browser.toPlainText())
                dialog.next.click()
                wait_loaded()
                self.assertEqual(len(dialog.items), 5)
                self.assertEqual(dialog.items[0].structure["page"], 31)
                self.assertFalse(dialog.next.isEnabled())
                self.assertTrue(dialog.previous.isEnabled())
            finally:
                dialog.reject()
            self.assertTrue(dialog.worker.cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
