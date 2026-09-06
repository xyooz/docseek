from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.index_tasks import (
    LANE_COMPAT,
    LANE_FAST,
    TASK_EXTRACTING,
    TASK_PENDING,
    TASK_READY_TO_COMMIT,
    IndexTaskStore,
)
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


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


class IndexTaskIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "docs"
        self.root.mkdir()
        self.database = SearchDatabase(self.base / "docseek.db")
        self.tasks = IndexTaskStore(self.database.db_path)
        self.chunks = ChunkStore(self.database.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_process_death_leaves_extracting_task_that_next_process_can_recover(self) -> None:
        target = self.root / "legacy.xls"
        target.write_bytes(b"synthetic legacy workbook")
        indexer = DirectoryIndexer(self.database)

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=SystemExit("synthetic process death"),
        ):
            with self.assertRaises(SystemExit):
                indexer.update_paths([target])

        tasks = self.tasks.list()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].path, str(target.resolve()))
        self.assertEqual(tasks[0].lane, LANE_COMPAT)
        self.assertEqual(tasks[0].state, TASK_EXTRACTING)
        self.assertEqual(tasks[0].attempts, 1)

        recovered = self.tasks.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, TASK_PENDING)

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            return_value=iter([DocumentChunk(0, "内容块 1", "恢复后的信贷资料")]),
        ):
            stats = DirectoryIndexer(self.database).update_paths([target])

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(self.tasks.count(), 0)
        self.assertEqual(
            [row.filename for row in self.chunks.search("恢复后的信贷")],
            [target.name],
        )

    def test_known_parser_failure_is_terminal_not_recovered_as_crash(self) -> None:
        target = self.root / "broken.xls"
        target.write_bytes(b"synthetic broken workbook")

        with mock.patch(
            "docseek.indexer.iter_document_chunks",
            side_effect=RuntimeError("known parser failure"),
        ):
            stats = DirectoryIndexer(self.database).update_paths([target])

        self.assertEqual(stats.skipped, 1)
        self.assertEqual(self.tasks.count(), 0)
        self.assertEqual(self.tasks.recover_interrupted(), [])

    def test_deferred_compatibility_backlog_is_durable_before_fast_work_finishes(self) -> None:
        legacy = self.root / "deferred.xls"
        fast = self.root / "current.txt"
        legacy.write_bytes(b"legacy")
        fast.write_text("fast", encoding="utf-8")
        indexer = DirectoryIndexer(self.database)

        with mock.patch.object(
            indexer,
            "_iter_supported_files",
            return_value=iter([legacy, fast]),
        ), mock.patch.object(
            indexer,
            "_index_existing_file",
            side_effect=SystemExit("crash while processing fast lane"),
        ):
            with self.assertRaises(SystemExit):
                indexer.scan(self.root)

        queued = self.tasks.list()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].path, str(legacy.resolve()))
        self.assertEqual(queued[0].lane, LANE_COMPAT)
        self.assertEqual(queued[0].state, TASK_PENDING)


if __name__ == "__main__":
    unittest.main()
