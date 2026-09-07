from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf
from openpyxl import Workbook

from docseek.location_preview import render_preview, preview_kind


class LocationPreviewTests(unittest.TestCase):
    def test_pdf_page_and_stale_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((30, 30), "first page")
                doc.new_page().insert_text((30, 30), "second page")
                doc.save(path)
            stat = path.stat()
            result = render_preview(str(path), "第 2 页", stat.st_mtime, stat.st_size)
            self.assertIn("png", result)
            self.assertIn("第 2 页", result["html"])
            with self.assertRaisesRegex(ValueError, "文件已变化"):
                render_preview(str(path), "第 2 页", stat.st_mtime, stat.st_size + 1)
            with self.assertRaisesRegex(ValueError, "页码"):
                render_preview(str(path), "第 3 页", stat.st_mtime, stat.st_size)

    def test_excel_real_rows_columns_and_html_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "明细"
            sheet.append(["姓名", "金额"])
            sheet.append(["<script>bad</script>", None, "C"])
            sheet.append([None, None, None])
            sheet.append(["target", 123])
            book.save(path)
            book.close()
            stat = path.stat()
            result = render_preview(str(path), "工作表 明细 · 行 2-4", stat.st_mtime, stat.st_size)["html"]
            self.assertIn("&lt;script&gt;", result)
            self.assertNotIn("<script>", result)
            self.assertIn("<th>3</th>", result)
            self.assertIn("<td></td><td>C</td>", result)
            self.assertIn("<th>4</th><td>target</td>", result)

    def test_worker_returns_error_as_json(self):
        result = subprocess.run([sys.executable, "-m", "docseek.location_preview",
                                 "missing.pdf", "第 1 页", "0", "0"],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))

    @unittest.skipUnless(os.name == "nt", "Windows windowed pipe")
    def test_windowed_worker_pipe(self):
        pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
        result = subprocess.run([pythonw, "-m", "docseek.location_preview",
                                 "missing.pdf", "第 1 页", "0", "0"],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))

    def test_unsupported_locator_is_not_offered(self):
        self.assertIsNone(preview_kind(".pdf", "文档块 1-2"))
        self.assertIsNone(preview_kind(".xls", "工作表 A · 行 1-2"))


if __name__ == "__main__":
    unittest.main()
