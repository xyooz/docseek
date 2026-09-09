from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from docseek.indexer import DirectoryIndexer
from docseek.scan_backend import (
    PythonScanBackend,
    RustScanBackend,
    RustScanBackendUnavailable,
    ScanBatch,
    ScanProgress,
    resolve_scan_backend,
)
from docseek.search_db import SearchDatabase


class _BrokenRustModule:
    def JobController(self):
        raise RuntimeError("simulated Rust initialization failure")


class _FakeRustSnapshot:
    directories_seen = 1
    files_seen = 0
    candidates_discovered = 0
    candidates_emitted = 0
    excluded = 0
    errors = 0
    current_path = None


class _FakeRustSession:
    def next_batch(self, _max_items: int = 128):
        return ScanBatch([], True, ScanProgress())

    def snapshot(self):
        return _FakeRustSnapshot()

    def is_finished(self):
        return True


class _FakeRustController:
    def start_scan(self, *_args):
        return _FakeRustSession()

    def cancel(self):
        return False


class _AvailableRustModule:
    def JobController(self):
        return _FakeRustController()


class _PostStartFailureSession:
    def next_batch(self, _max_items: int = 128):
        raise RustScanBackendUnavailable("simulated traversal failure")

    def snapshot(self):
        return ScanProgress()

    def cancel(self):
        return False

    def is_finished(self):
        return False


class _PostStartFailureBackend:
    scan_backend_name = "rust"
    allow_fallback = True

    def start_scan(self, _root: Path, _config):
        return _PostStartFailureSession()


class ScanBackendSelectionTests(unittest.TestCase):
    def test_resolver_supports_auto_python_and_strict_rust(self) -> None:
        auto = resolve_scan_backend("auto")
        python = resolve_scan_backend("python")
        rust = resolve_scan_backend("rust")

        self.assertIsInstance(auto, RustScanBackend)
        self.assertTrue(auto.allow_fallback)
        self.assertEqual(auto.scan_backend_name, "rust")
        self.assertIsInstance(python, PythonScanBackend)
        self.assertEqual(python.scan_backend_name, "python")
        self.assertIsInstance(rust, RustScanBackend)
        self.assertFalse(rust.allow_fallback)
        self.assertEqual(rust.scan_backend_name, "rust")

    def test_empty_or_missing_environment_defaults_to_auto(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            selected = resolve_scan_backend()
        self.assertIsInstance(selected, RustScanBackend)
        self.assertTrue(selected.allow_fallback)

        with patch.dict(os.environ, {"DOCSEEK_SCAN_BACKEND": ""}):
            selected = resolve_scan_backend()
        self.assertIsInstance(selected, RustScanBackend)
        self.assertTrue(selected.allow_fallback)

    def test_python_mode_never_attempts_rust(self) -> None:
        selected = resolve_scan_backend("python")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            (root / "document.txt").write_text("python backend", encoding="utf-8")
            indexer = DirectoryIndexer(
                SearchDatabase(Path(temp_dir) / "index.db"),
                enabled_extensions={".txt"},
                scan_backend=selected,
            )
            with self.assertLogs("docseek.indexer", level="INFO") as logs:
                stats = indexer.scan(root)

        self.assertEqual(stats.indexed, 1)
        self.assertEqual(indexer.scan_backend_name, "python")
        self.assertTrue(
            any("Scan backend selected: python" in line for line in logs.output)
        )

    def test_auto_uses_rust_when_session_initializes(self) -> None:
        selected = RustScanBackend(
            rust_module=_AvailableRustModule(),
            allow_fallback=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            indexer = DirectoryIndexer(
                SearchDatabase(Path(temp_dir) / "index.db"),
                scan_backend=selected,
            )
            with self.assertLogs("docseek.indexer", level="INFO") as logs:
                indexer.scan(root)

        self.assertIs(indexer.scan_backend, selected)
        self.assertEqual(indexer.scan_backend_name, "rust")
        self.assertTrue(
            any("Scan backend selected: rust" in line for line in logs.output)
        )

    def test_auto_falls_back_on_startup_initialization_failure(self) -> None:
        selected = RustScanBackend(
            rust_module=_BrokenRustModule(),
            allow_fallback=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            (root / "document.txt").write_text("Python fallback", encoding="utf-8")
            indexer = DirectoryIndexer(
                SearchDatabase(Path(temp_dir) / "index.db"),
                enabled_extensions={".txt"},
                scan_backend=selected,
            )
            with self.assertLogs("docseek.indexer", level="INFO") as logs:
                stats = indexer.scan(root)

        self.assertEqual(stats.indexed, 1)
        self.assertIsInstance(indexer.scan_backend, PythonScanBackend)
        self.assertEqual(indexer.scan_backend_name, "python")
        self.assertTrue(
            any(
                "Rust scan backend unavailable, falling back to Python" in line
                for line in logs.output
            )
        )
        self.assertTrue(
            any("Scan backend selected: python" in line for line in logs.output)
        )

    def test_strict_rust_reports_startup_failure_without_fallback(self) -> None:
        selected = RustScanBackend(
            rust_module=_BrokenRustModule(),
            allow_fallback=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            indexer = DirectoryIndexer(
                SearchDatabase(Path(temp_dir) / "index.db"),
                scan_backend=selected,
            )
            with self.assertRaises(RustScanBackendUnavailable):
                indexer.scan(root)

        self.assertIs(indexer.scan_backend, selected)
        self.assertEqual(indexer.scan_backend_name, "rust")

    def test_failure_after_session_creation_does_not_fallback(self) -> None:
        selected = _PostStartFailureBackend()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            indexer = DirectoryIndexer(
                SearchDatabase(Path(temp_dir) / "index.db"),
                scan_backend=selected,
            )
            with self.assertRaises(RustScanBackendUnavailable):
                indexer.scan(root)

        self.assertIs(indexer.scan_backend, selected)
        self.assertEqual(indexer.scan_backend_name, "rust")

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto.*python.*rust"):
            resolve_scan_backend("unexpected")


if __name__ == "__main__":
    unittest.main()
