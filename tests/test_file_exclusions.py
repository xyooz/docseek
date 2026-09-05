from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from docseek.chunk_store import ChunkStore
from docseek.file_exclusions import (
    FILE_EXCLUSION_PATTERNS_KEY,
    FileExclusionStore,
    InvalidFileExclusionPattern,
    decode_file_exclusion_patterns,
    matches_file_exclusion,
    normalize_file_exclusion_patterns,
)
from docseek.indexer import DirectoryIndexer
from docseek.pausable_settings_dialog import PausableIndexSettingsDialog
from docseek.search_db import SearchDatabase


class FileExclusionRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.root.mkdir()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")
        self.store = FileExclusionStore(self.db)
        self.chunks = ChunkStore(self.db.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rules_are_case_insensitive_deduplicated_and_support_extension_shorthand(self) -> None:
        patterns = normalize_file_exclusion_patterns(
            [" .LOG ", "*.log", "测试_*", "Exact.DOCX"]
        )
        self.assertEqual(patterns, ["*.LOG", "测试_*", "Exact.DOCX"])
        self.assertTrue(matches_file_exclusion("server.log", patterns))
        self.assertTrue(matches_file_exclusion("测试_客户.txt", patterns))
        self.assertTrue(matches_file_exclusion("exact.docx", patterns))
        self.assertFalse(matches_file_exclusion("客户制度.docx", patterns))

    def test_dangerous_all_file_and_path_rules_are_rejected(self) -> None:
        for pattern in ("*", "*.*", "temp/*.docx", r"temp\*.docx"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(InvalidFileExclusionPattern):
                    normalize_file_exclusion_patterns([pattern])

    def test_persisted_rules_round_trip_and_corrupt_values_degrade_safely(self) -> None:
        self.store.set_patterns(["*.bak", "测试_*"])
        self.assertEqual(self.store.patterns(), ["*.bak", "测试_*"])

        self.db._set_setting(FILE_EXCLUSION_PATTERNS_KEY, "not-json")
        self.assertEqual(self.store.patterns(), [])
        self.db._set_setting(FILE_EXCLUSION_PATTERNS_KEY, '["*", "*.log"]')
        self.assertEqual(decode_file_exclusion_patterns(self.db._get_setting(FILE_EXCLUSION_PATTERNS_KEY)), ["*.log"])

    def test_full_scan_never_indexes_matching_supported_files(self) -> None:
        visible = self.root / "客户制度.txt"
        excluded = self.root / "debug.log"
        visible.write_text("正常资料 信贷", encoding="utf-8")
        excluded.write_text("不应进入索引的日志 信贷", encoding="utf-8")
        self.store.set_patterns([".log"])

        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        self.assertGreaterEqual(stats.excluded, 1)
        self.assertEqual([row.filename for row in self.chunks.search("正常资料")], [visible.name])
        self.assertEqual(self.chunks.search("不应进入索引"), [])
        self.assertTrue(excluded.exists(), "排除索引绝不能删除原文件")

    def test_new_rule_removes_stale_index_on_next_reconciliation(self) -> None:
        target = self.root / "测试_旧方案.txt"
        target.write_text("旧方案仍在磁盘，但不再进入检索", encoding="utf-8")
        DirectoryIndexer(self.db).scan(self.root)
        self.assertEqual(len(self.chunks.search("旧方案仍在磁盘")), 1)

        self.store.set_patterns(["测试_*"])
        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.chunks.search("旧方案仍在磁盘"), [])
        self.assertTrue(target.exists())

    def test_precise_watcher_update_also_removes_now_excluded_file(self) -> None:
        target = self.root / "客户临时.txt"
        target.write_text("临时客户资料", encoding="utf-8")
        DirectoryIndexer(self.db).scan(self.root)
        self.assertEqual(len(self.chunks.search("临时客户资料")), 1)

        self.store.set_patterns(["客户临时.*"])
        stats = DirectoryIndexer(self.db).update_paths([target])

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.excluded, 1)
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.chunks.search("临时客户资料"), [])
        self.assertTrue(target.exists())


class FileExclusionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_settings_persist_normalized_rules_and_track_unsaved_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SearchDatabase(Path(temp_dir) / "docseek.db")
            dialog = PausableIndexSettingsDialog(db)
            try:
                self.assertFalse(dialog._has_unsaved_changes())
                dialog.file_pattern_edit.setPlainText(".LOG\n测试_*\n*.log")
                self.assertTrue(dialog._has_unsaved_changes())
                dialog.accept()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(FileExclusionStore(db).patterns(), ["*.LOG", "测试_*"])
            finally:
                dialog.close()

    def test_invalid_rule_does_not_mutate_settings_or_close_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SearchDatabase(Path(temp_dir) / "docseek.db")
            FileExclusionStore(db).set_patterns(["*.bak"])
            dialog = PausableIndexSettingsDialog(db)
            try:
                dialog.file_pattern_edit.setPlainText("*")
                with patch(
                    "docseek.pausable_settings_dialog.QMessageBox.warning"
                ) as warning:
                    dialog.accept()
                warning.assert_called_once()
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(FileExclusionStore(db).patterns(), ["*.bak"])
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
