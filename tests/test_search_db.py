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

    def test_two_character_cjk_search(self) -> None:
        self.db.upsert_document(
            path=r"C:\docs\credit.docx",
            filename="业务手册.docx",
            extension=".docx",
            modified_time=100.0,
            size=1024,
            content="客户经理办理信贷业务时需要查看客户资料。",
        )
        rows = self.db.search("信贷")
        self.assertEqual([row.filename for row in rows], ["业务手册.docx"])

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

    def test_path_filter(self) -> None:
        self.db.upsert_document(
            path=r"C:\制度\信贷\a.pdf",
            filename="a.pdf",
            extension=".pdf",
            modified_time=100.0,
            size=10,
            content="客户服务操作指引",
        )
        self.db.upsert_document(
            path=r"C:\培训\b.pdf",
            filename="b.pdf",
            extension=".pdf",
            modified_time=90.0,
            size=10,
            content="客户服务操作指引",
        )
        rows = self.db.search("客户服务", path_contains="制度")
        self.assertEqual([row.filename for row in rows], ["a.pdf"])

    def test_paged_search_does_not_repeat_rows(self) -> None:
        for index in range(5):
            self.db.upsert_document(
                path=fr"C:\docs\{index}.txt",
                filename=f"{index}.txt",
                extension=".txt",
                modified_time=float(100 - index),
                size=10,
                content="统一测试关键词",
            )
        first = self.db.search("统一测试关键词", limit=2, offset=0)
        second = self.db.search("统一测试关键词", limit=2, offset=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertTrue({row.path for row in first}.isdisjoint({row.path for row in second}))

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

    def test_excluded_paths_are_persisted_without_duplicates(self) -> None:
        excluded = Path(self.temp_dir.name) / "private"
        excluded.mkdir()
        self.db.add_excluded_path(str(excluded))
        self.db.add_excluded_path(str(excluded))
        self.assertEqual(self.db.get_excluded_paths(), [str(excluded.resolve())])
        self.db.remove_excluded_path(str(excluded))
        self.assertEqual(self.db.get_excluded_paths(), [])

    def test_max_file_size_setting_is_bounded(self) -> None:
        self.assertEqual(self.db.get_max_file_size_mb(), 200)
        self.db.set_max_file_size_mb(512)
        self.assertEqual(self.db.get_max_file_size_mb(), 512)
        self.db.set_max_file_size_mb(99999)
        self.assertEqual(self.db.get_max_file_size_mb(), 4096)

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
