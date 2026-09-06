from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.extraction_revision import current_extraction_revision
from docseek.extraction_state import ExtractionStatus
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class IndexCrashRecoveryTests(unittest.TestCase):
    """Regression tests for restart-safe, idempotent indexing behavior.

    These cases model the durable-state semantics used by mature local indexers:
    incomplete work is retried after restart, previously committed content stays
    searchable until replacement commits, and a successful repair becomes
    idempotent on later reconciliations.
    """

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

    def _state(self, target: Path):
        normalized = str(target.resolve())
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT revision, status, updated_at FROM extraction_state WHERE path = ?",
                (normalized,),
            ).fetchone()

    def _force_state(self, target: Path, status: ExtractionStatus) -> None:
        normalized = str(target.resolve())
        revision = current_extraction_revision(target.suffix)
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_state(path, revision, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (normalized, revision, str(status), time.time()),
            )

    def test_restart_repairs_stale_extracting_state_then_becomes_idempotent(self) -> None:
        target = self.root / "policy.txt"
        target.write_text("信贷政策操作指引", encoding="utf-8")
        first = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(first.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)

        # Simulate a process dying after durable work state was written but before
        # the next extraction attempt could reach its terminal commit state.
        self._force_state(target, ExtractionStatus.EXTRACTING)

        repaired = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(repaired.indexed, 1)
        self.assertEqual(repaired.unchanged, 0)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)
        self.assertEqual(
            [row.filename for row in self.store.search("信贷政策")],
            [target.name],
        )

        stable = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(stable.indexed, 0)
        self.assertEqual(stable.unchanged, 1)

    def test_all_transient_states_are_restart_repairable(self) -> None:
        for status in (
            ExtractionStatus.PENDING,
            ExtractionStatus.EXTRACTING,
            ExtractionStatus.READY_TO_COMMIT,
        ):
            with self.subTest(status=status):
                target = self.root / f"{status.value.lower()}.txt"
                target.write_text(f"恢复测试 {status.value}", encoding="utf-8")
                DirectoryIndexer(self.database).scan(self.root)
                self._force_state(target, status)

                repaired = DirectoryIndexer(self.database).update_paths([target])
                self.assertEqual(repaired.indexed, 1)
                self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)

    def test_failed_state_is_retried_even_when_file_metadata_is_unchanged(self) -> None:
        target = self.root / "retry.txt"
        target.write_text("跨境业务恢复测试", encoding="utf-8")
        DirectoryIndexer(self.database).scan(self.root)
        before = target.stat()

        self._force_state(target, ExtractionStatus.FAILED)
        repaired = DirectoryIndexer(self.database).scan(self.root)
        after = target.stat()

        self.assertEqual(before.st_mtime, after.st_mtime)
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(repaired.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)

    def test_timeout_state_is_retried_even_when_file_metadata_is_unchanged(self) -> None:
        target = self.root / "timeout-retry.txt"
        target.write_text("超时恢复测试", encoding="utf-8")
        DirectoryIndexer(self.database).scan(self.root)

        self._force_state(target, ExtractionStatus.TIMEOUT)
        repaired = DirectoryIndexer(self.database).update_paths([target])

        self.assertEqual(repaired.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)


if __name__ == "__main__":
    unittest.main()
