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


CORRUPT_SPOOL_WORKER = textwrap.dedent(
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
        if request.get("adapter_name") == "broken":
            with output.open("wb") as handle:
                pickle.dump((0, "partial", "must not leak"), handle)
                handle.write(b"\x80")
            chunk_count = 2
        else:
            with output.open("wb") as handle:
                pickle.dump((0, "fallback", "fallback content"), handle)
            chunk_count = 1

        print(json.dumps({
            "type": "result",
            "request_id": request["request_id"],
            "chunk_count": chunk_count,
            "output": str(output),
        }), flush=True)
    '''
)


def _corrupt_spool_worker_command() -> list[str]:
    return [sys.executable, "-c", CORRUPT_SPOOL_WORKER]


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
                    self.request_records: list[dict[str, object]] = []
                    worker_instances.append(self)

                def extract(self, *args, **kwargs):
                    source = Path(args[0]) if args else Path("<unknown>")
                    adapter_name = str(kwargs.get("adapter_name", "<default>"))
                    process = self._process
                    pid = None if process is None else process.pid
                    self.request_pids.append(pid)
                    record: dict[str, object] = {
                        "source": source,
                        "adapter": adapter_name,
                        "pid": pid,
                    }
                    self.request_records.append(record)
                    try:
                        chunks = super().extract(*args, **kwargs)
                    except BaseException as exc:
                        record["outcome"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise

                    def replay():
                        try:
                            yield from chunks
                        except BaseException as exc:
                            record["outcome"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                            raise
                        else:
                            record["outcome"] = "success"

                    return replay()

            def diagnostic_message() -> str:
                expected_paths = {
                    name: str((root / name).resolve())
                    for name in (
                        "testWORD.docx",
                        "testEXCEL.xlsx",
                        "testPPT.pptx",
                        "testPDF.pdf",
                    )
                }
                with database.connect() as conn:
                    file_rows = {
                        str(row["path"]): row
                        for row in conn.execute(
                            """
                            SELECT path, filename, extension, last_error
                            FROM files
                            """
                        ).fetchall()
                    }
                    state_rows = {
                        str(row["path"]): row
                        for row in conn.execute(
                            """
                            SELECT path, status, failure_count, owner_pid
                            FROM extraction_state
                            """
                        ).fetchall()
                    }
                    issue_rows = {
                        str(row["path"]): row
                        for row in conn.execute(
                            """
                            SELECT path, error_code, detail
                            FROM index_issues
                            """
                        ).fetchall()
                    }

                def find_row(rows, path_text):
                    row = rows.get(path_text)
                    if row is not None:
                        return row
                    name = Path(path_text).name.casefold()
                    matches = [
                        candidate
                        for candidate_path, candidate in rows.items()
                        if Path(candidate_path).name.casefold() == name
                    ]
                    return matches[0] if len(matches) == 1 else None

                records_by_name: dict[str, list[str]] = {}
                request_records = () if worker is None else worker.request_records
                for record in request_records:
                    source = Path(str(record["source"]))
                    records_by_name.setdefault(source.name, []).append(
                        "{}@pid={} [{}]".format(
                            record["adapter"],
                            record["pid"],
                            record.get("outcome", "in progress"),
                        )
                    )

                lines = [
                    "persistent integration diagnostics:",
                    f"stats={stats!r}",
                    "worker_state={}".format(
                        "<not created>" if worker is None else repr(worker.state)
                    ),
                    "worker_pids={}".format(
                        "<not created>"
                        if worker is None
                        else repr(worker.request_pids)
                    ),
                ]
                for name, path_text in expected_paths.items():
                    file_row = find_row(file_rows, path_text)
                    state_row = find_row(state_rows, path_text)
                    issue_row = find_row(issue_rows, path_text)
                    status = (
                        str(state_row["status"])
                        if state_row is not None
                        else "<missing>"
                    )
                    lifecycle = (
                        "indexed"
                        if status in {"INDEXED", "NO_TEXT", "OCR_REQUIRED"}
                        else "failed/skipped"
                        if state_row is not None or issue_row is not None
                        else "missing"
                    )
                    issue = (
                        "<none>"
                        if issue_row is None
                        else "{}: {}".format(
                            issue_row["error_code"],
                            str(issue_row["detail"]).replace("\n", " ")[:600],
                        )
                    )
                    last_error = (
                        "<none>"
                        if file_row is None
                        else str(file_row["last_error"] or "<none>")
                    )
                    lines.append(
                        "{} ext={} lifecycle={} file_row={} state={} "
                        "failure_count={} owner_pid={} last_error={} issue={} "
                        "attempts={}".format(
                            name,
                            (root / name).suffix,
                            lifecycle,
                            file_row is not None,
                            status,
                            "<missing>"
                            if state_row is None
                            else state_row["failure_count"],
                            "<missing>"
                            if state_row is None
                            else state_row["owner_pid"],
                            last_error,
                            issue,
                            "; ".join(records_by_name.get(name, ())) or "<none>",
                        )
                    )
                return "\n".join(lines)

            with patch("docseek.indexer.PersistentExtractionWorker", RecordingWorker):
                database = SearchDatabase(Path(directory) / "docseek.db")
                stats = DirectoryIndexer(database).scan(root)

            worker = worker_instances[0] if worker_instances else None
            self.assertEqual(
                stats.indexed,
                4,
                msg=diagnostic_message() if stats.indexed != 4 else None,
            )
            self.assertEqual(len(worker_instances), 1)
            assert worker is not None
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

    def test_corrupt_persistent_spool_falls_back_before_leaking_partial_chunks(self) -> None:
        worker = PersistentExtractionWorker(
            command_factory=_corrupt_spool_worker_command,
            timeout_seconds=5,
        )
        try:
            with patch(
                "docseek.legacy_isolation.available_legacy_adapter_names",
                return_value=("broken", "direct"),
            ):
                chunks = list(
                    iter_legacy_chunks_isolated(
                        Path("sample.docx"),
                        persistent_worker=worker,
                    )
                )

            self.assertEqual(
                chunks,
                [DocumentChunk(0, "fallback", "fallback content")],
            )
            self.assertTrue(worker.is_alive)
        finally:
            worker.close()

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

    def test_startup_failure_disables_persistent_worker_for_remainder_of_job(self) -> None:
        startup_attempts = 0

        def unavailable_command() -> list[str]:
            nonlocal startup_attempts
            startup_attempts += 1
            return [sys.executable, "-c", "import sys; sys.exit(23)"]

        worker = PersistentExtractionWorker(
            command_factory=unavailable_command,
            timeout_seconds=2,
        )
        expected = [DocumentChunk(0, "legacy", "one-shot fallback")]
        broker = ContentExtractionBroker()
        try:
            with patch(
                "docseek.legacy_isolation.iter_legacy_chunks_isolated",
                side_effect=lambda *args, **kwargs: iter(expected),
            ) as isolated:
                first = list(
                    broker.iter_chunks(
                        Path("first.docx"),
                        cancelled=lambda: False,
                        persistent_worker=worker,
                    )
                )
                second = list(
                    broker.iter_chunks(
                        Path("second.docx"),
                        cancelled=lambda: False,
                        persistent_worker=worker,
                    )
                )

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertEqual(startup_attempts, 1)
            self.assertEqual(isolated.call_count, 2)
            self.assertTrue(worker.unavailable_for_job)
        finally:
            worker.close()

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
