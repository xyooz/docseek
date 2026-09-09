from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import docseek_rust  # type: ignore[import-not-found]
except ImportError:
    docseek_rust = None

from docseek.scan_backend import (  # noqa: E402
    DEFAULT_IGNORED_DIR_NAMES,
    MAX_SCAN_BATCH_SIZE,
    MAX_SCAN_WORK_ITEMS,
    PythonScanBackend,
    RustScanBackend,
    ScanConfig,
    ScanCancelled,
    ScanIssue,
    iter_scan_candidates,
)
from docseek.index_priority import prioritize_index_candidates  # noqa: E402


def _collect_batches(session):
    batches = []
    while True:
        batch = session.next_batch(MAX_SCAN_BATCH_SIZE)
        batches.append(batch)
        if batch.finished:
            return batches


def _candidate_paths(batches, root: Path) -> list[str]:
    resolved_root = root.resolve()
    return [
        path.relative_to(resolved_root).as_posix()
        for batch in batches
        for path in batch.candidates
    ]


def _write(path: Path, contents: str = "content") -> None:
    path.write_text(contents, encoding="utf-8")


class PythonScanBackendTests(unittest.TestCase):
    def test_scan_config_normalizes_shared_options(self) -> None:
        config = ScanConfig(
            enabled_extensions=["TXT", "docx"],
            ignored_dir_names=["Cache", "项目"],
            excluded_paths=[Path("relative/excluded")],
            excluded_file_patterns=[" .LOG "],
            max_file_size=None,
        )

        self.assertEqual(config.enabled_extensions, frozenset({".txt", ".docx"}))
        self.assertEqual(config.ignored_dir_names, frozenset({"cache", "项目"}))
        self.assertEqual(config.excluded_file_patterns, ("*.LOG",))
        self.assertEqual(config.max_file_size, 200 * 1024 * 1024)

    def test_python_backend_streams_bounded_priority_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            for index in range(300):
                _write(root / f"file-{index:03d}.txt")

            config = ScanConfig(enabled_extensions=[".txt"])
            session = PythonScanBackend().start_scan(root, config)
            batches = _collect_batches(session)

            self.assertEqual(
                [len(batch.candidates) for batch in batches],
                [128, 128, 44, 0],
            )
            self.assertTrue(
                all(len(batch.candidates) <= MAX_SCAN_BATCH_SIZE for batch in batches)
            )
            self.assertEqual(batches[-1].finished, True)
            self.assertEqual(batches[-1].progress.candidates_emitted, 300)

    def test_sparse_scan_returns_progress_pulses_before_candidates(self) -> None:
        backends = [PythonScanBackend()]
        if docseek_rust is not None:
            backends.append(RustScanBackend())

        with tempfile.TemporaryDirectory() as temp_dir:
            for backend in backends:
                root = Path(temp_dir) / backend.__class__.__name__
                root.mkdir()
                for index in range(MAX_SCAN_WORK_ITEMS + 1):
                    (root / f"noncandidate-{index:04d}.bin").touch()

                progress_events = []
                paths = list(
                    iter_scan_candidates(
                        backend.start_scan(root, ScanConfig(enabled_extensions=[".txt"])),
                        on_progress=progress_events.append,
                    )
                )

                self.assertEqual(paths, [])
                self.assertGreaterEqual(len(progress_events), 3)
                self.assertTrue(
                    any(
                        MAX_SCAN_WORK_ITEMS - 1 <= progress.files_seen < MAX_SCAN_WORK_ITEMS + 1
                        for progress in progress_events
                    )
                )

    def test_scan_issues_are_drained_with_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            backend = PythonScanBackend()
            session = backend.start_scan(root)
            issues: list[ScanIssue] = []

            with patch(
                "docseek.scan_backend.os.scandir",
                side_effect=PermissionError("denied by test"),
            ):
                paths = list(
                    iter_scan_candidates(
                        session,
                        on_issues=issues.extend,
                    )
                )

            self.assertEqual(paths, [])
            self.assertEqual(
                [(issue.path, issue.error_code, issue.detail) for issue in issues],
                [(root.resolve(), "permission_denied", "denied by test")],
            )

    def test_discovery_callback_replay_matches_existing_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            _write(root / "制度.txt")

            events: list[tuple[str, int]] = []
            session = PythonScanBackend().start_scan(root)
            paths = list(
                iter_scan_candidates(
                    session,
                    on_discovery=lambda path, count: events.append((path.name, count)),
                )
            )

            self.assertEqual([path.name for path in paths], ["制度.txt"])
            self.assertEqual(events[0][1], 0)
            self.assertEqual(events[-1][1], 1)

    def test_python_and_rust_candidates_match_with_shared_configuration(self) -> None:
        if docseek_rust is None:
            self.skipTest("docseek_rust wheel is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            nested = root / "项目资料"
            excluded = root / "excluded"
            ignored = root / "skipme"
            (nested).mkdir(parents=True)
            excluded.mkdir()
            ignored.mkdir()
            (root / ".git").mkdir()

            _write(root / "modern.docx", "docx")
            _write(root / "notes.txt", "notes")
            _write(root / "legacy.wps", "wps")
            _write(root / "mail.eml", "mail")
            _write(root / "book.epub", "epub")
            _write(root / "reporta.txt", "letter")
            _write(nested / "说明.txt", "unicode")
            _write(root / "report1.txt", "digit")
            _write(root / "trace.log", "log")
            _write(root / "~$locked.docx", "office lock")
            _write(root / "skip.tmp", "temporary")
            _write(root / "oversize.txt", "x" * 128)
            _write(excluded / "secret.txt", "excluded")
            _write(ignored / "hidden.txt", "ignored")
            _write(root / ".git" / "metadata.txt", "ignored")

            config = ScanConfig(
                enabled_extensions=[".txt", ".docx", ".wps", ".eml", ".epub"],
                ignored_dir_names=DEFAULT_IGNORED_DIR_NAMES | {"skipme"},
                excluded_paths=[excluded],
                excluded_file_patterns=["secret.*", "report[0-9].txt", ".log"],
                max_file_size=32,
            )
            python_batches = _collect_batches(
                PythonScanBackend().start_scan(root, config)
            )
            rust_batches = _collect_batches(RustScanBackend().start_scan(root, config))
            self.assertTrue(
                all(
                    not str(path).startswith("\\\\?\\")
                    for batch in rust_batches
                    for path in batch.candidates
                )
            )
            python_paths = _candidate_paths(python_batches, root)
            rust_paths = _candidate_paths(rust_batches, root)

            # Directory enumeration order is intentionally not a cross-runtime
            # contract: os.scandir() and Rust read_dir() may return different
            # same-lane orders on Windows. Candidate membership and priority
            # lane ordering must still agree.
            self.assertEqual(
                set(python_paths),
                set(rust_paths),
            )
            self.assertEqual(
                set(python_paths),
                {
                    "modern.docx",
                    "notes.txt",
                    "reporta.txt",
                    "oversize.txt",
                    "项目资料/说明.txt",
                    "legacy.wps",
                    "book.epub",
                    "mail.eml",
                },
            )
            self.assertEqual(
                python_paths,
                [
                    path.as_posix()
                    for path in prioritize_index_candidates(map(Path, python_paths))
                ],
            )
            self.assertEqual(
                rust_paths,
                [
                    path.as_posix()
                    for path in prioritize_index_candidates(map(Path, rust_paths))
                ],
            )
            self.assertEqual(
                [len(batch.candidates) for batch in python_batches],
                [len(batch.candidates) for batch in rust_batches],
            )
            self.assertTrue(
                all(
                    len(batch.candidates) <= MAX_SCAN_BATCH_SIZE
                    for batch in rust_batches
                )
            )

            python_progress = python_batches[-1].progress
            rust_progress = rust_batches[-1].progress
            self.assertEqual(
                (python_progress.files_seen, python_progress.candidates_discovered),
                (rust_progress.files_seen, rust_progress.candidates_discovered),
            )
            self.assertEqual(python_progress.excluded, rust_progress.excluded)
            self.assertEqual(
                (python_progress.candidates_emitted, rust_progress.candidates_emitted),
                (8, 8),
            )

    def test_rust_cancel_translates_to_scan_cancelled(self) -> None:
        if docseek_rust is None:
            self.skipTest("docseek_rust wheel is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            _write(root / "document.txt")

            session = RustScanBackend().start_scan(root)
            self.assertTrue(session.cancel())
            with self.assertRaises(ScanCancelled):
                session.next_batch()

    def test_rust_cancel_from_another_thread_during_next_batch(self) -> None:
        if docseek_rust is None:
            self.skipTest("docseek_rust wheel is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            # Keep the scanner busy across multiple bounded progress batches.
            # These files are deliberately unsupported, so the worker must
            # keep pulling batches instead of returning after the first pulse.
            for index in range(50_000):
                (root / f"noncandidate-{index:05d}.bin").touch()

            session = RustScanBackend().start_scan(root)
            started = threading.Event()
            outcome: dict[str, object] = {}

            def scan() -> None:
                started.set()
                try:
                    while True:
                        batch = session.next_batch(1)
                        if batch.finished:
                            outcome["batch"] = batch
                            return
                except BaseException as exc:  # pass the worker result to the test
                    outcome["error"] = exc

            worker = threading.Thread(target=scan)
            worker.start()
            self.assertTrue(started.wait(timeout=2))

            # Wait until discovery has entered the directory before asking the
            # separate controller object to cancel the in-flight PyO3 call.
            deadline = time.monotonic() + 10
            while worker.is_alive() and time.monotonic() < deadline:
                snapshot = session._controller.last_snapshot()
                if snapshot is not None and snapshot.files_seen > 0:
                    break
                time.sleep(0.001)

            self.assertTrue(worker.is_alive(), "scan finished before concurrent cancel")
            self.assertTrue(session.cancel())
            worker.join(timeout=10)

            self.assertFalse(worker.is_alive(), "cancelled scan worker did not finish")
            self.assertIsInstance(outcome.get("error"), ScanCancelled)


if __name__ == "__main__":
    unittest.main()
