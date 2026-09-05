from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase
from docseek.search_session import close_thread_search_store, get_thread_search_store
from docseek.search_worker import SearchRequest, SearchResponse, SearchWorker


class SearchWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        close_thread_search_store()
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
            chunks=[
                DocumentChunk(0, "第 1 页", "客户经理办理信贷业务"),
                DocumentChunk(1, "第 2 页", "客户经理办理信贷业务"),
            ],
        )

    def tearDown(self) -> None:
        close_thread_search_store()
        self.temp_dir.cleanup()

    def test_older_queued_generation_becomes_noop(self) -> None:
        old = SearchWorker(
            self.store,
            SearchRequest(generation=2001, query="信贷", limit=100, offset=0),
        )
        SearchWorker(
            self.store,
            SearchRequest(generation=2002, query="客户经理", limit=100, offset=0),
        )
        responses: list[SearchResponse] = []
        old.signals.finished.connect(responses.append)

        old.run()

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].request.generation, 2001)
        self.assertEqual(responses[0].page.items, [])
        self.assertEqual(responses[0].page.total_count, 0)

    def test_search_session_is_reused_on_same_thread(self) -> None:
        first = get_thread_search_store(self.db_path)
        second = get_thread_search_store(self.db_path)
        self.assertIs(first, second)
        self.assertEqual(first.search("信贷")[0].filename, "manual.pdf")

    def test_worker_propagates_search_failure_with_generation(self) -> None:
        request = SearchRequest(generation=3011, query="信贷", limit=100, offset=0)
        worker = SearchWorker(self.store, request)
        errors: list[tuple[int, str]] = []
        worker.signals.failed.connect(lambda generation, message: errors.append((generation, message)))

        class FailingEngine:
            def search_page(self, *args, **kwargs):
                raise RuntimeError("synthetic failure")

        with patch("docseek.search_worker.ExactGroupedSearchEngine", return_value=FailingEngine()):
            worker.run()

        self.assertEqual(errors, [(3011, "synthetic failure")])

    def test_worker_returns_exact_page_with_generation(self) -> None:
        request = SearchRequest(
            generation=4007,
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
        self.assertEqual(response.request.generation, 4007)
        self.assertTrue(response.request.select_first)
        self.assertEqual(response.page.total_count, 1)
        self.assertEqual(response.page.items[0].filename, "manual.pdf")
        self.assertGreaterEqual(response.elapsed_ms, 0.0)

    def test_worker_applies_page_hint_to_best_chunk(self) -> None:
        request = SearchRequest(
            generation=5001,
            query="信贷 page:2",
            limit=100,
            offset=0,
        )
        worker = SearchWorker(self.store, request)
        responses: list[SearchResponse] = []
        errors: list[tuple[int, str]] = []
        worker.signals.finished.connect(responses.append)
        worker.signals.failed.connect(lambda generation, message: errors.append((generation, message)))

        worker.run()

        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].page.items[0].location, "第 2 页")
        self.assertNotIn("page:2", responses[0].page.items[0].snippet)


if __name__ == "__main__":
    unittest.main()
