from __future__ import annotations

import queue
import tempfile
import time
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase
from docseek.watcher import WatchBatch, WatchManager


class WatcherIntegrationTests(unittest.TestCase):
    def test_real_filesystem_modify_event_reaches_incremental_indexer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "documents"
            root.mkdir()
            target = root / "guide.txt"
            target.write_text("watcherinitialmarker 客户资料", encoding="utf-8")

            database = SearchDatabase(base / "docseek.db")
            chunks = ChunkStore(database.db_path)
            DirectoryIndexer(database).scan(root)
            self.assertEqual(len(chunks.search("watcherinitialmarker")), 1)

            batches: queue.Queue[WatchBatch] = queue.Queue()
            manager = WatchManager(batches.put, debounce_seconds=0.05)
            try:
                manager.start([str(root)], database.get_excluded_paths())
                time.sleep(0.20)
                target.write_text(
                    "watcherupdatemarker 客户资料 已修改并需要增量更新",
                    encoding="utf-8",
                )

                deadline = time.monotonic() + 5.0
                updated = False
                while time.monotonic() < deadline and not updated:
                    remaining = max(0.05, deadline - time.monotonic())
                    try:
                        batch = batches.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if batch.full_rescan:
                        DirectoryIndexer(database).scan(root)
                    elif batch.paths:
                        DirectoryIndexer(database).update_paths(
                            [Path(path) for path in batch.paths]
                        )
                    updated = bool(chunks.search("watcherupdatemarker"))

                self.assertTrue(updated, "真实文件修改事件没有在 5 秒内进入增量索引")
                self.assertEqual(chunks.search("watcherinitialmarker"), [])
            finally:
                manager.stop()


if __name__ == "__main__":
    unittest.main()
