from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docseek.chunk_store import ChunkStore
from docseek.extraction_state import ExtractionStatus
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class ExtractionRetryBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "docs"
        self.root.mkdir()
        self.db_path = self.base / "docseek.db"
        self.database = SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _state(self, path: Path):
        with self.store.connect() as conn:
            return conn.execute(
                """
                SELECT status, source_modified_time, source_size,
                       failure_count, retry_after, updated_at
                FROM extraction_state
                WHERE path = ?
                """,
                (str(path.resolve()),),
            ).fetchone()

    def test_full_scan_defers_unchanged_initial_failure(self) -> None:
        target = self.root / "broken.txt"
        target.write_text("暂时无法解析", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ) as parser:
            first = DirectoryIndexer(self.database).scan(self.root)
            self.assertEqual(parser.call_count, 1)

        state = self._state(target)
        self.assertEqual(first.skipped, 1)
        self.assertEqual(str(state["status"]), ExtractionStatus.FAILED)
        self.assertEqual(int(state["failure_count"]), 1)
        self.assertEqual(int(state["source_size"]), target.stat().st_size)
        self.assertGreater(float(state["retry_after"]), float(state["updated_at"]))

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=AssertionError("deferred file must not be reparsed"),
        ) as parser:
            second = DirectoryIndexer(self.database).scan(self.root)
            self.assertEqual(parser.call_count, 0)

        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(DirectoryIndexer(self.database).issues.count(), 1)

    def test_precise_retry_bypasses_full_scan_backoff(self) -> None:
        target = self.root / "manual-retry.txt"
        target.write_text("人工重试后应建立索引", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ):
            DirectoryIndexer(self.database).scan(self.root)

        retried = DirectoryIndexer(self.database).update_paths([target])
        self.assertEqual(retried.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)
        self.assertEqual(
            [row.filename for row in self.store.search("人工重试")],
            [target.name],
        )
        self.assertEqual(DirectoryIndexer(self.database).issues.count(), 0)

    def test_file_change_reactivates_before_retry_deadline(self) -> None:
        target = self.root / "changed.txt"
        target.write_text("旧内容", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ):
            DirectoryIndexer(self.database).scan(self.root)

        failed_state = self._state(target)
        old_mtime = float(failed_state["source_modified_time"])
        target.write_text("文件变化后立即重新解析", encoding="utf-8")
        target.touch()
        if target.stat().st_mtime == old_mtime:
            target.touch()
            stat = target.stat()
            target.touch()
            self.assertGreaterEqual(target.stat().st_mtime, stat.st_mtime)

        recovered = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(recovered.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)
        self.assertEqual(
            [row.filename for row in self.store.search("立即重新解析")],
            [target.name],
        )

    def test_expired_backoff_allows_full_scan_retry(self) -> None:
        target = self.root / "expired.txt"
        target.write_text("退避到期后恢复", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ):
            DirectoryIndexer(self.database).scan(self.root)

        with self.store.connect() as conn:
            conn.execute(
                "UPDATE extraction_state SET retry_after = 0 WHERE path = ?",
                (str(target.resolve()),),
            )

        retried = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(retried.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)

    def test_repeated_precise_failures_increase_backoff(self) -> None:
        target = self.root / "repeat.txt"
        target.write_text("持续失败", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ):
            DirectoryIndexer(self.database).update_paths([target])
            first = self._state(target)
            DirectoryIndexer(self.database).update_paths([target])
            second = self._state(target)

        self.assertEqual(int(first["failure_count"]), 1)
        self.assertEqual(int(second["failure_count"]), 2)
        first_delay = float(first["retry_after"]) - float(first["updated_at"])
        second_delay = float(second["retry_after"]) - float(second["updated_at"])
        self.assertGreater(second_delay, first_delay)


if __name__ == "__main__":
    unittest.main()
