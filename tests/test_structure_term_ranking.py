from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.search_db import SearchDatabase


class StructureTermRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.search = ExactGroupedSearchEngine(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(self, index: int, *, filename: str, location: str, content: str) -> None:
        self.store.replace_document(
            path=str(Path("C:/docs") / filename),
            filename=filename,
            extension=Path(filename).suffix,
            modified_time=float(index),
            size=1000 + index,
            chunks=[DocumentChunk(0, location, content)],
        )

    def test_all_query_terms_can_match_title_with_words_between_them(self) -> None:
        self._add(
            0,
            filename="body.docx",
            location="文档块 1-2",
            content="risk audit controls",
        )
        self._add(
            1,
            filename="structured.docx",
            location="文档块 1-2 · 标题 Risk quarterly audit",
            content="risk audit controls",
        )

        page = self.search.search_page("risk audit")

        self.assertEqual(page.total_count, 2)
        self.assertEqual(Path(page.items[0].path).name, "structured.docx")
        self.assertLess(page.items[0].score, page.items[1].score)

    def test_partial_title_term_match_does_not_receive_fallback_boost(self) -> None:
        self._add(
            0,
            filename="partial.docx",
            location="文档块 1-2 · 标题 Risk overview",
            content="risk audit controls",
        )
        self._add(
            1,
            filename="plain.docx",
            location="文档块 1-2",
            content="risk audit controls",
        )

        page = self.search.search_page("risk audit")

        self.assertEqual(page.total_count, 2)
        self.assertEqual(page.items[0].score, page.items[1].score)

    def test_same_file_prefers_non_contiguous_matching_title_chunk(self) -> None:
        self.store.replace_document(
            path="C:/docs/report.docx",
            filename="report.docx",
            extension=".docx",
            modified_time=1.0,
            size=1000,
            chunks=[
                DocumentChunk(0, "文档块 1-2", "risk audit controls"),
                DocumentChunk(
                    1,
                    "文档块 3-4 · 标题 Risk quarterly audit",
                    "risk audit controls",
                ),
            ],
        )

        page = self.search.search_page("risk audit")

        self.assertEqual(page.total_count, 1)
        self.assertEqual(
            page.items[0].location,
            "文档块 3-4 · 标题 Risk quarterly audit",
        )


if __name__ == "__main__":
    unittest.main()
