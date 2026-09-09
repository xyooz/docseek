from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import docseek_rust  # type: ignore[import-not-found]
except ImportError:
    docseek_rust = None

from docseek.chunk_codec import decode_chunk_content
from docseek.chunk_store import ChunkStore
from docseek.extraction_revision import current_extraction_revision
from docseek.extraction_state import ExtractionStatus
from docseek.index_issues import IndexIssueStore
from docseek.indexer import DirectoryIndexer, IndexCancelled, IndexStats
from docseek.search_db import SearchDatabase
from docseek.scan_backend import PythonScanBackend, RustScanBackend


BackendFactory = Callable[[], object]


@dataclass(frozen=True)
class _IndexSnapshot:
    """Stable, database-level representation used by the A/B oracle.

    SQLite row ids, update timestamps, and FTS scores are deliberately absent:
    they are implementation details rather than cross-backend behavior.
    """

    files: tuple[tuple[object, ...], ...]
    chunks: tuple[tuple[object, ...], ...]
    extraction_state: tuple[tuple[object, ...], ...]
    issues: tuple[tuple[str, str, str], ...]
    roots: tuple[str, ...]
    search: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]


def _backend_factories() -> tuple[tuple[str, BackendFactory], ...]:
    return (
        ("python", PythonScanBackend),
        ("rust", RustScanBackend),
    )


def _snapshot(
    database: SearchDatabase,
    queries: tuple[str, ...],
) -> _IndexSnapshot:
    store = ChunkStore(database.db_path)
    with store.connect() as conn:
        file_rows = conn.execute(
            """
            SELECT path, filename, extension, modified_time, size, last_error
            FROM files
            ORDER BY path
            """
        ).fetchall()
        files = tuple(
            (
                str(row["path"]),
                str(row["filename"]),
                str(row["extension"]),
                float(row["modified_time"]),
                int(row["size"]),
                row["last_error"],
            )
            for row in file_rows
        )

        chunk_rows = conn.execute(
            """
            SELECT
                f.path, c.ordinal, c.location, c.content,
                s.locator_version, s.kind, s.title, s.page, s.slide, s.sheet,
                s.row_start, s.row_end, s.line_start, s.line_end,
                s.block_start, s.block_end
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            LEFT JOIN chunk_structure s ON s.chunk_id = c.id
            ORDER BY f.path, c.ordinal
            """
        ).fetchall()
        chunks = tuple(
            (
                str(row["path"]),
                int(row["ordinal"]),
                str(row["location"] or ""),
                decode_chunk_content(row["content"]),
                row["locator_version"],
                row["kind"],
                row["title"],
                row["page"],
                row["slide"],
                row["sheet"],
                row["row_start"],
                row["row_end"],
                row["line_start"],
                row["line_end"],
                row["block_start"],
                row["block_end"],
            )
            for row in chunk_rows
        )

        state_rows = conn.execute(
            """
            SELECT
                path, revision, status, source_modified_time, source_size,
                failure_count, retry_after
            FROM extraction_state
            ORDER BY path
            """
        ).fetchall()
        extraction_state = tuple(
            (
                str(row["path"]),
                int(row["revision"]),
                str(row["status"]),
                None
                if row["source_modified_time"] is None
                else float(row["source_modified_time"]),
                None if row["source_size"] is None else int(row["source_size"]),
                int(row["failure_count"]),
                bool(float(row["retry_after"] or 0) > 0),
            )
            for row in state_rows
        )

    issues = tuple(
        sorted(
            (issue.path, issue.error_code, issue.detail)
            for issue in IndexIssueStore(database.db_path).list()
        )
    )

    search = tuple(
        (
            query,
            tuple(
                sorted(
                    (
                        row.path,
                        row.filename,
                        row.extension,
                        row.size,
                        row.location,
                        row.snippet,
                    )
                    for row in store.search(query)
                )
            ),
        )
        for query in queries
    )
    return _IndexSnapshot(
        files=files,
        chunks=chunks,
        extraction_state=extraction_state,
        issues=issues,
        roots=tuple(sorted(database.get_index_roots())),
        search=search,
    )


