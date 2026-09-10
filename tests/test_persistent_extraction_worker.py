from __future__ import annotations

import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from docseek.legacy_isolation import LegacyExtractionCancelled, LegacyExtractionTimeout
from docseek.persistent_extraction import (
    PersistentExtractionWorker,
    PersistentWorkerCrashed,
    PersistentWorkerError,
    persistent_worker_command,
)


STUB_WORKER = textwrap.dedent(
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
        request_type = request.get("type")
        if request_type == "shutdown":
            print(json.dumps({"type": "shutdown_ack", "request_id": None}), flush=True)
            raise SystemExit(0)
        request_id = request.get("request_id")
        source = Path(request.get("source", ""))
        if "crash" in source.name:
            os._exit(17)
        if "hang" in source.name:
            while True:
                time.sleep(10)
        if "bad" in source.name:
            print(json.dumps({
                "type": "error",
                "request_id": request_id,
                "error_type": "SyntheticParserError",
                "error": "synthetic parser failure",
            }), flush=True)
            continue
        print(json.dumps({
            "type": "progress",
            "request_id": request_id,
            "location": "stub",
            "current": 1,
        }), flush=True)
        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            pickle.dump((0, "stub", source.name), handle)
        print(json.dumps({
            "type": "result",
            "request_id": request_id,
            "chunk_count": 1,
            "output": str(output),
        }), flush=True)
    '''
)


def _stub_command() -> list[str]:
    return [sys.executable, "-c", STUB_WORKER]


REAL_PROGRESS_WORKER = textwrap.dedent(
    r'''
    from docseek import legacy_worker
    from docseek.chunks import DocumentChunk

    def fake_extract(source, output, *, adapter_name=None, on_progress=None):
        print("parser diagnostic", flush=True)
        if on_progress is not None:
            on_progress("fake-parser", 7)
        return legacy_worker.write_chunk_file(
            output,
            [DocumentChunk(0, "fake", "persistent-result")],
        )

    legacy_worker.extract_to_file = fake_extract
    raise SystemExit(legacy_worker.main(["--persistent"]))
    '''
)


def _real_progress_worker_command() -> list[str]:
    return [sys.executable, "-c", REAL_PROGRESS_WORKER]


UNICODE_PROTOCOL_WORKER = textwrap.dedent(
    r'''
    from docseek import legacy_worker

    def fake_extract(source, output, *, adapter_name=None, on_progress=None):
        if on_progress is not None:
            on_progress("工作表 测试", 7)
        raise ValueError("解析失败：测试")

    legacy_worker.extract_to_file = fake_extract
    raise SystemExit(legacy_worker.main(["--persistent"]))
    '''
)


def _unicode_protocol_worker_command() -> list[str]:
    return [sys.executable, "-c", UNICODE_PROTOCOL_WORKER]


class PersistentExtractionWorkerTests(unittest.TestCase):
    def test_real_worker_round_trip_and_clean_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.txt"
            source.write_text("信贷客户经理\n第二行", encoding="utf-8")
            progress: list[tuple[str, int]] = []

            worker = PersistentExtractionWorker(
                timeout_seconds=10,
                adapter_timeout_seconds=10,
            )
            worker.start()
            process = worker._process
            self.assertIsNotNone(process)
            chunks = list(
                worker.extract(
                    source,
                    adapter_name="direct",
                    on_progress=lambda location, current: progress.append(
                        (location, current)
                    ),
                )
            )
            self.assertEqual(len(chunks), 1)
            self.assertIn("信贷客户经理", chunks[0].content)
            # The direct text adapter has no progress events for this tiny
            # file; the protocol path itself is covered below with a worker
            # that emits progress during extraction.
            self.assertEqual(progress, [])
            self.assertEqual(worker.state, "ready")

            worker.close()
            self.assertFalse(worker.is_alive)
            self.assertIsNotNone(process.poll())
            worker.close()

    def test_real_worker_progress_survives_parser_stdout_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.txt"
            source.write_text("content", encoding="utf-8")
            progress: list[tuple[str, int]] = []
            with PersistentExtractionWorker(
                command_factory=_real_progress_worker_command,
                timeout_seconds=5,
            ) as worker:
                chunks = list(
                    worker.extract(
                        source,
                        adapter_name="fake",
                        on_progress=lambda location, current: progress.append(
                            (location, current)
                        ),
                    )
                )
            self.assertEqual([("fake-parser", 7)], progress)
            self.assertEqual(chunks[0].content, "persistent-result")

    def test_unicode_protocol_fields_round_trip_through_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.xlsx"
            source.write_text("content", encoding="utf-8")
            progress: list[tuple[str, int]] = []
            with PersistentExtractionWorker(
                command_factory=_unicode_protocol_worker_command,
                timeout_seconds=5,
            ) as worker:
                with self.assertRaises(PersistentWorkerError) as raised:
                    list(
                        worker.extract(
                            source,
                            adapter_name="fake",
                            on_progress=lambda location, current: progress.append(
                                (location, current)
                            ),
                        )
                    )

            self.assertEqual([("工作表 测试", 7)], progress)
            self.assertIn("解析失败：测试", str(raised.exception))

    def test_progress_callback_failure_kills_worker_before_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.txt"
            source.write_text("content", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_real_progress_worker_command,
                timeout_seconds=5,
            ) as worker:
                def fail_progress(_location: str, _current: int) -> None:
                    raise RuntimeError("stop from progress callback")

                with self.assertRaises(RuntimeError):
                    list(
                        worker.extract(
                            source,
                            adapter_name="fake",
                            on_progress=fail_progress,
                        )
                    )
                self.assertEqual(worker.state, "dead")
                chunks = list(worker.extract(source, adapter_name="fake"))

            self.assertEqual(chunks[0].content, "persistent-result")

    def test_progress_message_is_forwarded_by_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.txt"
            source.write_text("content", encoding="utf-8")
            progress: list[tuple[str, int]] = []
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            ) as worker:
                chunks = list(
                    worker.extract(
                        source,
                        adapter_name="stub",
                        on_progress=lambda location, current: progress.append(
                            (location, current)
                        ),
                    )
                )
            self.assertEqual([("stub", 1)], progress)
            self.assertEqual(chunks[0].content, "sample.txt")

    def test_one_worker_handles_1000_sequential_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.txt"
            source.write_text("连续请求", encoding="utf-8")
            with PersistentExtractionWorker(
                timeout_seconds=20,
                adapter_timeout_seconds=20,
            ) as worker:
                for _ in range(1000):
                    chunks = list(worker.extract(source, adapter_name="direct"))
                    self.assertEqual(chunks[0].content, "连续请求")
                self.assertTrue(worker.is_alive)

    def test_parser_error_does_not_kill_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "bad.txt"
            valid = root / "valid.txt"
            broken.write_text("bad", encoding="utf-8")
            valid.write_text("valid", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            ) as worker:
                with self.assertRaises(PersistentWorkerError):
                    list(worker.extract(broken, adapter_name="stub"))
                self.assertTrue(worker.is_alive)
                chunks = list(worker.extract(valid, adapter_name="stub"))
            self.assertEqual(chunks[0].content, "valid.txt")

    def test_real_parser_error_does_not_kill_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.docx"
            broken.write_bytes(b"not a docx")
            fixture = Path(__file__).parent / "fixtures" / "official" / "testWORD.docx"
            with PersistentExtractionWorker(
                timeout_seconds=10,
                adapter_timeout_seconds=10,
            ) as worker:
                with self.assertRaises(PersistentWorkerError):
                    list(worker.extract(broken, adapter_name="direct"))
                self.assertTrue(worker.is_alive)
                chunks = list(worker.extract(fixture, adapter_name="direct"))
            self.assertTrue(chunks)

    def test_crash_is_detected_and_restart_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crashed = root / "crash.txt"
            valid = root / "valid.txt"
            crashed.write_text("crash", encoding="utf-8")
            valid.write_text("valid", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            ) as worker:
                with self.assertRaises(PersistentWorkerCrashed):
                    list(worker.extract(crashed, adapter_name="stub"))
                self.assertEqual(worker.state, "dead")
                chunks = list(worker.extract(valid, adapter_name="stub"))
                worker.restart()
                chunks = list(worker.extract(valid, adapter_name="stub"))
                self.assertLessEqual(len(worker._reader_threads), 2)
            self.assertEqual(chunks[0].content, "valid.txt")

    def test_repeated_crash_respawn_converges_to_dead_then_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.txt"
            valid.write_text("valid", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            ) as worker:
                for attempt in range(50):
                    crashed = root / f"crash-{attempt}.txt"
                    crashed.write_text("crash", encoding="utf-8")
                    with self.assertRaises(PersistentWorkerCrashed):
                        list(worker.extract(crashed, adapter_name="stub"))
                    self.assertEqual(worker.state, "dead")
                    chunks = list(worker.extract(valid, adapter_name="stub"))
                    self.assertEqual(chunks[0].content, "valid.txt")
                    self.assertEqual(worker.state, "ready")

    def test_cancel_kills_hanging_worker_quickly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hang.txt"
            source.write_text("hang", encoding="utf-8")
            outcome: list[BaseException] = []
            worker = PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=10,
            )

            def extract() -> None:
                try:
                    list(worker.extract(source, adapter_name="stub"))
                except BaseException as exc:  # assertion below checks the type
                    outcome.append(exc)

            thread = threading.Thread(target=extract, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while worker.active_request_id is None and time.monotonic() < deadline:
                time.sleep(0.005)
            started = time.perf_counter()
            self.assertTrue(worker.cancel())
            thread.join(timeout=5)
            elapsed = time.perf_counter() - started

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], LegacyExtractionCancelled)
            self.assertLess(elapsed, 0.5)
            worker.close()

    def test_repeated_cancel_preserves_cancelled_outcome_before_worker_eof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hanging = root / "hang.txt"
            valid = root / "valid.txt"
            hanging.write_text("hang", encoding="utf-8")
            valid.write_text("valid", encoding="utf-8")
            worker = PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            )
            try:
                for attempt in range(50):
                    outcome: list[BaseException] = []

                    def extract() -> None:
                        try:
                            list(worker.extract(hanging, adapter_name="stub"))
                        except BaseException as exc:  # assertion below checks the type
                            outcome.append(exc)

                    thread = threading.Thread(target=extract, daemon=True)
                    thread.start()
                    deadline = time.monotonic() + 5
                    while (
                        worker.active_request_id is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.005)

                    self.assertIsNotNone(
                        worker.active_request_id,
                        f"cancel attempt {attempt} did not start",
                    )
                    self.assertTrue(worker.cancel())
                    thread.join(timeout=5)

                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(outcome), 1)
                    self.assertIsInstance(outcome[0], LegacyExtractionCancelled)
                    self.assertEqual(worker.state, "dead")

                    chunks = list(worker.extract(valid, adapter_name="stub"))
                    self.assertEqual(chunks[0].content, "valid.txt")
                    self.assertEqual(worker.state, "ready")
            finally:
                worker.close()

    def test_timeout_kills_worker_and_next_request_respawns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hanging = root / "hang.txt"
            valid = root / "valid.txt"
            hanging.write_text("hang", encoding="utf-8")
            valid.write_text("valid", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=5,
            ) as worker:
                with self.assertRaises(LegacyExtractionTimeout):
                    list(
                        worker.extract(
                            hanging,
                            adapter_name="stub",
                            timeout_seconds=0.10,
                        )
                    )
                self.assertEqual(worker.state, "dead")
                chunks = list(worker.extract(valid, adapter_name="stub"))
            self.assertEqual(chunks[0].content, "valid.txt")

    def test_stall_without_progress_kills_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hang.txt"
            source.write_text("hang", encoding="utf-8")
            with PersistentExtractionWorker(
                command_factory=_stub_command,
                timeout_seconds=2,
                adapter_timeout_seconds=2,
                stall_timeout_seconds=0.10,
            ) as worker:
                with self.assertRaises(LegacyExtractionTimeout):
                    list(
                        worker.extract(
                            source,
                            adapter_name="stub",
                            on_progress=lambda location, current: None,
                        )
                    )
                self.assertEqual(worker.state, "dead")

    def test_frozen_worker_command_is_explicit(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            command = persistent_worker_command()
        self.assertEqual(command[:2], [sys.executable, "--docseek-extract-worker"])
        self.assertEqual(command[2], "--persistent")


if __name__ == "__main__":
    unittest.main()
