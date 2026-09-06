from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.search_db import SearchDatabase


class MultiChunkRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = self.base / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.engine = ExactGroupedSearchEngine(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(self, name: str, chunks: list[str]) -> None:
        path = self.base / name
        path.write_text("fixture", encoding="utf-8")
        stat = path.stat()
        self.store.replace_document(
            path=str(path.resolve()),
            filename=name,
            extension=path.suffix,
            modified_time=stat.st_mtime,
            size=stat.st_size,
            chunks=[
                DocumentChunk(ordinal=index, location=f"文档块 {index + 1}", content=text)
                for index, text in enumerate(chunks)
            ],
        )

    def test_two_matching_chunks_receive_small_file_level_boost(self) -> None:
        self._add("single.txt", ["risk audit evidence"])
        self._add("double.txt", ["risk audit evidence", "risk audit evidence"])

        page = self.engine.search_page("risk audit")

        self.assertEqual([item.filename for item in page.items[:2]], ["double.txt", "single.txt"])
        self.assertLess(page.items[0].score, page.items[1].score)

    def test_three_and_many_matching_chunks_share_same_capped_bonus(self) -> None:
        self._add("three.txt", ["risk audit evidence"] * 3)
        self._add("many.txt", ["risk audit evidence"] * 20)

        page = self.engine.search_page("risk audit")
        scores = {item.filename: item.score for item in page.items}

        self.assertAlmostEqual(scores["three.txt"], scores["many.txt"], places=8)

    def test_filename_signal_still_dominates_multi_chunk_evidence(self) -> None:
        self._add("risk audit.txt", ["risk audit evidence"])
        self._add("generic.txt", ["risk audit evidence"] * 20)

        page = self.engine.search_page("risk audit")

        self.assertEqual(page.items[0].filename, "risk audit.txt")

    def test_best_snippet_selection_is_not_changed_by_file_level_evidence(self) -> None:
        path = self.base / "mixed.txt"
        path.write_text("fixture", encoding="utf-8")
        stat = path.stat()
        self.store.replace_document(
            path=str(path.resolve()),
            filename=path.name,
            extension=path.suffix,
            modified_time=stat.st_mtime,
            size=stat.st_size,
            chunks=[
                DocumentChunk(ordinal=0, location="文档块 1", content="risk audit filler filler filler"),
                DocumentChunk(ordinal=1, location="文档块 2", content="risk audit"),
                DocumentChunk(ordinal=2, location="文档块 3", content="risk audit filler"),
            ],
        )

        page = self.engine.search_page("risk audit")

        self.assertEqual(page.items[0].location, "文档块 2")

    def test_file_count_is_unchanged_by_multi_chunk_evidence(self) -> None:
        self._add("one.txt", ["risk audit"])
        self._add("many.txt", ["risk audit"] * 10)

        self.assertEqual(self.engine.count_files("risk audit"), 2)
        self.assertEqual(self.engine.search_page("risk audit", limit=1).total_count, 2)


if __name__ == "__main__":
    unittest.main()
