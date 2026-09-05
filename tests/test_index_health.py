from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from docseek.index_health import (
    IndexHealthSnapshot,
    capture_index_health,
    format_index_health,
    format_reconcile_time,
    format_storage_size,
    get_last_successful_reconcile,
    index_storage_bytes,
    record_successful_reconcile,
)
from docseek.index_issues import IndexIssueStore
from docseek.index_root_state import IndexRootStateStore
from docseek.search_db import SearchDatabase


class IndexHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        self.db = SearchDatabase(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_storage_size_includes_sqlite_sidecars(self) -> None:
        # Use dedicated dummy paths so SQLite itself cannot resize the files
        # while the assertion is being made.
        target = Path(self.temp_dir.name) / "footprint.db"
        target.write_bytes(b"a" * 100)
        Path(f"{target}-wal").write_bytes(b"b" * 20)
        Path(f"{target}-shm").write_bytes(b"c" * 5)
        self.assertEqual(index_storage_bytes(target), 125)

    def test_snapshot_reports_files_roots_pauses_and_issues(self) -> None:
        root_a = Path(self.temp_dir.name) / "A"
        root_b = Path(self.temp_dir.name) / "B"
        root_a.mkdir()
        root_b.mkdir()
        self.db.add_index_root(str(root_a))
        self.db.add_index_root(str(root_b))
        IndexRootStateStore(self.db).set_paused(root_b, True)

        self.db.upsert_document(
            path=str((root_a / "guide.txt").resolve()),
            filename="guide.txt",
            extension=".txt",
            modified_time=1.0,
            size=10,
            content="客户服务",
        )
        IndexIssueStore(self.db_path).record(
            str((root_a / "broken.pdf").resolve()),
            "os_error",
            "temporary",
        )

        snapshot = capture_index_health(self.db)

        self.assertEqual(snapshot.indexed_files, 1)
        self.assertGreater(snapshot.storage_bytes, 0)
        self.assertEqual(snapshot.total_roots, 2)
        self.assertEqual(snapshot.active_roots, 1)
        self.assertEqual(snapshot.paused_roots, 1)
        self.assertEqual(snapshot.issue_count, 1)

        text = format_index_health(snapshot)
        self.assertIn("1 个文件", text)
        self.assertIn("目录 2（监测 1 / 暂停 1）", text)
        self.assertIn("问题 1", text)

    def test_reconcile_timestamp_round_trip_and_invalid_values(self) -> None:
        local_time = datetime(2026, 1, 2, 3, 4)
        timestamp = local_time.timestamp()
        recorded = record_successful_reconcile(self.db, completed_at=timestamp)
        self.assertEqual(recorded, timestamp)
        self.assertEqual(get_last_successful_reconcile(self.db), timestamp)
        self.assertEqual(format_reconcile_time(timestamp), "2026-01-02 03:04")

        self.db._set_setting("last_successful_reconcile_at_v1", "not-a-time")
        self.assertIsNone(get_last_successful_reconcile(self.db))
        self.assertIn("尚未记录", format_reconcile_time(None))

    def test_format_storage_size(self) -> None:
        self.assertEqual(format_storage_size(512), "512 B")
        self.assertEqual(format_storage_size(1536), "1.5 KB")
        self.assertEqual(format_storage_size(2 * 1024 * 1024), "2.0 MB")

    def test_snapshot_dataclass_keeps_reconcile_field_explicit(self) -> None:
        snapshot = IndexHealthSnapshot(
            indexed_files=0,
            storage_bytes=0,
            total_roots=0,
            active_roots=0,
            paused_roots=0,
            issue_count=0,
            last_successful_reconcile_at=None,
        )
        self.assertIsNone(snapshot.last_successful_reconcile_at)


if __name__ == "__main__":
    unittest.main()
