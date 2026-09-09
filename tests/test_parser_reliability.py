from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunks import DocumentChunk
from docseek.document_adapters import DirectDocumentAdapter, DocumentAdapterRegistry
from docseek.extraction_broker import ContentExtractionBroker
from docseek.extraction_state import ExtractionStatus
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.legacy_isolation import LegacyExtractionCancelled
from docseek.search_db import SearchDatabase
from docseek.scan_backend import PythonScanBackend


class ParserReliabilityTests(unittest.TestCase):
    def test_direct_xlsx_isolated_when_indexer_supplies_cancellation(self) -> None:
        broker = ContentExtractionBroker(
            DocumentAdapterRegistry((DirectDocumentAdapter(),))
        )
        path = Path("hang.xlsx")
        expected = [DocumentChunk(0, "表格 1", "正文")]
        cancelled = lambda: False
        with patch(
            "docseek.legacy_isolation.iter_legacy_chunks_isolated",
            return_value=iter(expected),
        ) as isolated:
            actual = list(broker.iter_chunks(path, cancelled=cancelled))

        self.assertEqual(actual, expected)
        isolated.assert_called_once_with(path, cancelled=cancelled)

    def test_dead_parser_lease_is_interrupted_and_deferred_on_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            target = root / "stuck.xlsx"
            target.write_text("placeholder", encoding="utf-8")
            database = SearchDatabase(base / "index.db")
            indexer = DirectoryIndexer(database)
            stat = target.stat()
            with indexer.chunk_store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO extraction_state(
                        path, revision, status, updated_at,
                        source_modified_time, source_size, failure_count,
                        retry_after, owner_pid, started_at
                    ) VALUES (?, 1, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        str(target.resolve()),
                        str(ExtractionStatus.EXTRACTING),
                        time.time() - 60,
                        stat.st_mtime,
                        stat.st_size,
                        999999999,
                        time.time() - 60,
                    ),
                )

            with patch(
                "docseek.indexer.iter_document_chunks",
                side_effect=AssertionError("interrupted parser must not auto-retry"),
            ):
                stats = indexer.scan(root)

            self.assertEqual(stats.indexed, 0)
            self.assertEqual(stats.skipped, 1)
            with indexer.chunk_store.connect() as conn:
                state = conn.execute(
                    "SELECT status, owner_pid FROM extraction_state WHERE path = ?",
                    (str(target.resolve()),),
                ).fetchone()
            self.assertEqual(str(state["status"]), ExtractionStatus.INTERRUPTED)
            self.assertIsNone(state["owner_pid"])
            self.assertEqual(indexer.issues.list(limit=1)[0].error_code, "ParserInterrupted")

    def test_third_dead_parser_lease_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            target = root / "quarantine.xlsx"
            target.write_text("placeholder", encoding="utf-8")
            database = SearchDatabase(base / "index.db")
            indexer = DirectoryIndexer(database)
            stat = target.stat()
            with indexer.chunk_store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO extraction_state(
                        path, revision, status, updated_at,
                        source_modified_time, source_size, failure_count,
                        retry_after, owner_pid, started_at
                    ) VALUES (?, 1, ?, ?, ?, ?, 2, 0, ?, ?)
                    """,
                    (
                        str(target.resolve()),
                        str(ExtractionStatus.EXTRACTING),
                        time.time() - 60,
                        stat.st_mtime,
                        stat.st_size,
                        999999999,
                        time.time() - 60,
                    ),
                )

            stats = indexer.scan(root)
            self.assertEqual(stats.skipped, 1)
            with indexer.chunk_store.connect() as conn:
                status = conn.execute(
                    "SELECT status FROM extraction_state WHERE path = ?",
                    (str(target.resolve()),),
                ).fetchone()[0]
            self.assertEqual(str(status), ExtractionStatus.QUARANTINED)
            self.assertEqual(indexer.issues.list(limit=1)[0].error_code, "ParserQuarantined")

    def test_scan_stop_keeps_committed_neighbors_and_returns_parser_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            first = root / "before-stop.txt"
            stuck = root / "stuck.xlsx"
            first.write_text("这份内容在停止前已经提交", encoding="utf-8")
            stuck.write_bytes(b"placeholder")
            database = SearchDatabase(base / "index.db")
            # This test exercises parser cancellation and deliberately patches
            # Python's priority iterator to make the committed neighbor
            # deterministic. Rust's same-lane filesystem encounter order is
            # intentionally not part of the cross-backend contract.
            indexer = DirectoryIndexer(database, scan_backend=PythonScanBackend())

            def extract(path: Path, **_kwargs):
                if path.suffix.lower() == ".xlsx":
                    raise LegacyExtractionCancelled("user stop")
                return iter([DocumentChunk(0, "行 1", "停止前批次")])

            with (
                patch("docseek.indexer.iter_document_chunks", side_effect=extract),
                patch(
                    "docseek.indexer.prioritize_index_candidates",
                    side_effect=lambda candidates: iter(candidates),
                ),
                patch(
                    "docseek.scan_backend.prioritize_index_candidates",
                    side_effect=lambda candidates: iter(candidates),
                ),
            ):
                with self.assertRaises(IndexCancelled):
                    indexer.scan(root)

            self.assertEqual(
                [row.filename for row in indexer.chunk_store.search("停止前批次")],
                [first.name],
            )
            with indexer.chunk_store.connect() as conn:
                state = conn.execute(
                    "SELECT status, owner_pid, started_at, retry_after "
                    "FROM extraction_state WHERE path = ?",
                    (str(stuck.resolve()),),
                ).fetchone()
            self.assertEqual(str(state["status"]), ExtractionStatus.PENDING)
            self.assertIsNone(state["owner_pid"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(float(state["retry_after"]), 0.0)

    def test_parser_lease_flushes_text_batch_before_metadata_write(self) -> None:
        """A legacy parser lease must not race an open text batch transaction."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            (root / "a.txt").write_text("文本 A", encoding="utf-8")
            (root / "b.txt").write_text("文本 B", encoding="utf-8")
            legacy = root / "legacy.xls"
            legacy.write_bytes(b"placeholder")
            database = SearchDatabase(base / "index.db")
            indexer = DirectoryIndexer(database)

            def extract(path: Path, **_kwargs):
                return iter([DocumentChunk(0, "内容块 1", f"锁测试 {path.name}")])

            with patch("docseek.indexer.iter_document_chunks", side_effect=extract):
                stats = indexer.scan(root)

            self.assertEqual(stats.indexed, 3)
            self.assertEqual(stats.skipped, 0)
            self.assertEqual(indexer.issues.count(), 0)
            with indexer.chunk_store.connect() as conn:
                state = conn.execute(
                    "SELECT status FROM extraction_state WHERE path = ?",
                    (str(legacy.resolve()),),
                ).fetchone()
            self.assertIsNotNone(state)
            self.assertEqual(str(state["status"]), ExtractionStatus.INDEXED)


if __name__ == "__main__":
    unittest.main()
