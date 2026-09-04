from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.search_db import SearchDatabase


class SearchDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_keyword_search_and_metadata(self) -> None:
        self.db.upsert_document(
            path=r"C:\docs\guide.docx",
            filename="guide.docx",
            extension=".docx",
            modified_time=100.0,
            size=1024,
            content="客户经理办理信贷业务时需要查看客户资料。",
        )
        rows = self.db.search("客户经理")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].filename, "guide.docx")
        self.assertEqual(rows[0].extension, ".docx")

    def test_extension_filter(self) -> None:
        self.db.upsert_document(
            path=r"C:\docs\a.pdf",
            filename="a.pdf",
            extension=".pdf",
            modified_time=100.0,
            size=10,
            content="客户服务操作指引",
        )
        self.db.upsert_document(
            path=r"C:\docs\b.docx",
            filename="b.docx",
            extension=".docx",
            modified_time=100.0,
            size=10,
            content="客户服务培训材料",
        )
        rows = self.db.search("客户服务", extension=".pdf")
        self.assertEqual([row.filename for row in rows], ["a.pdf"])

    def test_multiple_roots_are_persisted_without_duplicates(self) -> None:
        root_a = Path(self.temp_dir.name) / "A"
        root_b = Path(self.temp_dir.name) / "B"
        root_a.mkdir()
        root_b.mkdir()
        self.db.add_index_root(str(root_a))
        self.db.add_index_root(str(root_b))
        self.db.add_index_root(str(root_a))
        roots = self.db.get_index_roots()
        self.assertEqual(len(roots), 2)
        self.assertIn(str(root_a.resolve()), roots)
        self.assertIn(str(root_b.resolve()), roots)

    def test_unchanged_detection(self) -> None:
        path = r"C:\docs\stable.pdf"
        self.db.upsert_document(
            path=path,
            filename="stable.pdf",
            extension=".pdf",
            modified_time=123.0,
            size=456,
            content="稳定内容",
        )
        self.assertTrue(self.db.is_unchanged(path, modified_time=123.0, size=456))
        self.assertFalse(self.db.is_unchanged(path, modified_time=124.0, size=456))


if __name__ == "__main__":
    unittest.main()
