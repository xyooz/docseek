from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from docseek.search_db import SearchDatabase
from docseek.search_session import (
    close_persistent_search_stores,
    close_thread_search_store,
    get_thread_search_store,
)


class SearchSessionLifecycleTests(unittest.TestCase):
    def test_main_thread_can_close_idle_worker_session_and_worker_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            SearchDatabase(db_path)

            ready = threading.Event()
            reopen = threading.Event()
            finished = threading.Event()
            observed: dict[str, object] = {}

            def worker() -> None:
                try:
                    first = get_thread_search_store(db_path)
                    observed["first"] = first
                    # Prove the connection is usable before main-thread shutdown.
                    with first.connect() as conn:
                        observed["version"] = conn.execute("PRAGMA user_version").fetchone()[0]
                    ready.set()
                    reopen.wait(timeout=5)

                    second = get_thread_search_store(db_path)
                    observed["second"] = second
                    observed["second_closed"] = second.closed
                finally:
                    close_thread_search_store()
                    finished.set()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(ready.wait(timeout=5))

            first = observed["first"]
            self.assertFalse(first.closed)  # type: ignore[union-attr]
            self.assertEqual(close_persistent_search_stores(db_path), 1)
            self.assertTrue(first.closed)  # type: ignore[union-attr]

            reopen.set()
            self.assertTrue(finished.wait(timeout=5))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            self.assertIsNot(observed["second"], first)
            self.assertFalse(observed["second_closed"])
            self.assertEqual(close_persistent_search_stores(db_path), 0)


if __name__ == "__main__":
    unittest.main()
