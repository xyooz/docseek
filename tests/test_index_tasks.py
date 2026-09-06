from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.index_tasks import (
    LANE_COMPAT,
    LANE_FAST,
    TASK_EXTRACTING,
    TASK_PENDING,
    TASK_READY_TO_COMMIT,
    IndexTaskStore,
)


class IndexTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.store = IndexTaskStore(self.base / "docseek.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_enqueue_round_trip_and_source_change_resets_attempts(self) -> None:
        path = self.base / "docs" / "policy.docx"
        self.store.enqueue(
            path,
            lane=LANE_FAST,
            revision=2,
            modified_time=10.0,
            size=123,
        )
        self.store.mark_extracting(path)

        task = self.store.list()[0]
        self.assertEqual(task.state, TASK_EXTRACTING)
        self.assertEqual(task.attempts, 1)

        self.store.enqueue(
            path,
            lane=LANE_FAST,
            revision=2,
            modified_time=11.0,
            size=124,
        )
        task = self.store.list()[0]
        self.assertEqual(task.state, TASK_PENDING)
        self.assertEqual(task.attempts, 0)
        self.assertEqual(task.modified_time, 11.0)
        self.assertEqual(task.size, 124)

    def test_same_source_requeue_preserves_attempt_count(self) -> None:
        path = self.base / "legacy.xls"
        self.store.enqueue(
            path,
            lane=LANE_COMPAT,
            revision=1,
            modified_time=10.0,
            size=100,
        )
        self.store.mark_extracting(path)
        self.store.enqueue(
            path,
            lane=LANE_COMPAT,
            revision=1,
            modified_time=10.0,
            size=100,
        )

        task = self.store.list()[0]
        self.assertEqual(task.state, TASK_PENDING)
        self.assertEqual(task.attempts, 1)

    def test_recovery_resets_abandoned_inflight_and_prefers_fast_lane(self) -> None:
        fast = self.base / "a.docx"
        compat = self.base / "b.xls"
        ready = self.base / "c.wps"
        for path, lane in (
            (compat, LANE_COMPAT),
            (fast, LANE_FAST),
            (ready, LANE_COMPAT),
        ):
            self.store.enqueue(
                path,
                lane=lane,
                revision=1,
                modified_time=1.0,
                size=10,
            )
        self.store.mark_extracting(fast)
        self.store.mark_extracting(compat)
        self.store.mark_ready_to_commit(ready)

        before = {task.path: task.state for task in self.store.list()}
        self.assertEqual(before[str(fast)], TASK_EXTRACTING)
        self.assertEqual(before[str(ready)], TASK_READY_TO_COMMIT)

        recovered = self.store.recover_interrupted()
        self.assertEqual([task.path for task in recovered][0], str(fast))
        self.assertTrue(all(task.state == TASK_PENDING for task in recovered))
        self.assertEqual(len(recovered), 3)

    def test_complete_is_idempotent(self) -> None:
        path = self.base / "a.pdf"
        self.store.enqueue(
            path,
            lane=LANE_FAST,
            revision=1,
            modified_time=1.0,
            size=10,
        )
        self.store.complete(path)
        self.store.complete(path)
        self.assertEqual(self.store.count(), 0)

    def test_remove_under_root_keeps_other_tasks(self) -> None:
        root_a = self.base / "a"
        root_b = self.base / "b"
        path_a = root_a / "one.docx"
        path_b = root_b / "two.docx"
        for path in (path_a, path_b):
            self.store.enqueue(
                path,
                lane=LANE_FAST,
                revision=1,
                modified_time=1.0,
                size=10,
            )

        removed = self.store.remove_under_root(root_a)
        self.assertEqual(removed, 1)
        self.assertEqual([task.path for task in self.store.list()], [str(path_b)])


if __name__ == "__main__":
    unittest.main()
