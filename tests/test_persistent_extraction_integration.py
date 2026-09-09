from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunks import DocumentChunk
from docseek.extraction_broker import ContentExtractionBroker
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.legacy_isolation import (
    LegacyExtractionError,
    iter_legacy_chunks_isolated,
)
from docseek.persistent_extraction import (
    PersistentExtractionWorker,
    PersistentWorkerError,
)
from docseek.search_db import SearchDatabase


HANGING_WORKER = textwrap.dedent(
    r'''
    import json
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
        if "hang" in Path(request["source"]).name:
            while True:
                time.sleep(10)
        output = Path(request["output"])
        with output.open("wb") as handle:
            pickle.dump((0, "stub", "ok"), handle)
        print(json.dumps({
            "type": "result",
            "request_id": request["request_id"],
            "chunk_count": 1,
            "output": str(output),
        }), flush=True)
    '''
)


def _hanging_worker_command() -> list[str]:
    return [sys.executable, "-c", HANGING_WORKER]


class PersistentExtractionIntegrationTests(unittest.TestCase):
    def test_office_and_pdf_files_share_one_worker_pid_and_close_at_scan_end(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "official"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "documents"
            root.mkdir()
            for name in ("testWORD.docx", "testEXCEL.xlsx", "testPPT.pptx", "testPDF.pdf"):
                shutil.copy2(fixture_root / name, root / name)

            worker_instances: list[PersistentExtractionWorker] = []

            class RecordingWorker(PersistentExtractionWorker):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.request_pids: list[int | None] = []
                    worker_instances.append(self)

                def extract(self, *args, **kwargs):
                    process = self._process
                    self.request_pids.append(
                        None if process is None else process.pid
                    )
                    return super().extract(*args, **kwargs)

            with patch("docseek.indexer.PersistentExtractionWorker", RecordingWorker):
                database = SearchDatabase(Path(directory) / "docseek.db")
                stats = DirectoryIndexer(database).scan(root)

            self.assertEqual(stats.indexed, 4)
            self.assertEqual(len(worker_instances), 1)
            worker = worker_instances[0]
            self.assertGreaterEqual(len(worker.request_pids), 4)
            self.assertEqual(len(set(worker.request_pids)), 1)
            self.assertFalse(worker.is_alive)
            self.assertEqual(worker.state, "closed")

    def test_parser_error_does_not_replace_worker_before_next_file(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "official" / "testWORD.docx"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "broken.docx"
            broken.write_bytes(b"not a docx")
            worker = PersistentExtractionWorker(timeout_seconds=20)
            try:
                with self.assertRaises(LegacyExtractionError):
                    list(
                        iter_legacy_chunks_isolated(
                            broken,
                            persistent_worker=worker,
                            timeout_seconds=10,
                        )
                    )
                self.assertTrue(worker.is_alive)
                first_pid = worker._process.pid  # type: ignore[union-attr]

                chunks = list(
                    iter_legacy_chunks_isolated(
                        fixture,
                        persistent_worker=worker,
                        timeout_seconds=20,
                    )
                )
                self.assertTrue(chunks)
                self.assertEqual(worker._process.pid, first_pid)  # type: ignore[union-attr]
            finally:
                worker.close()

    def test_persistent_worker_keeps_adapter_fallback_cascade(self) -> None:
        class FallbackWorker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def extract(self, source, *, adapter_name, **kwargs):
                del source, kwargs
                self.calls.append(adapter_name)
                if adapter_name == "broken":
                    raise PersistentWorkerError("synthetic adapter failure")
                return iter([DocumentChunk(0, "fallback", "persistent success")])

        worker = FallbackWorker()
        source = Path("sample.docx")
        with patch(
            "docseek.legacy_isolation.available_legacy_adapter_names",
            return_value=("broken", "direct"),
        ):
            chunks = list(
                iter_legacy_chunks_isolated(
                    source,
                    persistent_worker=worker,
                )
            )

        self.assertEqual(worker.calls, ["broken", "direct"])
        self.assertEqual(chunks[0].content, "persistent success")

    def test_worker_startup_failure_uses_one_shot_compatibility_fallback(self) -> None:
        class UnavailableWorker:
            def start(self) -> None:
                raise PersistentWorkerError("synthetic worker startup failure")

        expected = [DocumentChunk(0, "legacy", "one-shot fallback")]
        broker = ContentExtractionBroker()
        with patch(
            "docseek.legacy_isolation.iter_legacy_chunks_isolated",
            return_value=iter(expected),
        ) as isolated:
            chunks = list(
                broker.iter_chunks(
                    Path("sample.docx"),
                    cancelled=lambda: False,
                    persistent_worker=UnavailableWorker(),
                )
            )

        isolated.assert_called_once()
        self.assertEqual(chunks, expected)

    def test_cancel_kills_active_worker_and_scan_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "documents"
            root.mkdir()
            (root / "hang.docx").write_bytes(b"parser input")
            worker_instances: list[PersistentExtractionWorker] = []

            class HangingWorker(PersistentExtractionWorker):
                def __init__(self, *args, **kwargs):
                    kwargs["command_factory"] = _hanging_worker_command
                    kwargs["timeout_seconds"] = 30
                    super().__init__(*args, **kwargs)
                    worker_instances.append(self)

            database = SearchDatabase(Path(directory) / "docseek.db")
            outcome: list[BaseException] = []

            with patch("docseek.indexer.PersistentExtractionWorker", HangingWorker):
                indexer = DirectoryIndexer(database)

                def run_scan() -> None:
                    try:
                        indexer.scan(root)
                    except BaseException as exc:
                        outcome.append(exc)

                thread = threading.Thread(target=run_scan, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10
                while (
                    not worker_instances
                    or worker_instances[0].active_request_id is None
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)

                self.assertTrue(worker_instances)
                self.assertIsNotNone(worker_instances[0].active_request_id)
                started = time.perf_counter()
                indexer.cancel()
                thread.join(timeout=5)
                elapsed = time.perf_counter() - started

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], IndexCancelled)
            self.assertLess(elapsed, 1.0)
            self.assertFalse(worker_instances[0].is_alive)
            self.assertEqual(worker_instances[0].state, "closed")


if __name__ == "__main__":
    unittest.main()
