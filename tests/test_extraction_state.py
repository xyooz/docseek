from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.extraction_state import ExtractionStatus, failed_state_is_deferred
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.legacy_isolation import LegacyExtractionCancelled, LegacyExtractionTimeout
from docseek.schema import CURRENT_SCHEMA_VERSION
from docseek.search_db import SearchDatabase


class ExtractionStateTests(unittest.TestCase):
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
        normalized = str(path.resolve())
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT revision, status, updated_at FROM extraction_state WHERE path = ?",
                (normalized,),
            ).fetchone()

    def test_empty_text_is_no_text_and_is_not_reparsed_forever(self) -> None:
        target = self.root / "empty.txt"
        target.write_text("", encoding="utf-8")

        first = DirectoryIndexer(self.database).scan(self.root)
        state = self._state(target)

        self.assertEqual(first.indexed, 1)
        self.assertEqual(first.no_text, 1)
        self.assertEqual(first.chunks, 0)
        self.assertEqual(str(state["status"]), ExtractionStatus.NO_TEXT)
        self.assertGreater(float(state["updated_at"]), 0)

        first_updated_at = float(state["updated_at"])
        second = DirectoryIndexer(self.database).scan(self.root)
        second_state = self._state(target)

        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(float(second_state["updated_at"]), first_updated_at)

    def test_textless_pdf_is_ocr_required_and_is_not_reparsed(self) -> None:
        target = self.root / "scan.pdf"
        document = fitz.open()
        document.new_page()
        document.save(target)
        document.close()

        first = DirectoryIndexer(self.database).scan(self.root)
        state = self._state(target)

        self.assertEqual(first.indexed, 1)
        self.assertEqual(first.ocr_required, 1)
        self.assertEqual(first.chunks, 0)
        self.assertEqual(str(state["status"]), ExtractionStatus.OCR_REQUIRED)
        self.assertEqual(self.store.search("扫描件正文"), [])

        second = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.unchanged, 1)

    def test_v8_zero_chunk_row_is_repaired_once_after_upgrade(self) -> None:
        target = self.root / "legacy-empty.txt"
        target.write_text("", encoding="utf-8")
        stat = target.stat()
        normalized = str(target.resolve())

        self.store.replace_document(
            path=normalized,
            filename=target.name,
            extension=target.suffix,
            modified_time=stat.st_mtime,
            size=stat.st_size,
            chunks=[],
        )
        with self.store.connect() as conn:
            conn.execute("DROP TABLE extraction_state")
            conn.execute(
                "CREATE TABLE extraction_state(path TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO extraction_state(path, revision) VALUES (?, 1)",
                (normalized,),
            )
            conn.execute("PRAGMA user_version = 8")

        self.store = ChunkStore(self.db_path)
        with self.store.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            migrated = conn.execute(
                "SELECT status, updated_at FROM extraction_state WHERE path = ?",
                (normalized,),
            ).fetchone()
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(str(migrated["status"]), ExtractionStatus.INDEXED)
        self.assertEqual(float(migrated["updated_at"]), 0.0)

        repaired = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(repaired.indexed, 1)
        self.assertEqual(repaired.no_text, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.NO_TEXT)

        stable = DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual(stable.indexed, 0)
        self.assertEqual(stable.unchanged, 1)

    def test_failed_refresh_keeps_previous_searchable_content_and_recovers(self) -> None:
        target = self.root / "policy.txt"
        target.write_text("旧版信贷规则", encoding="utf-8")
        DirectoryIndexer(self.database).scan(self.root)
        self.assertEqual([row.filename for row in self.store.search("旧版信贷")], [target.name])

        target.write_text("新版跨境规则", encoding="utf-8")
        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("synthetic parser failure"),
        ):
            failed = DirectoryIndexer(self.database).update_paths([target])

        self.assertEqual(failed.skipped, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.FAILED)
        self.assertEqual([row.filename for row in self.store.search("旧版信贷")], [target.name])
        self.assertEqual(self.store.search("新版跨境"), [])

        recovered = DirectoryIndexer(self.database).update_paths([target])
        self.assertEqual(recovered.indexed, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.INDEXED)
        self.assertEqual(self.store.search("旧版信贷"), [])
        self.assertEqual([row.filename for row in self.store.search("新版跨境")], [target.name])
        self.assertEqual(DirectoryIndexer(self.database).issues.count(), 0)

    def test_parser_timeout_is_recorded_as_timeout_state(self) -> None:
        target = self.root / "timeout.txt"
        target.write_text("等待解析", encoding="utf-8")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=LegacyExtractionTimeout("synthetic timeout"),
        ):
            stats = DirectoryIndexer(self.database).update_paths([target])

        self.assertEqual(stats.skipped, 1)
        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.TIMEOUT)
        issue = DirectoryIndexer(self.database).issues.list(limit=1)[0]
        self.assertEqual(issue.error_code, "LegacyExtractionTimeout")

    def test_manual_deferred_states_do_not_depend_on_a_fake_retry_date(self) -> None:
        common = dict(
            stored_revision=1,
            current_revision=1,
            source_modified_time=12.0,
            source_size=42,
            current_modified_time=12.0,
            current_size=42,
            retry_after=0,
            now=10_000_000_000,
        )
        for status in (
            ExtractionStatus.INTERRUPTED,
            ExtractionStatus.SKIPPED,
            ExtractionStatus.QUARANTINED,
        ):
            with self.subTest(status=status):
                self.assertTrue(failed_state_is_deferred(status, **common))

        self.assertFalse(
            failed_state_is_deferred(
                ExtractionStatus.INTERRUPTED,
                **{**common, "current_size": 43},
            )
        )

    def test_stopping_parser_returns_extracting_file_to_pending(self) -> None:
        target = self.root / "large.xlsx"
        target.write_bytes(b"placeholder")
        indexer = DirectoryIndexer(self.database)
        normalized = str(target.resolve())
        revision = 1
        indexer._record_extraction_started(normalized, revision, target.stat())
        indexer._record_parser_job_cancelled(normalized)

        with self.store.connect() as conn:
            state = conn.execute(
                """
                SELECT status, retry_after, failure_count, owner_pid, started_at
                FROM extraction_state WHERE path = ?
                """,
                (normalized,),
            ).fetchone()
        self.assertEqual(str(state["status"]), ExtractionStatus.PENDING)
        self.assertEqual(float(state["retry_after"]), 0.0)
        self.assertEqual(int(state["failure_count"]), 0)
        self.assertIsNone(state["owner_pid"])
        self.assertIsNone(state["started_at"])

        # PENDING is deliberately retryable on the next explicit/full scan.
        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            return_value=iter([DocumentChunk(0, "工作表 Sheet1 · 行 1", "恢复内容")]),
        ):
            stats = indexer.update_paths([target])
        self.assertEqual(stats.indexed, 1)

    def test_isolated_cancel_exception_uses_pending_state_not_skipped(self) -> None:
        target = self.root / "cancelled.xlsx"
        target.write_bytes(b"placeholder")
        indexer = DirectoryIndexer(self.database)

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=LegacyExtractionCancelled("user stop"),
        ):
            with self.assertRaises(IndexCancelled):
                indexer.update_paths([target])

        self.assertEqual(str(self._state(target)["status"]), ExtractionStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
