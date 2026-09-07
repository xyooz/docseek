from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from docseek.index_issues import IndexIssue
from docseek.index_retry import IndexRetryPlan, build_index_retry_plan, dispatch_index_retry


class IndexRetryPlanTests(unittest.TestCase):
    @staticmethod
    def _issue(path: Path, code: str = "os_error") -> IndexIssue:
        return IndexIssue(str(path), code, "test", 1.0)

    def test_supported_document_gets_precise_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()
            target = root / "broken.docx"

            plan = build_index_retry_plan(
                [self._issue(target)],
                active_roots=[root],
            )

            self.assertEqual(plan.file_paths, (target.resolve(),))
            self.assertEqual(plan.rescan_roots, ())
            self.assertEqual(plan.skipped_paths, ())

    def test_directory_issue_rescans_root_and_drops_redundant_file_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            directory = root / "locked"
            root.mkdir()
            target = root / "broken.pdf"

            plan = build_index_retry_plan(
                [self._issue(target), self._issue(directory, "permission_denied")],
                active_roots=[root],
            )

            self.assertEqual(plan.rescan_roots, (root.resolve(),))
            self.assertEqual(plan.file_paths, ())

    def test_issue_under_paused_or_removed_root_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = Path(temp_dir) / "active"
            paused = Path(temp_dir) / "paused"
            active.mkdir()
            paused.mkdir()
            paused_file = paused / "blocked.docx"

            plan = build_index_retry_plan(
                [self._issue(paused_file)],
                active_roots=[active],
            )

            self.assertTrue(plan.empty)
            self.assertEqual(plan.skipped_paths, (str(paused_file),))

    def test_nested_root_is_preferred_for_directory_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outer = Path(temp_dir) / "docs"
            inner = outer / "team"
            inner.mkdir(parents=True)
            problem = inner / "locked-folder"

            plan = build_index_retry_plan(
                [self._issue(problem)],
                active_roots=[outer, inner],
            )

            self.assertEqual(plan.rescan_roots, (inner.resolve(),))

    def test_dispatch_queues_precise_work_behind_directory_reconciliation(self) -> None:
        root = Path("root")
        target = Path("other") / "retry.docx"
        window = Mock()
        window.pending_watch_paths = set()
        plan = IndexRetryPlan(
            file_paths=(target,),
            rescan_roots=(root,),
            skipped_paths=(),
        )

        dispatch_index_retry(window, plan)

        self.assertEqual(window.pending_watch_paths, {str(target)})
        window._start_index.assert_called_once_with([root])
        window._start_path_update.assert_not_called()

    def test_dispatch_precise_only_uses_existing_path_worker(self) -> None:
        target = Path("retry.docx")
        window = Mock()
        window.pending_watch_paths = set()
        plan = IndexRetryPlan(
            file_paths=(target,),
            rescan_roots=(),
            skipped_paths=(),
        )

        dispatch_index_retry(window, plan)

        window._start_path_update.assert_called_once_with([target])
        window._start_index.assert_not_called()


if __name__ == "__main__":
    unittest.main()