class FullIndexBackendRegressionTests(unittest.TestCase):
    """Compare the complete Python and Rust discovery lifecycles.

    These tests intentionally use one source tree and two independent
    databases. The parser, writer, SQLite schema, and source mtimes are shared;
    only the discovery backend changes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if docseek_rust is None:
            raise unittest.SkipTest("docseek_rust wheel is not installed")

    @staticmethod
    def _scan(
        database_path: Path,
        root: Path,
        backend_factory: BackendFactory,
        **options: object,
    ) -> tuple[SearchDatabase, DirectoryIndexer, IndexStats]:
        database = SearchDatabase(database_path)
        database.add_index_root(str(root))
        indexer = DirectoryIndexer(
            database,
            scan_backend=backend_factory(),
            **options,
        )
        return database, indexer, indexer.scan(root)

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _assert_backend_parity(
        self,
        python_stats: IndexStats,
        python_snapshot: _IndexSnapshot,
        rust_stats: IndexStats,
        rust_snapshot: _IndexSnapshot,
    ) -> None:
        self.assertEqual(python_stats, rust_stats)
        self.assertEqual(python_snapshot, rust_snapshot)

    def _populate_boundary_fixture(self, root: Path) -> dict[str, object]:
        self._write(root / "visible.txt", "共同回归 marker visible")
        self._write(root / "项目资料" / "说明.txt", "Unicode marker 中文路径")
        self._write(root / "pattern-secret.txt", "pattern excluded")
        self._write(root / "disabled.log", "disabled extension")
        self._write(root / "~$office.docx", "Office lock file")
        self._write(root / "scratch.tmp", "temporary file")
        self._write(root / "empty.txt", "")
        self._write(root / "large.txt", "L" * 256)
        (root / "broken.docx").write_bytes(b"not a valid Office package")

        excluded = root / "excluded"
        self._write(excluded / "secret.txt", "excluded directory")
        ignored = root / ".git"
        self._write(ignored / "metadata.txt", "ignored directory")
        return {
            "enabled_extensions": {".txt", ".docx"},
            "excluded_paths": [str(excluded)],
            "excluded_file_patterns": ["pattern-*.txt"],
            "max_file_size": 128,
        }

    def _populate_real_corpus(self, root: Path) -> dict[str, object]:
        fixture_root = REPO_ROOT / "tests" / "fixtures" / "official"
        for name in ("testWORD.docx", "testEXCEL.xlsx", "testPPT.pptx", "testPDF.pdf"):
            shutil.copy2(fixture_root / name, root / name)
        self._write(root / "marker.txt", "real corpus search marker")
        return {
            "enabled_extensions": {".txt", ".docx", ".xlsx", ".pptx", ".pdf"},
            "max_file_size": 10 * 1024 * 1024,
        }

    def test_fresh_full_index_matches_files_chunks_states_issues_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "boundary"
            root.mkdir()
            options = self._populate_boundary_fixture(root)
            queries = ("共同回归", "Unicode marker", "pattern excluded")

            python_db, _, python_stats = self._scan(
                base / "python.db", root, PythonScanBackend, **options
            )
            rust_db, _, rust_stats = self._scan(
                base / "rust.db", root, RustScanBackend, **options
            )
            self._assert_backend_parity(
                python_stats,
                _snapshot(python_db, queries),
                rust_stats,
                _snapshot(rust_db, queries),
            )

            python_issues = {
                issue.error_code
                for issue in IndexIssueStore(python_db.db_path).list()
            }
            self.assertIn("file_too_large", python_issues)
            self.assertNotIn("permission_denied", python_issues)

    def test_real_office_pdf_corpus_has_full_index_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "real-corpus"
            root.mkdir()
            options = self._populate_real_corpus(root)
            queries = ("real corpus search marker", "document", "test")

            python_db, _, python_stats = self._scan(
                base / "python.db", root, PythonScanBackend, **options
            )
            rust_db, _, rust_stats = self._scan(
                base / "rust.db", root, RustScanBackend, **options
            )
            self._assert_backend_parity(
                python_stats,
                _snapshot(python_db, queries),
                rust_stats,
                _snapshot(rust_db, queries),
            )

    def test_unchanged_rescan_is_identical_after_fresh_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            self._write(root / "stable.txt", "unchanged marker")
            self._write(root / "项目资料" / "说明.md", "unchanged 中文 marker")
            queries = ("unchanged marker", "中文 marker")

            results: dict[str, tuple[IndexStats, _IndexSnapshot]] = {}
            for name, backend_factory in _backend_factories():
                database, _, first_stats = self._scan(
                    base / f"{name}.db", root, backend_factory
                )
                second_indexer = DirectoryIndexer(
                    database,
                    scan_backend=backend_factory(),
                )
                second_stats = second_indexer.scan(root)
                self.assertEqual(second_stats.indexed, 0)
                self.assertEqual(second_stats.unchanged, 2)
                self.assertEqual(second_stats.removed, 0)
                self.assertEqual(second_stats.skipped, 0)
                results[name] = (second_stats, _snapshot(database, queries))
                self.assertEqual(first_stats.indexed, 2)

            self._assert_backend_parity(
                results["python"][0],
                results["python"][1],
                results["rust"][0],
                results["rust"][1],
            )

    def test_modify_add_delete_and_rename_reconcile_identically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            self._write(root / "stable.txt", "stable content")
            self._write(root / "modify.txt", "old content")
            self._write(root / "delete.txt", "delete me")
            self._write(root / "rename-old.txt", "rename me")
            queries = (
                "stable content",
                "new content",
                "new file",
                "rename me",
                "delete me",
            )

            databases: dict[str, SearchDatabase] = {}
            for name, backend_factory in _backend_factories():
                database, _, first_stats = self._scan(
                    base / f"{name}.db", root, backend_factory
                )
                self.assertEqual(first_stats.indexed, 4)
                databases[name] = database

            # Apply one shared filesystem transition after both databases have
            # established the same baseline.
            self._write(root / "modify.txt", "new content after modification")
            self._write(root / "new.txt", "new file content")
            (root / "delete.txt").unlink()
            (root / "rename-old.txt").rename(root / "rename-new.txt")

            results: dict[str, tuple[IndexStats, _IndexSnapshot]] = {}
            for name, backend_factory in _backend_factories():
                second_stats = DirectoryIndexer(
                    databases[name],
                    scan_backend=backend_factory(),
                ).scan(root)
                results[name] = (
                    second_stats,
                    _snapshot(databases[name], queries),
                )

            self._assert_backend_parity(
                results["python"][0],
                results["python"][1],
                results["rust"][0],
                results["rust"][1],
            )
            self.assertEqual(results["python"][0].removed, 2)
            self.assertEqual(results["python"][0].indexed, 3)
            self.assertEqual(results["python"][0].unchanged, 1)
            paths = {row[0] for row in results["python"][1].files}
            self.assertNotIn(str((root / "delete.txt").resolve()), paths)
            self.assertNotIn(str((root / "rename-old.txt").resolve()), paths)
            self.assertIn(str((root / "rename-new.txt").resolve()), paths)

    def test_cancel_retry_and_database_reopen_preserve_final_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            for index in range(24):
                self._write(root / f"document-{index:02d}.txt", f"retry marker {index}")
            queries = ("retry marker", "document")

            baseline_db, _, _ = self._scan(
                base / "baseline.db", root, PythonScanBackend
            )
            baseline = _snapshot(baseline_db, queries)

            for name, backend_factory in _backend_factories():
                with self.subTest(backend=name):
                    database_path = base / f"cancel-{name}.db"
                    database = SearchDatabase(database_path)
                    database.add_index_root(str(root))
                    indexer = DirectoryIndexer(
                        database,
                        scan_backend=backend_factory(),
                    )
                    cancelled = False

                    def cancel_after_committed_batch(
                        _path: Path,
                        stats: IndexStats,
                    ) -> None:
                        nonlocal cancelled
                        if not cancelled and stats.indexed >= 16:
                            cancelled = True
                            indexer.cancel()

                    with self.assertRaises(IndexCancelled):
                        indexer.scan(root, on_progress=cancel_after_committed_batch)
                    self.assertTrue(cancelled)

                    # Reopening the database is part of the contract: a stop
                    # must not leave the writer connection or a partial batch
                    # blocking the next indexing run.
                    reopened = SearchDatabase(database_path)
                    retry_stats = DirectoryIndexer(
                        reopened,
                        scan_backend=backend_factory(),
                    ).scan(root)
                    self.assertEqual(retry_stats.removed, 0)
                    self.assertEqual(_snapshot(reopened, queries), baseline)

    @staticmethod
    def _force_stale_extracting(database: SearchDatabase, path: Path) -> None:
        normalized = str(path.resolve())
        revision = current_extraction_revision(path.suffix)
        stale_time = time.time() - 60
        store = ChunkStore(database.db_path)
        with store.connect() as conn:
            conn.execute(
                """
                UPDATE extraction_state
                SET revision = ?, status = ?, updated_at = ?,
                    owner_pid = NULL, started_at = ?
                WHERE path = ?
                """,
                (
                    revision,
                    str(ExtractionStatus.EXTRACTING),
                    stale_time,
                    stale_time,
                    normalized,
                ),
            )

    def test_restart_recovery_is_backend_parity_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            target = root / "recover.txt"
            self._write(target, "restart recovery marker")
            queries = ("restart recovery marker",)
            results: dict[str, _IndexSnapshot] = {}

            for name, backend_factory in _backend_factories():
                database, _, first_stats = self._scan(
                    base / f"{name}.db", root, backend_factory
                )
                self.assertEqual(first_stats.indexed, 1)
                self._force_stale_extracting(database, target)

                repaired = DirectoryIndexer(
                    database,
                    scan_backend=backend_factory(),
                ).scan(root)
                self.assertEqual(repaired.indexed, 1)
                self.assertEqual(repaired.skipped, 0)
                results[name] = _snapshot(database, queries)

                with ChunkStore(database.db_path).connect() as conn:
                    state = conn.execute(
                        "SELECT status FROM extraction_state WHERE path = ?",
                        (str(target.resolve()),),
                    ).fetchone()
                self.assertEqual(str(state["status"]), ExtractionStatus.INDEXED)

            self.assertEqual(results["python"], results["rust"])


if __name__ == "__main__":
    unittest.main()
