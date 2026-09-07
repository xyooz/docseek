from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


class SearchPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

        for index in range(13):
            self.store.replace_document(
                path=fr"C:\docs\file_{index:02d}.pdf",
                filename=f"file_{index:02d}.pdf",
                extension=".pdf",
                modified_time=float(index),
                size=1024 + index,
                chunks=[
                    DocumentChunk(0, "第 1 页", f"信贷业务 文件 {index}"),
                    DocumentChunk(1, "第 2 页", "信贷业务"),
                ],
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_keyword_page_returns_file_level_total(self) -> None:
        first = self.store.search_page("信贷业务", limit=5, offset=0)
        second = self.store.search_page("信贷业务", limit=5, offset=5)

        self.assertEqual(first.total_count, 13)
        self.assertEqual(second.total_count, 13)
        self.assertEqual(len(first.items), 5)
        self.assertEqual(len(second.items), 5)

    def test_filter_only_page_returns_total(self) -> None:
        page = self.store.search_page("", extension=".pdf", limit=4, offset=0)

        self.assertEqual(page.total_count, 13)
        self.assertEqual(len(page.items), 4)

    def test_empty_tail_page_keeps_total(self) -> None:
        page = self.store.search_page("信贷业务", limit=5, offset=20)

        self.assertEqual(page.items, [])
        self.assertEqual(page.total_count, 13)


if __name__ == "__main__":
    unittest.main()
