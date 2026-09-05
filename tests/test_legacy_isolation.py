from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docseek.chunks import DocumentChunk
from docseek.extraction_broker import ContentExtractionBroker
from docseek.legacy_isolation import (
    LegacyExtractionCancelled,
    LegacyExtractionTimeout,
    iter_legacy_chunks_isolated,
    legacy_worker_command,
    wait_for_worker,
)
from docseek.legacy_worker import iter_chunk_file, write_chunk_file


class LegacyIsolationTests(unittest.TestCase):
    def test_chunk_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "chunks.bin"
            expected = [
                DocumentChunk(0, "工作表 Sheet1 · 行 1-2", "alpha\nbeta"),
                DocumentChunk(1, "工作表 Sheet2 · 行 1", "中文内容"),
            ]
            self.assertEqual(write_chunk_file(output, expected), 2)
            self.assertEqual(list(iter_chunk_file(output)), expected)
            self.assertFalse(output.with_name(output.name + ".part").exists())

    def test_source_worker_round_trip_uses_real_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "sample.txt"
            source.write_text("信贷客户经理\n第二行", encoding="utf-8")
            chunks = list(iter_legacy_chunks_isolated(source, timeout_seconds=20))
            self.assertTrue(chunks)
            self.assertIn("信贷客户经理", "\n".join(chunk.content for chunk in chunks))

    def test_timeout_terminates_hanging_worker(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with self.assertRaises(LegacyExtractionTimeout):
            wait_for_worker(process, timeout_seconds=0.10)
        self.assertIsNotNone(process.poll())

    def test_cancel_terminates_worker(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with self.assertRaises(LegacyExtractionCancelled):
            wait_for_worker(
                process,
                timeout_seconds=10,
                cancelled=lambda: True,
            )
        self.assertIsNotNone(process.poll())

    def test_source_and_frozen_worker_commands_are_explicit(self) -> None:
        source = Path("legacy.xls")
        output = Path("chunks.bin")
        command = legacy_worker_command(source, output)
        self.assertEqual(command[:3], [sys.executable, "-m", "docseek.legacy_worker"])

        with mock.patch.object(sys, "frozen", True, create=True):
            frozen = legacy_worker_command(source, output)
        self.assertEqual(frozen[0], sys.executable)
        self.assertEqual(frozen[1], "--docseek-extract-worker")

    def test_broker_routes_legacy_format_to_isolated_lane(self) -> None:
        expected = [DocumentChunk(0, "内容块 1", "legacy body")]
        broker = ContentExtractionBroker()
        with mock.patch(
            "docseek.legacy_isolation.iter_legacy_chunks_isolated",
            return_value=iter(expected),
        ) as isolated:
            actual = list(broker.iter_chunks(Path("sample.xls")))
        self.assertEqual(actual, expected)
        isolated.assert_called_once()

    def test_worker_marker_bypasses_isolation(self) -> None:
        broker = ContentExtractionBroker()
        previous = os.environ.get("DOCSEEK_LEGACY_WORKER")
        os.environ["DOCSEEK_LEGACY_WORKER"] = "1"
        try:
            self.assertFalse(broker._should_isolate(Path("sample.xls")))
        finally:
            if previous is None:
                os.environ.pop("DOCSEEK_LEGACY_WORKER", None)
            else:
                os.environ["DOCSEEK_LEGACY_WORKER"] = previous


if __name__ == "__main__":
    unittest.main()
