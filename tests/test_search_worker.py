from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase
from docseek.search_worker import SearchRequest, SearchResponse, SearchWorker


class SearchWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.store.replace_document(
            path=r"C:\docs\manual.pdf",
            filename="manual.pdf",
            extension=".pdf",
            modified_time=1.0,
            size=100,
            chunks=[DocumentChunk(0, "第 1 页", "客户经理办理信贷业务")],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_worker_returns_exact_page_with_generation(self) -> None:
        request = SearchRequest(
            generation=7,
            query="信贷",
            limit=100,
            offset=0,
            select_first=True,
        )
        worker = SearchWorker(self.store, request)
        responses: list[SearchResponse] = []
        errors: list[tuple[int, str]] = []
        worker.signals.finished.connect(responses.append)
        worker.signals.failed.connect(lambda generation, message: errors.append((generation, message)))

        worker.run()

        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertEqual(response.request.generation, 7)
        self.assertTrue(response.request.select_first)
        self.assertEqual(response.page.total_count, 1)
        self.assertEqual(response.page.items[0].filename, "manual.pdf")
        self.assertGreaterEqual(response.elapsed_ms, 0.0)

    def test_worker_propagates_search_failure_with_generation(self) -> None:
        request = SearchRequest(generation=11, query="信贷", limit=100, offset=0)
        worker = SearchWorker(self.store, request)
        errors: list[tuple[int, str]] = []
        worker.signals.failed.connect(lambda generation, message: errors.append((generation, message)))

        original = self.store.search_page

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic failure")

        self.store.search_page = fail  # type: ignore[method-assign]
        try:
            worker.run()
        finally:
            self.store.search_page = original  # type: ignore[method-assign]

        self.assertEqual(errors, [(11, "synthetic failure")])


if __name__ == "__main__":
    unittest.main()
