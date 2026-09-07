from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.file_exclusions import FileExclusionStore
from docseek.index_issues import IndexIssueStore
from docseek.index_maintenance import (
    ACTION_REBUILD,
    ACTION_RESET,
    apply_pending_restore,
    create_index_backup,
    pending_restore_path,
    perform_maintenance,
    purge_root_index,
    stage_index_restore,
    validate_index_backup,
)
from docseek.index_root_state import IndexRootStateStore
from docseek.schema import CURRENT_SCHEMA_VERSION
from docseek.search_db import SearchDatabase


class IndexMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = self.base / "docseek.db"
        self.db = SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _index_file(self, path: Path, marker: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(marker, encoding="utf-8")
        stat = path.stat()
        self.store.replace_document(
            path=str(path.resolve()),
            filename=path.name,
            extension=path.suffix.lower(),
            modified_time=stat.st_mtime,
            size=stat.st_size,
            chunks=[DocumentChunk(0, "文本行 1-1", marker)],
        )

    def test_backup_is_consistent_searchable_and_current_schema(self) -> None:
        root = self.base / "docs"
        source = root / "guide.txt"
        self.db.add_index_root(str(root))
        self._index_file(source, "客户经理 信贷业务")

        backup = create_index_backup(self.db_path, self.base / "backup.db")
        validation = validate_index_backup(backup)

        self.assertEqual(validation.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(validation.indexed_files, 1)
        rows = ChunkStore(backup).search("信贷业务")
        self.assertEqual([row.filename for row in rows], ["guide.txt"])

    def test_rebuild_clears_index_but_preserves_scope_and_source_files(self) -> None:
        root = self.base / "docs"
        excluded = root / "private"
        excluded.mkdir(parents=True)
        source = root / "manual.txt"
        source.write_text("身份证有效期维护", encoding="utf-8")
        self.db.add_index_root(str(root))
        self.db.add_excluded_path(str(excluded))
        IndexRootStateStore(self.db).set_paused(root, True)
        FileExclusionStore(self.db).set_patterns(["*.bak"])
        self._index_file(source, "身份证有效期维护")
        IndexIssueStore(self.db_path).record(
            str((root / "broken.pdf").resolve()),
            "os_error",
            "temporary",
        )

        result = perform_maintenance(ACTION_REBUILD, self.db_path)

        self.assertTrue(source.exists())
        self.assertEqual(ChunkStore(self.db_path).search("身份证有效期"), [])
        current = SearchDatabase(self.db_path)
        self.assertEqual(current.get_index_roots(), [str(root.resolve())])
        self.assertEqual(current.get_excluded_paths(), [str(excluded.resolve())])
        self.assertEqual(IndexRootStateStore(current).paused_roots(), [str(root.resolve())])
        self.assertEqual(FileExclusionStore(current).patterns(), ["*.bak"])
        self.assertEqual(IndexIssueStore(self.db_path).count(), 0)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(result.backup_path.exists())
        self.assertEqual(result.cleared_files, 1)

    def test_reset_removes_index_scope_but_keeps_source_and_user_preferences(self) -> None:
        root = self.base / "docs"
        excluded = root / "private"
        excluded.mkdir(parents=True)
        source = root / "notice.txt"
        source.write_text("网点服务规范", encoding="utf-8")
        self.db.add_index_root(str(root))
        self.db.add_excluded_path(str(excluded))
        FileExclusionStore(self.db).set_patterns(["*.tmp"])
        self.db._set_setting("search_history_v1", "keep-history")
        self.db._set_setting("results_header_state_v1", "keep-layout")
        self._index_file(source, "网点服务规范")

        result = perform_maintenance(ACTION_RESET, self.db_path)

        current = SearchDatabase(self.db_path)
        self.assertTrue(source.exists())
        self.assertEqual(ChunkStore(self.db_path).search("网点服务"), [])
        self.assertEqual(current.get_index_roots(), [])
        self.assertEqual(current.get_excluded_paths(), [])
        self.assertEqual(FileExclusionStore(current).patterns(), [])
        self.assertEqual(current._get_setting("search_history_v1"), "keep-history")
        self.assertEqual(current._get_setting("results_header_state_v1"), "keep-layout")
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(result.backup_path.exists())

    def test_purge_root_removes_production_chunks_and_issues_only_under_that_root(self) -> None:
        root_a = self.base / "a"
        root_b = self.base / "b"
        file_a = root_a / "a.txt"
        file_b = root_b / "b.txt"
        self.db.add_index_root(str(root_a))
        self.db.add_index_root(str(root_b))
        self._index_file(file_a, "ROOT_A_ONLY 客户经理")
        self._index_file(file_b, "ROOT_B_ONLY 客户经理")
        issues = IndexIssueStore(self.db_path)
        issues.record(str((root_a / "broken.pdf").resolve()), "os_error", "a")
        issues.record(str((root_b / "broken.pdf").resolve()), "os_error", "b")

        removed = purge_root_index(self.db_path, root_a)

        self.assertEqual(removed, 1)
        self.assertEqual(ChunkStore(self.db_path).search("ROOT_A_ONLY"), [])
        self.assertEqual(
            [row.filename for row in ChunkStore(self.db_path).search("ROOT_B_ONLY")],
            ["b.txt"],
        )
        remaining_issue_paths = {issue.path for issue in issues.list()}
        self.assertNotIn(str((root_a / "broken.pdf").resolve()), remaining_issue_paths)
        self.assertIn(str((root_b / "broken.pdf").resolve()), remaining_issue_paths)
        self.assertTrue(file_a.exists())
        self.assertTrue(file_b.exists())

    def test_staged_restore_replaces_database_on_next_start_and_keeps_safety_backup(self) -> None:
        root = self.base / "docs"
        original = root / "original.txt"
        self.db.add_index_root(str(root))
        self._index_file(original, "恢复前需要保留的制度")
        backup = create_index_backup(self.db_path, self.base / "known-good.db")

        # Mutate the live index after the backup so successful restore is visible.
        self.store.remove_document(str(original.resolve()))
        changed = root / "changed.txt"
        self._index_file(changed, "恢复后不应保留的临时内容")
        self.assertEqual(
            [row.filename for row in ChunkStore(self.db_path).search("临时内容")],
            ["changed.txt"],
        )

        staged = stage_index_restore(self.db_path, backup)
        self.assertEqual(staged, pending_restore_path(self.db_path))
        self.assertTrue(staged.exists())
        # Staging alone never mutates the current live database.
        self.assertEqual(
            [row.filename for row in ChunkStore(self.db_path).search("临时内容")],
            ["changed.txt"],
        )

        applied = apply_pending_restore(self.db_path)

        self.assertIsNotNone(applied)
        self.assertFalse(staged.exists())
        self.assertIsNotNone(applied.safety_backup_path)
        self.assertTrue(applied.safety_backup_path.exists())
        restored = ChunkStore(self.db_path)
        self.assertEqual(
            [row.filename for row in restored.search("制度")],
            ["original.txt"],
        )
        self.assertEqual(restored.search("临时内容"), [])

    def test_stage_restore_rejects_non_docseek_database_without_touching_current_index(self) -> None:
        root = self.base / "docs"
        source = root / "good.txt"
        self.db.add_index_root(str(root))
        self._index_file(source, "当前索引仍然有效")
        invalid = self.base / "not-docseek.db"
        invalid.write_bytes(b"not a sqlite database")

        with self.assertRaises(Exception):
            stage_index_restore(self.db_path, invalid)

        self.assertFalse(pending_restore_path(self.db_path).exists())
        self.assertEqual(
            [row.filename for row in ChunkStore(self.db_path).search("仍然有效")],
            ["good.txt"],
        )


if __name__ == "__main__":
    unittest.main()
