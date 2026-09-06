from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.search_db import SearchDatabase


class SourceChangeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "docs"
        self.root.mkdir()
        self.path = self.root / "source.txt"
        self.path.write_text("original", encoding="utf-8")
        self.indexer = DirectoryIndexer(SearchDatabase(Path(self.temp.name) / "index.db"))
        self.indexer.scan(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_change_during_parse_rolls_back_then_retries_without_backoff(self):
        for full_scan in (False, True):
            with self.subTest(full_scan=full_scan):
                self.path.write_text("original", encoding="utf-8")
                self.indexer.update_paths([self.path])
                self.path.write_text("new revision", encoding="utf-8")
                def changing(*args, **kwargs):
                    yield DocumentChunk(0, "行 1-1", "uncommitted")
                    self.path.write_text("latest revision", encoding="utf-8")
                with patch("docseek.indexer.iter_document_chunks", changing):
                    stats = self.indexer.scan(self.root) if full_scan else self.indexer.update_paths([self.path])
                self.assertEqual(stats.indexed, 0)
                engine = ExactGroupedSearchEngine(self.indexer.chunk_store)
                self.assertEqual(engine.search_page("uncommitted").total_count, 0)
                self.assertEqual(engine.search_page("original").total_count, 1)
                with self.indexer.chunk_store.connect() as conn:
                    self.assertEqual(conn.execute("SELECT status FROM extraction_state").fetchone()[0], "PENDING")
                repaired = self.indexer.scan(self.root)
                self.assertEqual(repaired.indexed, 1)
                self.assertEqual(engine.search_page("latest").total_count, 1)

    def test_delete_during_scan_does_not_resurrect_file(self):
        self.path.write_text("new revision", encoding="utf-8")
        def deleting(*args, **kwargs):
            yield DocumentChunk(0, "行 1-1", "resurrected")
            self.path.unlink()
        with patch("docseek.indexer.iter_document_chunks", deleting):
            stats = self.indexer.scan(self.root)
        self.assertEqual(stats.indexed, 0)
        self.assertEqual(stats.removed, 1)
        with self.indexer.chunk_store.connect() as conn:
            for table in ("files", "chunks", "chunk_structure", "extraction_state"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_cancel_after_last_chunk_does_not_commit(self):
        self.path.write_text("new revision", encoding="utf-8")
        def cancelling(*args, **kwargs):
            yield DocumentChunk(0, "行 1-1", "cancelled")
            self.indexer.cancel()
        with patch("docseek.indexer.iter_document_chunks", cancelling):
            with self.assertRaises(IndexCancelled):
                self.indexer.update_paths([self.path])
        engine = ExactGroupedSearchEngine(self.indexer.chunk_store)
        self.assertEqual(engine.search_page("cancelled").total_count, 0)
        self.assertEqual(engine.search_page("original").total_count, 1)


if __name__ == "__main__":
    unittest.main()
