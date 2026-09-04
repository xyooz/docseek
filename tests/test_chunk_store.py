from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


class ChunkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        self.db = SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_sample(
        self,
        *,
        path: str,
        filename: str,
        extension: str,
        chunks: list[DocumentChunk],
        modified_time: float = 100.0,
        size: int = 1024,
    ) -> None:
        self.store.replace_document(
            path=path,
            filename=filename,
            extension=extension,
            modified_time=modified_time,
            size=size,
            chunks=chunks,
        )

    def test_returns_exact_hit_location(self) -> None:
        self._insert_sample(
            path=r"C:\docs\manual.pdf",
            filename="manual.pdf",
            extension=".pdf",
            chunks=[
                DocumentChunk(0, "第 1 页", "普通说明"),
                DocumentChunk(1, "第 2 页", "客户经理办理信贷业务操作说明"),
            ],
        )
        rows = self.store.search("信贷")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].location, "第 2 页")
        self.assertIn("[[HIT]]信贷[[/HIT]]", rows[0].snippet)

    def test_path_and_extension_filter(self) -> None:
        self._insert_sample(
            path=r"C:\制度\credit.pdf",
            filename="credit.pdf",
            extension=".pdf",
            chunks=[DocumentChunk(0, "第 1 页", "客户服务制度")],
        )
        self._insert_sample(
            path=r"C:\培训\credit.docx",
            filename="credit.docx",
            extension=".docx",
            chunks=[DocumentChunk(0, "文档块 1-1", "客户服务培训")],
        )
        rows = self.store.search("客户服务", extension=".pdf", path_contains="制度")
        self.assertEqual([row.filename for row in rows], ["credit.pdf"])

    def test_date_and_size_filters(self) -> None:
        self._insert_sample(
            path=r"C:\docs\old.pdf",
            filename="old.pdf",
            extension=".pdf",
            modified_time=100.0,
            size=2 * 1024,
            chunks=[DocumentChunk(0, "第 1 页", "客户制度")],
        )
        self._insert_sample(
            path=r"C:\docs\new.pdf",
            filename="new.pdf",
            extension=".pdf",
            modified_time=300.0,
            size=20 * 1024,
            chunks=[DocumentChunk(0, "第 1 页", "客户制度")],
        )

        rows = self.store.search("客户制度", modified_after=200.0)
        self.assertEqual([row.filename for row in rows], ["new.pdf"])

        rows = self.store.search("客户制度", modified_before=200.0)
        self.assertEqual([row.filename for row in rows], ["old.pdf"])

        rows = self.store.search("客户制度", min_size=10 * 1024)
        self.assertEqual([row.filename for row in rows], ["new.pdf"])

        rows = self.store.search("客户制度", max_size=5 * 1024)
        self.assertEqual([row.filename for row in rows], ["old.pdf"])

    def test_filter_only_browse_uses_file_metadata_without_keyword(self) -> None:
        self._insert_sample(
            path=r"C:\docs\old.pdf",
            filename="old.pdf",
            extension=".pdf",
            modified_time=100.0,
            size=2 * 1024,
            chunks=[DocumentChunk(0, "第 1 页", "任意正文")],
        )
        self._insert_sample(
            path=r"C:\docs\new.pdf",
            filename="new.pdf",
            extension=".pdf",
            modified_time=300.0,
            size=20 * 1024,
            chunks=[DocumentChunk(0, "第 1 页", "完全不同的正文")],
        )
        self._insert_sample(
            path=r"C:\docs\note.docx",
            filename="note.docx",
            extension=".docx",
            modified_time=400.0,
            size=30 * 1024,
            chunks=[DocumentChunk(0, "文档块 1-1", "Word 正文")],
        )

        rows = self.store.search("", extension=".pdf", modified_after=200.0)
        self.assertEqual([row.filename for row in rows], ["new.pdf"])
        self.assertEqual(rows[0].location, "")
        self.assertEqual(rows[0].snippet, "")

    def test_filter_only_browse_paginates_at_file_level(self) -> None:
        for index in range(7):
            self._insert_sample(
                path=fr"C:\docs\browse_{index}.pdf",
                filename=f"browse_{index}.pdf",
                extension=".pdf",
                modified_time=float(index),
                chunks=[DocumentChunk(0, "第 1 页", "正文")],
            )

        page1 = self.store.search("", extension=".pdf", limit=3, offset=0)
        page2 = self.store.search("", extension=".pdf", limit=3, offset=3)
        page3 = self.store.search("", extension=".pdf", limit=3, offset=6)
        paths = [row.path for row in page1 + page2 + page3]
        self.assertEqual(len(paths), 7)
        self.assertEqual(len(set(paths)), 7)

    def test_best_chunk_is_collapsed_to_one_file_result(self) -> None:
        self._insert_sample(
            path=r"C:\docs\multi.pdf",
            filename="multi.pdf",
            extension=".pdf",
            chunks=[
                DocumentChunk(0, "第 1 页", "信贷 信贷 信贷"),
                DocumentChunk(1, "第 2 页", "信贷"),
            ],
        )
        rows = self.store.search("信贷")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].filename, "multi.pdf")

    def test_file_level_pagination_has_no_duplicates_or_gaps(self) -> None:
        for index in range(12):
            chunk_count = 1 if index % 2 == 0 else 7
            chunks = [
                DocumentChunk(chunk_no, f"第 {chunk_no + 1} 页", f"信贷业务 文件{index}")
                for chunk_no in range(chunk_count)
            ]
            self._insert_sample(
                path=fr"C:\docs\file_{index:02d}.pdf",
                filename=f"file_{index:02d}.pdf",
                extension=".pdf",
                chunks=chunks,
            )

        page1 = self.store.search("信贷业务", limit=5, offset=0)
        page2 = self.store.search("信贷业务", limit=5, offset=5)
        page3 = self.store.search("信贷业务", limit=5, offset=10)

        paths = [row.path for row in page1 + page2 + page3]
        self.assertEqual(len(paths), 12)
        self.assertEqual(len(set(paths)), 12)

    def test_exact_filename_stem_ranks_above_body_only_match(self) -> None:
        self._insert_sample(
            path=r"C:\docs\credit_manual.pdf",
            filename="信贷.pdf",
            extension=".pdf",
            chunks=[DocumentChunk(0, "第 1 页", "普通业务说明")],
        )
        self._insert_sample(
            path=r"C:\docs\other.pdf",
            filename="其他制度.pdf",
            extension=".pdf",
            chunks=[DocumentChunk(0, "第 1 页", "信贷 信贷 信贷 信贷 信贷")],
        )

        rows = self.store.search("信贷")
        self.assertEqual(rows[0].filename, "信贷.pdf")


if __name__ == "__main__":
    unittest.main()
