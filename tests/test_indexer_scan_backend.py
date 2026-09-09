from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import docseek_rust  # type: ignore[import-not-found]
except ImportError:
    docseek_rust = None

from docseek.chunk_store import ChunkStore
from docseek.index_issues import IndexIssueStore
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.scan_backend import (
    PythonScanBackend,
    RustScanBackend,
    RustScanBackendUnavailable,
    ScanBatch,
    ScanCancelled,
    ScanIssue,
    ScanProgress,
    resolve_scan_backend,
)
from docseek.search_db import SearchDatabase


class _UnavailableBackend:
    allow_fallback = True

    def start_scan(self, _root: Path, _config):
        raise RustScanBackendUnavailable("test backend unavailable")


class _BlockingSession:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_calls = 0

    def next_batch(self, _max_items: int = 128) -> ScanBatch:
        self.started.set()
        self.cancelled.wait(timeout=5)
        if not self.cancelled.is_set():
            raise AssertionError("test scan was not cancelled")
        raise ScanCancelled("test scan cancelled")

    def cancel(self) -> bool:
        self.cancel_calls += 1
        self.cancelled.set()
        return True

    def snapshot(self) -> ScanProgress:
        return ScanProgress()

    def is_finished(self) -> bool:
        return self.cancelled.is_set()


class _BlockingBackend:
    def __init__(self) -> None:
        self.session = _BlockingSession()

    def start_scan(self, _root: Path, _config):
        return self.session


class _IssueSession:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._returned = False

    def next_batch(self, _max_items: int = 128) -> ScanBatch:
        if self._returned:
            return ScanBatch([], True, ScanProgress(current_path=self._root))
        self._returned = True
        return ScanBatch(
            [],
            True,
            ScanProgress(current_path=self._root, errors=1),
            [ScanIssue(self._root / "denied", "permission_denied", "denied by test")],
        )

    def cancel(self) -> bool:
        return False

    def snapshot(self) -> ScanProgress:
        return ScanProgress(current_path=self._root)

    def is_finished(self) -> bool:
        return self._returned


class _IssueBackend:
    def __init__(self) -> None:
        self.session: _IssueSession | None = None

    def start_scan(self, root: Path, _config):
        self.session = _IssueSession(root)
        return self.session


@unittest.skipUnless(docseek_rust is not None, "docseek_rust wheel is not installed")
class RustDirectoryIndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "docs"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _search_snapshot(database: SearchDatabase) -> list[tuple[str, ...]]:
        rows = ChunkStore(database.db_path).search("共同内容")
        return sorted(
            (
                row.path,
                row.filename,
                row.extension,
                row.location,
                row.snippet,
            )
            for row in rows
        )

    def _populate(self) -> None:
        nested = self.root / "项目资料"
        nested.mkdir()
        excluded = self.root / "excluded"
        excluded.mkdir()
        (self.root / "modern.txt").write_text("共同内容 现代文件", encoding="utf-8")
        (self.root / "notes.md").write_text("共同内容 Markdown 文件", encoding="utf-8")
        (nested / "说明.txt").write_text("共同内容 Unicode 文件", encoding="utf-8")
        (self.root / "skip.txt").write_text("不应进入索引", encoding="utf-8")
        (excluded / "secret.txt").write_text("不应进入索引", encoding="utf-8")

    def test_python_and_rust_full_index_results_match(self) -> None:
        self._populate()
        config = {
            "enabled_extensions": {".txt", ".md"},
            "excluded_paths": [str(self.root / "excluded")],
            "excluded_file_patterns": ["skip.*"],
        }

        python_db = SearchDatabase(self.base / "python.db")
        python_stats = DirectoryIndexer(
            python_db,
            scan_backend=PythonScanBackend(),
            **config,
        ).scan(self.root)

        rust_db = SearchDatabase(self.base / "rust.db")
        rust_stats = DirectoryIndexer(
            rust_db,
            scan_backend=RustScanBackend(),
            **config,
        ).scan(self.root)

        self.assertEqual(python_stats.scanned, rust_stats.scanned)
        self.assertEqual(python_stats.indexed, rust_stats.indexed)
        self.assertEqual(python_stats.excluded, rust_stats.excluded)
        self.assertEqual(self._search_snapshot(python_db), self._search_snapshot(rust_db))

    def test_rust_backend_keeps_oversized_file_in_indexer_lifecycle(self) -> None:
        target = self.root / "large.txt"
        target.write_text("超过限制", encoding="utf-8")
        database = SearchDatabase(self.base / "index.db")

        stats = DirectoryIndexer(
            database,
            max_file_size=4,
            enabled_extensions={".txt"},
            scan_backend=RustScanBackend(),
        ).scan(self.root)

        self.assertEqual(stats.indexed, 0)
        self.assertEqual(stats.skipped, 1)
        issues = IndexIssueStore(database.db_path).list()
        self.assertEqual([(issue.path, issue.error_code) for issue in issues], [
            (str(target.resolve()), "file_too_large")
        ])
        self.assertEqual(database.count_files(), 0)


class DirectoryIndexerScanControlTests(unittest.TestCase):
    def test_cancel_forwards_to_active_scan_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            root.mkdir()
            backend = _BlockingBackend()
            indexer = DirectoryIndexer(
                SearchDatabase(base / "index.db"),
                scan_backend=backend,
            )
            outcome: list[BaseException] = []

            def run_scan() -> None:
                try:
                    indexer.scan(root)
                except BaseException as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=run_scan)
            worker.start()
            self.assertTrue(backend.session.started.wait(timeout=2))
            indexer.cancel()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(backend.session.cancel_calls, 1)
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], IndexCancelled)

    def test_unavailable_backend_falls_back_to_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            root.mkdir()
            (root / "fallback.txt").write_text("Python fallback", encoding="utf-8")
            indexer = DirectoryIndexer(
                SearchDatabase(base / "index.db"),
                enabled_extensions={".txt"},
                scan_backend=_UnavailableBackend(),
            )

            stats = indexer.scan(root)

            self.assertEqual(stats.indexed, 1)
            self.assertIsInstance(indexer.scan_backend, PythonScanBackend)

    def test_discovery_issue_is_recorded_and_counted_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            root.mkdir()
            database = SearchDatabase(base / "index.db")
            indexer = DirectoryIndexer(
                database,
                scan_backend=_IssueBackend(),
            )

            stats = indexer.scan(root)

            self.assertEqual(stats.skipped, 1)
            issues = IndexIssueStore(database.db_path).list()
            self.assertEqual(
                [(issue.path, issue.error_code, issue.detail) for issue in issues],
                [(str((root / "denied").resolve()), "permission_denied", "denied by test")],
            )

    def test_backend_resolver_supports_explicit_modes(self) -> None:
        self.assertIsInstance(resolve_scan_backend("auto"), RustScanBackend)
        self.assertIsInstance(resolve_scan_backend("python"), PythonScanBackend)
        self.assertIsInstance(resolve_scan_backend("rust"), RustScanBackend)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"DOCSEEK_SCAN_BACKEND": "rust"}):
                self.assertIsInstance(
                    DirectoryIndexer(
                        SearchDatabase(Path(temp_dir) / "index.db")
                    ).scan_backend,
                    RustScanBackend,
                )
        with self.assertRaises(ValueError):
            resolve_scan_backend("unexpected")


if __name__ == "__main__":
    unittest.main()
