from __future__ import annotations

import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.extraction_revision import current_extraction_revision
from docseek.extraction_state import ExtractionStatus
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.persistent_extraction import PersistentExtractionWorker
from docseek.search_db import SearchDatabase


FAULT_INJECTION_WORKER = textwrap.dedent(
    r'''
    import json
    import os
    import pickle
    import sys
    import time
    from pathlib import Path

    print(json.dumps({"type": "ready", "protocol_version": 1}), flush=True)
    for raw_line in sys.stdin:
        request = json.loads(raw_line)
        if request.get("type") == "shutdown":
            print(json.dumps({"type": "shutdown_ack", "request_id": None}), flush=True)
            raise SystemExit(0)

        request_id = request["request_id"]
        source = Path(request["source"])
        if "bad" in source.name:
            print(json.dumps({
                "type": "error",
                "request_id": request_id,
                "error_type": "SyntheticParserError",
                "error": "synthetic parser failure",
            }), flush=True)
            continue
        if "crash" in source.name:
            os._exit(17)
        if "hang" in source.name:
            while True:
                time.sleep(10)

        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            pickle.dump((0, "stub", f"indexed {source.name}"), handle)
        print(json.dumps({
            "type": "result",
            "request_id": request_id,
            "chunk_count": 1,
            "output": str(output),
        }), flush=True)
    '''
)


def _fault_injection_worker_command() -> list[str]:
    return [sys.executable, "-c", FAULT_INJECTION_WORKER]


VALID_WORKER = textwrap.dedent(
    r'''
    import json
    import pickle
    import sys
    from pathlib import Path

    print(json.dumps({"type": "ready", "protocol_version": 1}), flush=True)
    for raw_line in sys.stdin:
        request = json.loads(raw_line)
        if request.get("type") == "shutdown":
            print(json.dumps({"type": "shutdown_ack", "request_id": None}), flush=True)
            raise SystemExit(0)

        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            pickle.dump((0, "stub", "recovered content"), handle)
        print(json.dumps({
            "type": "result",
            "request_id": request["request_id"],
            "chunk_count": 1,
            "output": str(output),
        }), flush=True)
    '''
)


def _valid_worker_command() -> list[str]:
    return [sys.executable, "-c", VALID_WORKER]


