from __future__ import annotations

import unittest

from PySide6.QtCore import Qt

from docseek.chunk_store import ChunkSearchResult
from docseek.results_model import SearchResultsModel


class SearchResultsModelTests(unittest.TestCase):
    @staticmethod
    def _result(
        filename: str,
        *,
        location: str = "第 2 页",
        size: int = 1536,
        modified_time: float = 0.0,
    ) -> ChunkSearchResult:
        return ChunkSearchResult(
            path=rf"C:\docs\{filename}",
            filename=filename,
            extension=".pdf",
            modified_time=modified_time,
            size=size,
            location=location,
            snippet="命中正文",
            score=1.0,
        )

    def test_headers_and_display_values(self) -> None:
        model = SearchResultsModel()
        model.append_items([self._result("manual.pdf")])

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.columnCount(), 6)
        self.assertEqual(model.headerData(0, Qt.Horizontal), "文件名")
        self.assertEqual(model.data(model.index(0, 0)), "manual.pdf")
        self.assertEqual(model.data(model.index(0, 1)), "第 2 页")
        self.assertEqual(model.data(model.index(0, 2)), "PDF")
        self.assertEqual(model.data(model.index(0, 3)), "1.5 KB")
        self.assertEqual(model.data(model.index(0, 5)), r"C:\docs\manual.pdf")

    def test_user_role_and_result_at_keep_original_result(self) -> None:
        result = self._result("manual.pdf")
        model = SearchResultsModel()
        model.append_items([result])

        self.assertEqual(model.data(model.index(0, 0), Qt.UserRole), result.path)
        self.assertIs(model.result_at(0), result)
        self.assertIsNone(model.result_at(-1))
        self.assertIsNone(model.result_at(1))

    def test_append_and_clear_update_row_count(self) -> None:
        model = SearchResultsModel()
        model.append_items([self._result("a.pdf"), self._result("b.pdf")])
        model.append_items([self._result("c.pdf")])
        self.assertEqual(model.rowCount(), 3)
        self.assertEqual(model.data(model.index(2, 0)), "c.pdf")

        model.clear()
        self.assertEqual(model.rowCount(), 0)
        self.assertIsNone(model.result_at(0))

    def test_empty_location_uses_readable_placeholder(self) -> None:
        model = SearchResultsModel()
        model.append_items([self._result("manual.pdf", location="")])
        self.assertEqual(model.data(model.index(0, 1)), "—")


if __name__ == "__main__":
    unittest.main()
