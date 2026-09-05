from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.progressive_search import ProgressiveSearchEngine
from docseek.search_db import SearchDatabase


class ProgressiveSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.engine = ProgressiveSearchEngine(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(self, index: int, chunks: int, *, content: str = "客户经理 信贷业务") -> None:
        name = f"doc_{index:03d}.txt"
        path = str(Path("C:/docs") / name)
        self.store.replace_document(
            path=path,
            filename=name,
            extension=".txt",
            modified_time=float(index),
            size=100 + index,
            chunks=[DocumentChunk(i, f"块 {i + 1}", content) for i in range(chunks)],
        )

    def test_topk_adapts_when_one_file_owns_many_top_chunks(self) -> None:
        self._add(0, 20)
        for index in range(1, 8):
            self._add(index, 1)

        page = self.engine.search_topk(
            "信贷",
            limit=5,
            candidate_multiplier=1,
            max_candidates=64,
        )
        self.assertEqual(len(page.items), 5)
        self.assertEqual(len({row.path for row in page.items}), 5)
        self.assertGreater(page.candidates_scanned, 5)

    def test_exact_total_is_available_when_candidate_stream_is_exhausted(self) -> None:
        for index in range(4):
            self._add(index, 1)
        page = self.engine.search_topk("信贷", limit=10, candidate_multiplier=2)
        self.assertTrue(page.exhausted)
        self.assertEqual(page.exact_total, 4)

    def test_exact_count_does_not_require_ranking_all_results(self) -> None:
        for index in range(6):
            self._add(index, 3)
        self._add(99, 1, content="完全无关内容")
        self.assertEqual(self.engine.count_files("信贷"), 6)

    def test_metadata_filters_apply_to_topk_and_count(self) -> None:
        for index in range(5):
            self._add(index, 1)
        page = self.engine.search_topk("信贷", limit=10, min_size=103)
        self.assertEqual({row.filename for row in page.items}, {"doc_003.txt", "doc_004.txt"})
        self.assertEqual(self.engine.count_files("信贷", min_size=103), 2)

    def test_filename_exact_match_keeps_priority_in_candidate_rerank(self) -> None:
        self._add(0, 1, content="信贷 信贷 信贷")
        path = str(Path("C:/docs") / "信贷.txt")
        self.store.replace_document(
            path=path,
            filename="信贷.txt",
            extension=".txt",
            modified_time=0.0,
            size=20,
            chunks=[DocumentChunk(0, "块 1", "普通正文包含信贷")],
        )
        page = self.engine.search_topk("信贷", limit=2)
        self.assertEqual(page.items[0].filename, "信贷.txt")


if __name__ == "__main__":
    unittest.main()