class PersistentExtractionReliabilityTests(unittest.TestCase):
    def _run_update_pair(
        self,
        first_name: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> tuple[object, list[int | None], str, bool, dict[str, str]]:
        """Run two production update_paths requests through one test worker."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / first_name
            second = base / "valid.docx"
            first.write_bytes(b"synthetic parser input")
            second.write_bytes(b"synthetic parser input")
            database = SearchDatabase(base / "index.db")
            worker_instances: list[PersistentExtractionWorker] = []

            class RecordingWorker(PersistentExtractionWorker):
                def __init__(self, *args, **kwargs):
                    kwargs["command_factory"] = _fault_injection_worker_command
                    kwargs["timeout_seconds"] = timeout_seconds
                    kwargs["adapter_timeout_seconds"] = timeout_seconds
                    kwargs["stall_timeout_seconds"] = None
                    super().__init__(*args, **kwargs)
                    self.request_pids: list[int | None] = []
                    worker_instances.append(self)

                def extract(self, *args, **kwargs):
                    process = self._process
                    self.request_pids.append(
                        None if process is None else int(process.pid)
                    )
                    return super().extract(*args, **kwargs)

            with (
                patch("docseek.indexer.PersistentExtractionWorker", RecordingWorker),
                patch(
                    "docseek.legacy_isolation.available_legacy_adapter_names",
                    return_value=("stub",),
                ),
            ):
                stats = DirectoryIndexer(database).update_paths([first, second])

            self.assertEqual(len(worker_instances), 1)
            worker = worker_instances[0]
            statuses: dict[str, str] = {}
            with ChunkStore(database.db_path).connect() as conn:
                rows = conn.execute(
                    "SELECT path, status FROM extraction_state ORDER BY path"
                ).fetchall()
                for row in rows:
                    statuses[Path(str(row["path"])).name] = str(row["status"])

            return (
                stats,
                worker.request_pids,
                worker.state,
                worker.is_alive,
                statuses,
            )

    def test_update_paths_reuses_one_worker_and_closes_at_job_end(self) -> None:
        stats, pids, state, alive, statuses = self._run_update_pair("first.docx")

        self.assertEqual(stats.indexed, 2)
        self.assertEqual(stats.skipped, 0)
        self.assertEqual(len(pids), 2)
        self.assertEqual(len(set(pids)), 1)
        self.assertEqual(state, "closed")
        self.assertFalse(alive)
        self.assertEqual(statuses, {"first.docx": "INDEXED", "valid.docx": "INDEXED"})

    def test_parser_error_keeps_worker_for_next_document(self) -> None:
        stats, pids, state, alive, statuses = self._run_update_pair("bad.docx")

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(len(pids), 2)
        self.assertEqual(len(set(pids)), 1)
        self.assertEqual(state, "closed")
        self.assertFalse(alive)
        self.assertEqual(statuses["bad.docx"], "FAILED")
        self.assertEqual(statuses["valid.docx"], "INDEXED")

    def test_worker_crash_respawns_for_next_document(self) -> None:
        stats, pids, state, alive, statuses = self._run_update_pair("crash.docx")

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(len(pids), 2)
        self.assertNotEqual(pids[0], pids[1])
        self.assertEqual(state, "closed")
        self.assertFalse(alive)
        self.assertEqual(statuses["crash.docx"], "FAILED")
        self.assertEqual(statuses["valid.docx"], "INDEXED")

    def test_worker_timeout_kills_and_respawns_for_next_document(self) -> None:
        stats, pids, state, alive, statuses = self._run_update_pair(
            "hang.docx",
            timeout_seconds=0.5,
        )

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(len(pids), 2)
        self.assertNotEqual(pids[0], pids[1])
        self.assertEqual(state, "closed")
        self.assertFalse(alive)
        self.assertEqual(statuses["hang.docx"], "FAILED")
        self.assertEqual(statuses["valid.docx"], "INDEXED")

    def test_cancelled_document_has_no_partial_chunks_and_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "hang.docx"
            target.write_bytes(b"synthetic parser input")
            database = SearchDatabase(base / "index.db")
            worker_instances: list[PersistentExtractionWorker] = []

            class HangingWorker(PersistentExtractionWorker):
                def __init__(self, *args, **kwargs):
                    kwargs["command_factory"] = _fault_injection_worker_command
                    kwargs["timeout_seconds"] = 30
                    kwargs["adapter_timeout_seconds"] = 30
                    kwargs["stall_timeout_seconds"] = None
                    super().__init__(*args, **kwargs)
                    worker_instances.append(self)

            indexer = DirectoryIndexer(database)
            outcome: list[BaseException] = []

            with (
                patch("docseek.indexer.PersistentExtractionWorker", HangingWorker),
                patch(
                    "docseek.legacy_isolation.available_legacy_adapter_names",
                    return_value=("stub",),
                ),
            ):
                thread = threading.Thread(
                    target=lambda: self._capture_error(
                        outcome, indexer.update_paths, [target]
                    ),
                    daemon=True,
                )
                thread.start()
                deadline = time.monotonic() + 10
                while (
                    not worker_instances
                    or worker_instances[0].active_request_id is None
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)

                self.assertTrue(worker_instances)
                self.assertIsNotNone(worker_instances[0].active_request_id)
                indexer.cancel()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], IndexCancelled)
            worker = worker_instances[0]
            self.assertFalse(worker.is_alive)
            self.assertEqual(worker.state, "closed")

            normalized = str(target.resolve())
            with ChunkStore(database.db_path).connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                    0,
                )
                state = conn.execute(
                    "SELECT status, owner_pid, started_at "
                    "FROM extraction_state WHERE path = ?",
                    (normalized,),
                ).fetchone()
            self.assertIsNotNone(state)
            self.assertEqual(str(state["status"]), ExtractionStatus.PENDING)
            self.assertIsNone(state["owner_pid"])
            self.assertIsNone(state["started_at"])

            class RecoveringWorker(PersistentExtractionWorker):
                def __init__(self, *args, **kwargs):
                    kwargs["command_factory"] = _valid_worker_command
                    super().__init__(*args, **kwargs)

            retry = DirectoryIndexer(database)
            with (
                patch("docseek.indexer.PersistentExtractionWorker", RecoveringWorker),
                patch(
                    "docseek.legacy_isolation.available_legacy_adapter_names",
                    return_value=("stub",),
                ),
            ):
                retry_stats = retry.update_paths([target])
            self.assertEqual(retry_stats.indexed, 1)
            self.assertEqual(retry_stats.skipped, 0)

    @staticmethod
    def _capture_error(outcome, function, *args) -> None:
        try:
            function(*args)
        except BaseException as exc:
            outcome.append(exc)

    def test_abandoned_parser_lease_reaches_interrupted_then_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            target = root / "abandoned.docx"
            target.write_bytes(b"synthetic parser input")
            database = SearchDatabase(base / "index.db")
            store = ChunkStore(database.db_path)

            def force_abandoned(failure_count: int) -> None:
                now = time.time() - 60
                with store.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO extraction_state(
                            path, revision, status, updated_at,
                            source_modified_time, source_size, failure_count,
                            retry_after, owner_pid, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            revision=excluded.revision,
                            status=excluded.status,
                            updated_at=excluded.updated_at,
                            source_modified_time=excluded.source_modified_time,
                            source_size=excluded.source_size,
                            failure_count=excluded.failure_count,
                            retry_after=excluded.retry_after,
                            owner_pid=excluded.owner_pid,
                            started_at=excluded.started_at
                        """,
                        (
                            str(target.resolve()),
                            current_extraction_revision(target.suffix),
                            str(ExtractionStatus.EXTRACTING),
                            now,
                            target.stat().st_mtime,
                            target.stat().st_size,
                            failure_count,
                            999_999_999,
                            now,
                        ),
                    )

            force_abandoned(0)
            first = DirectoryIndexer(database).scan(root)
            with store.connect() as conn:
                first_state = conn.execute(
                    "SELECT status FROM extraction_state WHERE path = ?",
                    (str(target.resolve()),),
                ).fetchone()
            self.assertEqual(first.skipped, 1)
            self.assertEqual(str(first_state["status"]), ExtractionStatus.INTERRUPTED)

            force_abandoned(2)
            second = DirectoryIndexer(database).scan(root)
            with store.connect() as conn:
                second_state = conn.execute(
                    "SELECT status FROM extraction_state WHERE path = ?",
                    (str(target.resolve()),),
                ).fetchone()
            self.assertEqual(second.skipped, 1)
            self.assertEqual(str(second_state["status"]), ExtractionStatus.QUARANTINED)


if __name__ == "__main__":
    unittest.main()
