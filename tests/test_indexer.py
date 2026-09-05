from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from docseek.chunk_store import ChunkStore
from docseek.index_issues import IndexIssueStore
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class DirectoryIndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.root.mkdir()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")
        self.chunks = ChunkStore(self.db.db_path)
        self.issues = IndexIssueStore(self.db.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_excluded_directory_is_not_indexed(self) -> None:
        visible = self.root / "visible.txt"
        visible.write_text("公开工作资料 信贷", encoding="utf-8")
        private_dir = self.root / "private"
        private_dir.mkdir()
        (private_dir / "secret.txt").write_text("不应索引的内部资料", encoding="utf-8")

        self.db.add_excluded_path(str(private_dir))
        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        self.assertGreaterEqual(stats.excluded, 1)
        self.assertEqual([row.filename for row in self.chunks.search("信贷")], ["visible.txt"])
        self.assertEqual(self.chunks.search("内部资料"), [])

    def test_second_scan_skips_unchanged_file(self) -> None:
        target = self.root / "guide.txt"
        target.write_text("客户服务操作指引", encoding="utf-8")

        first = DirectoryIndexer(self.db).scan(self.root)
        second = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(first.indexed, 1)
        self.assertGreaterEqual(first.chunks, 1)
        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.unchanged, 1)

    def test_full_scan_uses_prefetched_state_instead_of_per_file_metadata_queries(self) -> None:
        for index in range(5):
            (self.root / f"guide_{index}.txt").write_text(
                f"客户服务操作指引 {index}", encoding="utf-8"
            )
        DirectoryIndexer(self.db).scan(self.root)

        with patch.object(
            self.db,
            "is_unchanged",
            side_effect=AssertionError("full scan should use prefetched state"),
        ):
            stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 0)
        self.assertEqual(stats.unchanged, 5)

    def test_pre_chunk_file_is_migrated_even_when_unchanged(self) -> None:
        target = self.root / "legacy.txt"
        target.write_text("历史制度材料 信贷", encoding="utf-8")
        stat = target.stat()
        self.db.upsert_document(
            path=str(target.resolve()),
            filename=target.name,
            extension=".txt",
            modified_time=stat.st_mtime,
            size=stat.st_size,
            content="历史制度材料 信贷",
        )

        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        rows = self.chunks.search("信贷")
        self.assertEqual([row.filename for row in rows], ["legacy.txt"])
        self.assertTrue(rows[0].location.startswith("行 "))

    def test_precise_update_reindexes_only_changed_file(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("旧内容 信贷", encoding="utf-8")
        second.write_text("保持不变 客户", encoding="utf-8")
        DirectoryIndexer(self.db).scan(self.root)

        first.write_text("新的精准增量内容 风控资料", encoding="utf-8")
        stats = DirectoryIndexer(self.db).update_paths([first])

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.indexed, 1)
        self.assertEqual([row.filename for row in self.chunks.search("风控资料")], ["first.txt"])
        self.assertEqual(self.chunks.search("旧内容"), [])
        self.assertEqual([row.filename for row in self.chunks.search("保持不变")], ["second.txt"])

    def test_precise_update_removes_deleted_file(self) -> None:
        target = self.root / "deleted.txt"
        target.write_text("即将删除 信贷资料", encoding="utf-8")
        DirectoryIndexer(self.db).scan(self.root)
        self.assertEqual(len(self.chunks.search("即将删除")), 1)

        target.unlink()
        stats = DirectoryIndexer(self.db).update_paths([target])

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.chunks.search("即将删除"), [])

    def test_file_growing_over_limit_removes_stale_index(self) -> None:
        target = self.root / "growing.txt"
        target.write_text("原先可检索 信贷内容", encoding="utf-8")
        DirectoryIndexer(self.db, max_file_size=1024).scan(self.root)
        self.assertEqual(len(self.chunks.search("原先可检索")), 1)

        target.write_text("X" * 2000, encoding="utf-8")
        stats = DirectoryIndexer(self.db, max_file_size=100).update_paths([target])

        self.assertEqual(stats.removed, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(self.chunks.search("原先可检索"), [])
        self.assertEqual(self.issues.list()[0].error_code, "file_too_large")

    def test_oversized_new_file_is_persisted_as_issue(self) -> None:
        target = self.root / "large.txt"
        target.write_text("超过限制的文件内容", encoding="utf-8")

        stats = DirectoryIndexer(self.db, max_file_size=4).scan(self.root)

        self.assertEqual(stats.indexed, 0)
        self.assertEqual(stats.skipped, 1)
        issues = self.issues.list()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, str(target.resolve()))
        self.assertEqual(issues[0].error_code, "file_too_large")
        self.assertEqual(self.db.count_files(), 0)

    def test_issue_is_cleared_after_successful_retry(self) -> None:
        target = self.root / "retry.txt"
        target.write_text("客户经理信贷资料", encoding="utf-8")

        DirectoryIndexer(self.db, max_file_size=4).scan(self.root)
        self.assertEqual(self.issues.count(), 1)

        stats = DirectoryIndexer(self.db, max_file_size=1024 * 1024).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(self.issues.count(), 0)
        self.assertEqual([row.filename for row in self.chunks.search("信贷")], ["retry.txt"])

    def test_xlsx_detail_progress_is_propagated_during_scan(self) -> None:
        target = self.root / "客户清单.xlsx"
        workbook = Workbook(write_only=True)
        worksheet = workbook.create_sheet("客户明细")
        for row_no in range(1, 1_506):
            worksheet.append([row_no, f"客户{row_no}"])
        workbook.save(target)
        workbook.close()

        progress: list[tuple[str, str, int]] = []
        stats = DirectoryIndexer(self.db).scan(
            self.root,
            on_detail=lambda path, location, current: progress.append(
                (path.name, location, current)
            ),
        )

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(
            progress,
            [
                ("客户清单.xlsx", "工作表 客户明细", 1_000),
                ("客户清单.xlsx", "工作表 客户明细", 1_505),
            ],
        )


if __name__ == "__main__":
    unittest.main()
