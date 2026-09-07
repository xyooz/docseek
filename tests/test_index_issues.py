from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.index_issues import IndexIssueStore, issue_label


class IndexIssueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.root.mkdir()
        self.store = IndexIssueStore(Path(self.temp_dir.name) / "docseek.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_record_updates_existing_path_and_summary(self) -> None:
        target = str((self.root / "broken.pdf").resolve())
        self.store.record(target, "BadZipFile", "第一次")
        self.store.record(target, "file_too_large", "第二次")

        issues = self.store.list()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].error_code, "file_too_large")
        self.assertEqual(issues[0].detail, "第二次")
        self.assertEqual(self.store.summary(), {"file_too_large": 1})

    def test_clear_removes_issue(self) -> None:
        target = str((self.root / "retry.txt").resolve())
        self.store.record(target, "os_error", "temporary")
        self.assertEqual(self.store.count(), 1)
        self.store.clear(target)
        self.assertEqual(self.store.count(), 0)

    def test_permission_issue_is_not_pruned_when_path_looks_missing(self) -> None:
        target = str((self.root / "inaccessible").resolve())
        self.store.record(target, "permission_denied", "denied")

        removed = self.store.clear_under_root_if_missing(str(self.root), set())

        self.assertEqual(removed, 0)
        self.assertEqual(self.store.count(), 1)

    def test_compatibility_and_container_errors_use_user_facing_labels(self) -> None:
        self.assertEqual(issue_label("LegacyExtractionError"), "兼容格式解析失败")
        self.assertIn("扩展名不一致", issue_label("PackageNotFoundError"))
        self.assertIn("XML", issue_label("ParseError"))
        self.assertIn("解析组件", issue_label("AdapterUnavailable"))


if __name__ == "__main__":
    unittest.main()
