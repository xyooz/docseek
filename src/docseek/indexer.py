from __future__ import annotations

import json
import logging
import os
import threading
import time
from itertools import islice
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .chunk_store import ChunkStore
from .chunk_writer import ChunkBatchWriter
from .chunks import HTML_EXTENSIONS, TEXT_EXTENSIONS, iter_document_chunks
from .extraction_revision import current_extraction_revision
from .extraction_state import (
    ExtractionStatus,
    failed_state_is_deferred,
    retry_delay_seconds,
    status_for_error_code,
    status_for_extraction_result,
    unchanged_state_is_complete,
)
from .file_exclusions import (
    FILE_EXCLUSION_PATTERNS_KEY,
    decode_file_exclusion_patterns,
    matches_file_exclusion,
    normalize_file_exclusion_patterns,
)
from .index_cleanup import remove_missing_under_root
from .index_health import record_successful_reconcile
from .index_issues import IndexIssueStore
from .index_formats import (
    ENABLED_INDEX_EXTENSIONS_KEY,
    decode_enabled_extensions,
    normalize_enabled_extensions,
)
from .index_priority import prioritize_index_candidates
from .document_types import KNOWN_DOCUMENT_EXTENSIONS
from .scan_backend import (
    MAX_SCAN_BATCH_SIZE,
    PythonScanBackend,
    RustScanBackendUnavailable,
    ScanBackend,
    ScanCancelled,
    ScanConfig,
    ScanIssue,
    ScanSession,
    iter_scan_candidates,
    resolve_scan_backend,
)
from .search_db import SearchDatabase


logger = logging.getLogger(__name__)


DEFAULT_IGNORED_DIR_NAMES = {
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    "$recycle.bin",
    "system volume information",
}
# Small text files benefit substantially from fewer FTS5 transaction boundaries.
# The separate text-character cap still bounds writer-lock duration and memory for
# larger documents, while Office/PDF/compatibility formats continue to spool and
# flush per document in ChunkBatchWriter.
FULL_SCAN_BATCH_SIZE = 512
FULL_SCAN_BATCH_TEXT_CHARS = 8_000_000
# Commit the first useful results quickly, then widen batches for sustained
# throughput. This makes a first search possible without returning to the high
# transaction overhead of committing every small text document.
FULL_SCAN_EARLY_COMMIT_COUNTS = frozenset({16, 64})
DISCOVERY_PROGRESS_INTERVAL_SECONDS = 0.15
DISCOVERY_PROGRESS_CANDIDATE_STEP = 250
MAX_INTERRUPTED_ATTEMPTS_BEFORE_QUARANTINE = 3
LEGACY_EXTRACTING_STALE_SECONDS = 5.0
PARSER_LEASE_EXTENSIONS = frozenset(
    KNOWN_DOCUMENT_EXTENSIONS - TEXT_EXTENSIONS - HTML_EXTENSIONS - {".xml"}
)


@dataclass(slots=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    chunks: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0
    excluded: int = 0
    no_text: int = 0
    ocr_required: int = 0

    def merge(self, other: "IndexStats") -> None:
        self.scanned += other.scanned
        self.indexed += other.indexed
        self.chunks += other.chunks
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        self.removed += other.removed
        self.excluded += other.excluded
        self.no_text += other.no_text
        self.ocr_required += other.ocr_required


class IndexCancelled(Exception):
    pass


class SourceChangedDuringExtraction(Exception):
    pass


class DirectoryIndexer:
    """Incremental local indexer using bounded, location-aware chunks."""

    def __init__(
        self,
        database: SearchDatabase,
        *,
        max_file_size: int | None = None,
        ignored_dir_names: set[str] | None = None,
        excluded_paths: list[str] | None = None,
        excluded_file_patterns: list[str] | None = None,
        enabled_extensions: set[str] | frozenset[str] | None = None,
        scan_backend: ScanBackend | None = None,
    ) -> None:
        self.database = database
        self.chunk_store = ChunkStore(database.db_path)
        self.issues = IndexIssueStore(database.db_path)

        (
            configured_max_mb,
            configured_excluded,
            configured_file_patterns,
            configured_enabled_extensions,
        ) = self._load_runtime_settings()
        if max_file_size is None:
            max_file_size = configured_max_mb * 1024 * 1024
        self.max_file_size = max_file_size
        self.ignored_dir_names = {
            name.casefold() for name in (ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES)
        }
        raw_excluded = excluded_paths if excluded_paths is not None else configured_excluded
        self.excluded_paths = [Path(path).resolve() for path in raw_excluded]
        raw_patterns = (
            excluded_file_patterns
            if excluded_file_patterns is not None
            else configured_file_patterns
        )
        self.excluded_file_patterns = normalize_file_exclusion_patterns(raw_patterns)
        self.enabled_extensions = (
            configured_enabled_extensions
            if enabled_extensions is None
            else normalize_enabled_extensions(enabled_extensions)
        )
        self.scan_backend = (
            scan_backend if scan_backend is not None else resolve_scan_backend()
        )
        self._cancel = threading.Event()
        self._scan_session_lock = threading.Lock()
        self._active_scan_session: ScanSession | None = None

    @property
    def scan_backend_name(self) -> str:
        """Return the backend currently selected for the indexer."""
        return str(getattr(self.scan_backend, "scan_backend_name", "custom"))

    def _load_runtime_settings(
        self,
    ) -> tuple[int, list[str], list[str], frozenset[str]]:
        """Read indexer settings with one lightweight metadata connection.

        The legacy SearchDatabase connection still owns compatibility FTS
        tables and historically executed journal-mode setup on every connect.
        Indexing is latency-sensitive, so runtime settings are read directly
        from the shared metadata table instead of paying those legacy costs for
        every watcher batch.
        """
        with self.chunk_store.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings "
                "WHERE key IN ('max_file_size_mb', 'excluded_paths', ?, ?) ",
                (FILE_EXCLUSION_PATTERNS_KEY, ENABLED_INDEX_EXTENSIONS_KEY),
            ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}

        try:
            max_mb = max(1, min(int(values.get("max_file_size_mb", "200")), 4096))
        except ValueError:
            max_mb = 200

        excluded: list[str] = []
        raw_excluded = values.get("excluded_paths")
        if raw_excluded:
            try:
                parsed = json.loads(raw_excluded)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                excluded = [str(item) for item in parsed if item]

        file_patterns = decode_file_exclusion_patterns(
            values.get(FILE_EXCLUSION_PATTERNS_KEY)
        )
        enabled_extensions = decode_enabled_extensions(
            values.get(ENABLED_INDEX_EXTENSIONS_KEY)
        )
        return max_mb, excluded, file_patterns, enabled_extensions

    def cancel(self) -> None:
        self._cancel.set()
        with self._scan_session_lock:
            session = self._active_scan_session
        if session is None:
            return
        try:
            session.cancel()
        except Exception:
            # The Python cancellation event remains authoritative for parser
            # and indexing work. A backend cancellation failure must not make
            # the UI stop action itself fail.
            logger.warning("scan backend cancellation failed", exc_info=True)

    def _set_active_scan_session(self, session: ScanSession) -> None:
        with self._scan_session_lock:
            self._active_scan_session = session

    def _clear_active_scan_session(self, session: ScanSession) -> None:
        with self._scan_session_lock:
            if self._active_scan_session is session:
                self._active_scan_session = None

    def _start_scan_session(self, root: Path) -> ScanSession:
        config = ScanConfig(
            enabled_extensions=self.enabled_extensions,
            ignored_dir_names=self.ignored_dir_names,
            excluded_paths=self.excluded_paths,
            excluded_file_patterns=self.excluded_file_patterns,
            max_file_size=self.max_file_size,
        )
        try:
            session = self.scan_backend.start_scan(root, config)
        except RustScanBackendUnavailable:
            # The fallback boundary is intentionally only around
            # start_scan(). Once a session exists, candidate/traversal errors
            # must not restart discovery and duplicate lifecycle work.
            if not bool(getattr(self.scan_backend, "allow_fallback", True)):
                logger.error(
                    "Rust scan backend unavailable; strict Rust mode is enabled"
                )
                raise
            logger.warning("Rust scan backend unavailable, falling back to Python")
            self.scan_backend = PythonScanBackend()
            session = self.scan_backend.start_scan(root, config)
        logger.info("Scan backend selected: %s", self.scan_backend_name)
        self._set_active_scan_session(session)
        return session

    @staticmethod
    def _normalize(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

    def _normalize_discovered_candidate(
        self,
        path: Path,
        *,
        normalized_directory: str,
        is_symlink: bool,
    ) -> str:
        """Reuse the resolved parent discovered by the full-scan walker.

        A normal DirEntry is already known to live under the directory we just
        resolved before ``scandir``. Joining that canonical parent with the
        entry's actual name is equivalent to resolving the child again without
        paying another filesystem round trip. File symlinks deliberately keep
        the old full ``resolve`` behavior so aliases still map to their target.
        """
        if is_symlink:
            return self._normalize(path)
        return os.path.join(normalized_directory, path.name)

    def _has_file_record(self, path: str) -> bool:
        with self.chunk_store.connect() as conn:
            row = conn.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)).fetchone()
        return row is not None

    def _is_unchanged(
        self,
        path: str,
        *,
        modified_time: float,
        size: int,
        extraction_revision: int,
    ) -> bool:
        with self.chunk_store.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    f.modified_time,
                    f.size,
                    EXISTS(
                        SELECT 1 FROM chunks c
                        WHERE c.file_id = f.id
                    ) AS has_chunk,
                    COALESCE(s.revision, 0) AS extraction_revision,
                    s.status AS extraction_status
                FROM files f
                LEFT JOIN extraction_state s ON s.path = f.path
                WHERE f.path = ?
                """,
                (path,),
            ).fetchone()
        if row is None:
            return False
        return (
            float(row["modified_time"]) == float(modified_time)
            and int(row["size"]) == int(size)
            and int(row["extraction_revision"]) >= int(extraction_revision)
            and unchanged_state_is_complete(
                row["extraction_status"],
                has_chunk=bool(row["has_chunk"]),
            )
        )

    def _load_index_state(
        self,
        root: Path | None = None,
    ) -> dict[
        str,
        tuple[
            float | None,
            int | None,
            bool,
            int,
            str | None,
            float | None,
            int | None,
            float,
        ],
    ]:
        """Load committed and failure-only state once for reconciliation."""
        prefix = self._normalize(root).rstrip("/\\") + os.sep if root is not None else None
        # Directory boundaries exclude siblings such as docs-backup. A range
        # keeps the existing path indexes usable and treats %/_ literally.
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else None
        parameters = (prefix, upper) if prefix else ()
        file_scope = " WHERE f.path >= ? AND f.path < ?" if prefix else ""
        failure_scope = " AND s.path >= ? AND s.path < ?" if prefix else ""
        with self.chunk_store.connect() as conn:
            file_rows = conn.execute(
                """
                SELECT
                    f.path,
                    f.modified_time,
                    f.size,
                    EXISTS(
                        SELECT 1 FROM chunks c
                        WHERE c.file_id = f.id
                    ) AS has_chunk,
                    COALESCE(s.revision, 0) AS extraction_revision,
                    s.status AS extraction_status,
                    s.source_modified_time,
                    s.source_size,
                    COALESCE(s.retry_after, 0) AS retry_after
                FROM files f
                LEFT JOIN extraction_state s ON s.path = f.path
                """
                + file_scope, parameters
            ).fetchall()
            failure_only_rows = conn.execute(
                """
                SELECT
                    s.path,
                    s.revision AS extraction_revision,
                    s.status AS extraction_status,
                    s.source_modified_time,
                    s.source_size,
                    COALESCE(s.retry_after, 0) AS retry_after
                FROM extraction_state s
                LEFT JOIN files f ON f.path = s.path
                WHERE f.path IS NULL
                """
                + failure_scope, parameters
            ).fetchall()

        state = {
            str(row["path"]): (
                float(row["modified_time"]),
                int(row["size"]),
                bool(row["has_chunk"]),
                int(row["extraction_revision"]),
                str(row["extraction_status"]) if row["extraction_status"] is not None else None,
                float(row["source_modified_time"])
                if row["source_modified_time"] is not None
                else None,
                int(row["source_size"]) if row["source_size"] is not None else None,
                float(row["retry_after"]),
            )
            for row in file_rows
        }
        for row in failure_only_rows:
            state[str(row["path"])] = (
                None,
                None,
                False,
                int(row["extraction_revision"]),
                str(row["extraction_status"]) if row["extraction_status"] is not None else None,
                float(row["source_modified_time"])
                if row["source_modified_time"] is not None
                else None,
                int(row["source_size"]) if row["source_size"] is not None else None,
                float(row["retry_after"]),
            )
        return state

    def _record_extraction_state(
        self,
        path: str,
        revision: int,
        status: ExtractionStatus,
    ) -> None:
        with self.chunk_store.connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_state(
                    path, revision, status, updated_at, owner_pid, started_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    owner_pid=NULL,
                    started_at=NULL
                """,
                (path, int(revision), str(status), time.time()),
            )

    def _record_extraction_started(
        self,
        path: str,
        revision: int,
        stat,
    ) -> None:
        """Persist a parser lease before entering third-party extraction code."""
        now = time.time()
        with self.chunk_store.connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_state(
                    path, revision, status, updated_at,
                    source_modified_time, source_size, retry_after,
                    owner_pid, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    source_modified_time=excluded.source_modified_time,
                    source_size=excluded.source_size,
                    retry_after=0,
                    owner_pid=excluded.owner_pid,
                    started_at=excluded.started_at
                """,
                (
                    path,
                    int(revision),
                    str(ExtractionStatus.EXTRACTING),
                    now,
                    float(stat.st_mtime),
                    int(stat.st_size),
                    os.getpid(),
                    now,
                ),
            )

    @staticmethod
    def _process_is_alive(pid: int | None) -> bool:
        if pid is None or int(pid) <= 0:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _recover_interrupted_states(self, root: Path) -> None:
        """Quarantine parser leases left by a process that no longer exists."""
        resolved_root = root.resolve()
        with self.chunk_store.connect() as conn:
            rows = conn.execute(
                """
                SELECT path, owner_pid, started_at, updated_at, failure_count
                FROM extraction_state
                WHERE status = ?
                """,
                (str(ExtractionStatus.EXTRACTING),),
            ).fetchall()

        recovered: list[tuple[str, str, str]] = []
        now = time.time()
        for row in rows:
            stored_path = str(row["path"])
            candidate = Path(stored_path)
            try:
                candidate.resolve().relative_to(resolved_root)
            except ValueError:
                continue

            owner_pid = row["owner_pid"]
            started_at = row["started_at"]
            # Rows written before v12 have no owner. Treat them as abandoned;
            # new rows are safe to identify by the owning process ID.
            if owner_pid is None:
                # v11 had no owner PID. Give a just-written legacy row a short
                # grace period so an in-process repair can finish; older rows
                # are treated as abandoned on the next reconciliation.
                marker_time = started_at or row["updated_at"] or 0
                abandoned = now - float(marker_time) > LEGACY_EXTRACTING_STALE_SECONDS
            else:
                abandoned = not self._process_is_alive(int(owner_pid))
            if not abandoned:
                continue

            attempts = int(row["failure_count"] or 0) + 1
            quarantined = attempts >= MAX_INTERRUPTED_ATTEMPTS_BEFORE_QUARANTINE
            status = (
                ExtractionStatus.QUARANTINED
                if quarantined
                else ExtractionStatus.INTERRUPTED
            )
            error_code = "ParserQuarantined" if quarantined else "ParserInterrupted"
            detail = (
                "文件解析进程在上次运行中异常中断，已隔离；请在问题文件中手动重试。"
                if quarantined
                else "文件解析进程在上次运行中异常中断，已暂停自动重试；请手动重试。"
            )
            with self.chunk_store.connect() as conn:
                conn.execute(
                    """
                    UPDATE extraction_state
                    SET status = ?, updated_at = ?, failure_count = ?,
                        retry_after = ?, owner_pid = NULL, started_at = NULL
                    WHERE path = ? AND status = ?
                    """,
                    (
                        str(status),
                        now,
                        attempts,
                        0,
                        stored_path,
                        str(ExtractionStatus.EXTRACTING),
                    ),
                )
            recovered.append((stored_path, error_code, detail))

        if recovered:
            self.issues.record_many(recovered)

    def _record_failure_state(self, path: str, error_code: str) -> None:
        revision = current_extraction_revision(Path(path).suffix)
        status = status_for_error_code(error_code)
        now = time.time()
        try:
            stat = Path(path).stat()
            source_modified_time: float | None = float(stat.st_mtime)
            source_size: int | None = int(stat.st_size)
        except OSError:
            source_modified_time = None
            source_size = None

        with self.chunk_store.connect() as conn:
            previous = conn.execute(
                """
                SELECT revision, status, source_modified_time, source_size, failure_count
                FROM extraction_state
                WHERE path = ?
                """,
                (path,),
            ).fetchone()
            same_failed_source = (
                previous is not None
                and str(previous["status"]) in {
                    str(ExtractionStatus.FAILED),
                    str(ExtractionStatus.TIMEOUT),
                    str(ExtractionStatus.EXTRACTING),
                    str(ExtractionStatus.SKIPPED),
                    str(ExtractionStatus.INTERRUPTED),
                    str(ExtractionStatus.QUARANTINED),
                }
                and int(previous["revision"]) == int(revision)
                and previous["source_modified_time"] is not None
                and source_modified_time is not None
                and float(previous["source_modified_time"]) == source_modified_time
                and previous["source_size"] is not None
                and source_size is not None
                and int(previous["source_size"]) == source_size
            )
            failure_count = (
                int(previous["failure_count"]) + 1 if same_failed_source else 1
            )
            if status in {
                ExtractionStatus.SKIPPED,
                ExtractionStatus.INTERRUPTED,
                ExtractionStatus.QUARANTINED,
            }:
                # Explicitly deferred states are governed by their status and
                # source metadata, not by a fake one-year timer.
                retry_after = 0
            else:
                retry_after = now + retry_delay_seconds(status, failure_count)
            conn.execute(
                """
                INSERT INTO extraction_state(
                    path, revision, status, updated_at,
                    source_modified_time, source_size, failure_count, retry_after,
                    owner_pid, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    source_modified_time=excluded.source_modified_time,
                    source_size=excluded.source_size,
                    failure_count=excluded.failure_count,
                    retry_after=excluded.retry_after,
                    owner_pid=NULL,
                    started_at=NULL
                """,
                (
                    path,
                    int(revision),
                    str(status),
                    now,
                    source_modified_time,
                    source_size,
                    failure_count,
                    retry_after,
                ),
            )

    def _record_parser_job_cancelled(self, path: str) -> None:
        """Return an actively parsed file to PENDING after a user stop.

        Stopping the index job is deliberately different from asking DocSeek
        to skip a file.  No failure count or deferred retry is added, and a
        later scan may process the unchanged file normally.
        """
        now = time.time()
        with self.chunk_store.connect() as conn:
            row = conn.execute(
                "SELECT status FROM extraction_state WHERE path = ?",
                (path,),
            ).fetchone()
        if row is not None and str(row["status"]) == str(ExtractionStatus.EXTRACTING):
            with self.chunk_store.connect() as conn:
                conn.execute(
                    """
                    UPDATE extraction_state
                    SET status = ?, updated_at = ?, retry_after = 0,
                        owner_pid = NULL, started_at = NULL
                    WHERE path = ? AND status = ?
                    """,
                    (
                        str(ExtractionStatus.PENDING),
                        now,
                        path,
                        str(ExtractionStatus.EXTRACTING),
                    ),
                )
            self.issues.clear(path)

    # Compatibility alias for embedders that used the old private helper.
    def _record_parser_cancelled(self, path: str) -> None:
        self._record_parser_job_cancelled(path)

    def _remove_indexed_path(self, path: str, stats: IndexStats) -> None:
        if self._has_file_record(path):
            self.chunk_store.remove_document(path)
            stats.removed += 1
        with self.chunk_store.connect() as conn:
            conn.execute("DELETE FROM extraction_state WHERE path = ?", (path,))

    @staticmethod
    def _error_code(exc: Exception) -> str:
        # TimeoutError is an OSError subclass on Python/Windows. Preserve the
        # parser-specific type before the broad OS mapping so the lifecycle can
        # distinguish a killable parser timeout from a filesystem failure.
        if type(exc).__name__ == "LegacyExtractionTimeout":
            return "LegacyExtractionTimeout"
        if type(exc).__name__ == "LegacyExtractionCancelled":
            return "ParserCancelled"
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, FileNotFoundError):
            return "file_not_found"
        if isinstance(exc, OSError):
            return "os_error"
        return type(exc).__name__

    def _is_supported_candidate(self, path: Path) -> bool:
        name = path.name
        return (
            not name.startswith("~$")
            and not name.endswith(".tmp")
            and path.suffix.lower() in self.enabled_extensions
        )

    def _is_file_pattern_excluded(self, path: Path) -> bool:
        return matches_file_exclusion(path, self.excluded_file_patterns)

    def _index_existing_file(
        self,
        path: Path,
        normalized: str,
        stats: IndexStats,
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
        on_detail: Callable[[Path, str, int], None] | None = None,
        prefetched_state: tuple[
            float | None,
            int | None,
            bool,
            int,
            str | None,
            float | None,
            int | None,
            float,
        ]
        | None = None,
        state_prefetched: bool = False,
        writer: ChunkBatchWriter | None = None,
        clear_issue: bool = True,
    ) -> bool:
        stat = path.stat()
        if stat.st_size > self.max_file_size:
            if writer is not None:
                writer.flush()
            self._remove_indexed_path(normalized, stats)
            stats.skipped += 1
            actual_mb = stat.st_size / (1024 * 1024)
            limit_mb = self.max_file_size / (1024 * 1024)
            self.issues.record(
                normalized,
                "file_too_large",
                f"文件大小 {actual_mb:.1f} MB，当前索引上限 {limit_mb:.0f} MB",
            )
            return False

        extraction_revision = current_extraction_revision(path.suffix)
        if state_prefetched:
            if (
                prefetched_state is not None
                and failed_state_is_deferred(
                    prefetched_state[4],
                    stored_revision=prefetched_state[3],
                    current_revision=extraction_revision,
                    source_modified_time=prefetched_state[5],
                    source_size=prefetched_state[6],
                    current_modified_time=stat.st_mtime,
                    current_size=stat.st_size,
                    retry_after=prefetched_state[7],
                )
            ):
                stats.skipped += 1
                return False

            unchanged = (
                prefetched_state is not None
                and prefetched_state[0] is not None
                and prefetched_state[1] is not None
                and float(prefetched_state[0]) == float(stat.st_mtime)
                and int(prefetched_state[1]) == int(stat.st_size)
                and int(prefetched_state[3]) >= extraction_revision
                and unchanged_state_is_complete(
                    prefetched_state[4],
                    has_chunk=bool(prefetched_state[2]),
                )
            )
        else:
            unchanged = self._is_unchanged(
                normalized,
                modified_time=stat.st_mtime,
                size=stat.st_size,
                extraction_revision=extraction_revision,
            )

        if unchanged:
            if clear_issue:
                self.issues.clear(normalized)
            stats.unchanged += 1
            return True

        if on_progress:
            on_progress(path, stats)

        def report_detail(location: str, current: int) -> None:
            if self._cancel.is_set():
                raise IndexCancelled()
            if on_detail:
                on_detail(path, location, current)
            elif on_progress:
                display = Path(f"{path.name} · {location} · 已读取 {current:,} 行")
                on_progress(display, stats)

        extension = path.suffix.lower()
        expected_source = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

        def validate_source() -> None:
            if self._cancel.is_set():
                raise IndexCancelled()
            current = path.stat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != expected_source:
                raise SourceChangedDuringExtraction("文件在解析期间发生变化，已放弃本次结果，等待重新索引。")

        # This commit is intentionally separate from the chunk transaction:
        # if the parser process or the host dies, the next launch can see that
        # extraction was in progress even though no document rows were saved.
        if extension in PARSER_LEASE_EXTENSIONS:
            if self._cancel.is_set():
                raise IndexCancelled()
            # The lease is written through a separate metadata connection. Do
            # not let a preceding lightweight text batch hold SQLite's single
            # writer slot while that durable parser marker is recorded.
            if writer is not None:
                writer.flush()
            self._record_extraction_started(normalized, extraction_revision, stat)

        common_args = {
            "path": normalized,
            "filename": path.name,
            "extension": extension,
            "modified_time": stat.st_mtime,
            "size": stat.st_size,
            "validate_source": validate_source,
            "chunks": iter_document_chunks(
                path,
                on_progress=report_detail
                if on_detail or on_progress or extension == ".xlsx"
                else None,
                cancelled=self._cancel.is_set,
            ),
        }
        if writer is not None:
            chunk_count = writer.replace_document(
                **common_args,
                extraction_revision=extraction_revision,
            )
        else:
            with ChunkBatchWriter(self.chunk_store, batch_size=1) as single_writer:
                chunk_count = single_writer.replace_document(
                    **common_args, extraction_revision=extraction_revision)

        status = status_for_extraction_result(extension, chunk_count)
        if clear_issue:
            self.issues.clear(normalized)
        stats.indexed += 1
        stats.chunks += chunk_count
        if status is ExtractionStatus.NO_TEXT:
            stats.no_text += 1
        elif status is ExtractionStatus.OCR_REQUIRED:
            stats.ocr_required += 1
        return True

    def update_paths(
        self,
        paths: Iterable[Path],
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
        on_detail: Callable[[Path, str, int], None] | None = None,
    ) -> IndexStats:
        """Update only the changed filesystem paths reported by the watcher."""
        stats = IndexStats()
        processed: set[str] = set()

        for path in paths:
            if self._cancel.is_set():
                raise IndexCancelled()

            normalized = self._normalize(path)
            if normalized in processed:
                continue
            processed.add(normalized)
            stats.scanned += 1

            try:
                if self._is_excluded(path):
                    self._remove_indexed_path(normalized, stats)
                    self.issues.clear(normalized)
                    stats.excluded += 1
                    continue

                if not self._is_supported_candidate(path):
                    self._remove_indexed_path(normalized, stats)
                    self.issues.clear(normalized)
                    continue

                if self._is_file_pattern_excluded(path):
                    self._remove_indexed_path(normalized, stats)
                    self.issues.clear(normalized)
                    stats.excluded += 1
                    continue

                try:
                    path.stat()
                except FileNotFoundError:
                    self._remove_indexed_path(normalized, stats)
                    self.issues.clear(normalized)
                    continue

                self._index_existing_file(
                    path,
                    normalized,
                    stats,
                    on_progress=on_progress,
                    on_detail=on_detail,
                )
            except IndexCancelled:
                self._record_parser_job_cancelled(normalized)
                raise
            except SourceChangedDuringExtraction as exc:
                stats.skipped += 1
                self._record_extraction_state(
                    normalized,
                    current_extraction_revision(path.suffix),
                    ExtractionStatus.PENDING,
                )
                self.issues.record(normalized, "source_changed", str(exc))
            except FileNotFoundError:
                self._remove_indexed_path(normalized, stats)
                self.issues.clear(normalized)
            except Exception as exc:
                if type(exc).__name__ == "LegacyExtractionCancelled":
                    self._record_parser_job_cancelled(normalized)
                    raise IndexCancelled() from exc
                stats.skipped += 1
                error_code = self._error_code(exc)
                self._record_failure_state(normalized, error_code)
                self.issues.record(normalized, error_code, str(exc))

        return stats

    def scan(
        self,
        root: Path,
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
        on_detail: Callable[[Path, str, int], None] | None = None,
        on_discovery: Callable[[Path, int], None] | None = None,
        on_candidates_ready: Callable[[int], None] | None = None,
    ) -> IndexStats:
        root = root.resolve()
        self._cancel.clear()
        stats = IndexStats()
        seen_paths: set[str] = set()
        successful_paths: set[str] = set()
        self._recover_interrupted_states(root)
        index_state = self._load_index_state(root)

        discovery_issue_clears: set[str] = set()
        discovery_issue_records: list[tuple[str, str, str]] = []
        normalized_candidates: dict[str, str] = {}

        discovery_callback = on_discovery
        if discovery_callback is None and on_progress is not None:
            def discovery_callback(path: Path, count: int) -> None:
                current = path.name or str(path)
                on_progress(
                    Path(
                        f"正在扫描目录 · 已发现 {count:,} 个候选文件 · 当前：{current}"
                    ),
                    stats,
                )

        def report_backend_discovery(path: Path, count: int) -> None:
            # ScanBackend owns traversal now, but directory issue cleanup stays
            # in the indexer lifecycle. A progress path is either a visited
            # directory or an entry inside one; clearing the corresponding
            # directory key preserves the old no-stale-issue contract without
            # opening SQLite during discovery.
            candidates = [path]
            if not path.is_dir():
                candidates.append(path.parent)
            for candidate in candidates:
                if candidate.is_dir():
                    discovery_issue_clears.add(self._normalize(candidate))
            if discovery_callback is not None:
                discovery_callback(path, count)

        def report_backend_issues(issues: list[ScanIssue]) -> None:
            for issue in issues:
                normalized = self._normalize(issue.path)
                discovery_issue_records.append(
                    (normalized, issue.error_code, issue.detail)
                )
                stats.skipped += 1

        scan_session = self._start_scan_session(root)
        discovery = iter_scan_candidates(
            scan_session,
            max_items=MAX_SCAN_BATCH_SIZE,
            on_discovery=report_backend_discovery,
            on_progress=lambda progress: setattr(stats, "excluded", progress.excluded),
            on_issues=report_backend_issues,
        )
        normalized_directories = {root: self._normalize(root)}

        def normalize_backend_candidate(path: Path) -> str:
            key = str(path)
            existing = normalized_candidates.get(key)
            if existing is not None:
                return existing

            parent = path.parent
            normalized_directory = normalized_directories.get(parent)
            if normalized_directory is None:
                normalized_directory = self._normalize(parent)
                normalized_directories[parent] = normalized_directory
            normalized = self._normalize_discovered_candidate(
                path,
                normalized_directory=normalized_directory,
                is_symlink=path.is_symlink(),
            )
            normalized_candidates[key] = normalized
            return normalized

        def candidate_batches():
            discovered = 0
            try:
                while True:
                    if self._cancel.is_set():
                        writer.abort()
                        raise IndexCancelled()
                    # Finish the previous write transaction before more filesystem
                    # discovery; issue metadata remains buffered until the end.
                    writer.flush()
                    try:
                        batch = list(islice(discovery, MAX_SCAN_BATCH_SIZE))
                    except ScanCancelled as exc:
                        writer.abort()
                        raise IndexCancelled() from exc
                    discovered += len(batch)
                    finished = len(batch) < MAX_SCAN_BATCH_SIZE
                    if finished:
                        if on_candidates_ready:
                            on_candidates_ready(discovered)
                        elif on_progress:
                            on_progress(
                                Path(f"扫描完成 · 共发现 {discovered:,} 个候选文件"), stats
                            )
                    yield from prioritize_index_candidates(batch)
                    if finished:
                        break
            finally:
                self._clear_active_scan_session(scan_session)

        # Discovery stays outside the batched chunk writer. Healthy directory
        # traversal therefore performs no SQLite writes, while issue cleanup is
        # collapsed into at most two short metadata transactions before content
        # indexing starts. This preserves the single-writer invariant that fixed
        # the real Windows ``database is locked`` reports without paying one
        # connection/commit per visited directory.
        last_path = None

        with ChunkBatchWriter(
            self.chunk_store,
            batch_size=FULL_SCAN_BATCH_SIZE,
            max_batch_text_chars=FULL_SCAN_BATCH_TEXT_CHARS,
        ) as writer:
            for path in candidate_batches():
                last_path = path
                if self._cancel.is_set():
                    writer.abort()
                    raise IndexCancelled()

                stats.scanned += 1
                normalized = normalize_backend_candidate(path)
                seen_paths.add(normalized)

                try:
                    success = self._index_existing_file(
                        path,
                        normalized,
                        stats,
                        on_progress=on_progress,
                        on_detail=on_detail,
                        prefetched_state=index_state.get(normalized),
                        state_prefetched=True,
                        writer=writer,
                        clear_issue=False,
                    )
                    if success:
                        successful_paths.add(normalized)
                    if stats.indexed in FULL_SCAN_EARLY_COMMIT_COUNTS:
                        writer.flush()
                    if on_progress and stats.scanned % 250 == 0:
                        on_progress(path, stats)
                except IndexCancelled:
                    writer.abort()
                    self._record_parser_job_cancelled(normalized)
                    raise
                except SourceChangedDuringExtraction as exc:
                    writer.flush()
                    stats.skipped += 1
                    self._record_extraction_state(
                        normalized,
                        current_extraction_revision(path.suffix),
                        ExtractionStatus.PENDING,
                    )
                    self.issues.record(normalized, "source_changed", str(exc))
                except FileNotFoundError:
                    writer.flush()
                    self._remove_indexed_path(normalized, stats)
                    self.issues.clear(normalized)
                except Exception as exc:
                    if type(exc).__name__ == "LegacyExtractionCancelled":
                        writer.abort()
                        self._record_parser_job_cancelled(normalized)
                        raise IndexCancelled() from exc
                    writer.flush()
                    stats.skipped += 1
                    error_code = self._error_code(exc)
                    self._record_failure_state(normalized, error_code)
                    self.issues.record(normalized, error_code, str(exc))

        self.issues.clear_many(discovery_issue_clears)
        self.issues.record_many(discovery_issue_records)
        if on_progress and last_path is not None:
            # Always publish the terminal count. Unchanged files deliberately
            # report in coarse batches, so without this event a short refresh
            # could disappear while its progress bar still showed 0%.
            on_progress(last_path, stats)

        self.issues.clear_many(successful_paths)
        stats.removed += remove_missing_under_root(self.chunk_store, str(root), seen_paths)
        self.issues.clear_under_root_if_missing(str(root), seen_paths)
        self.database.add_index_root(str(root))
        record_successful_reconcile(self.database)
        return stats

    def _is_excluded(self, path: Path) -> bool:
        if not self.excluded_paths:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        for excluded in self.excluded_paths:
            if resolved == excluded:
                return True
            try:
                resolved.relative_to(excluded)
                return True
            except ValueError:
                continue
        return False

    def _iter_supported_files(
        self,
        root: Path,
        stats: IndexStats,
        *,
        issue_clears: set[str] | None = None,
        issue_records: list[tuple[str, str, str]] | None = None,
        on_discovery: Callable[[Path, int], None] | None = None,
        normalized_candidates: dict[str, str] | None = None,
    ) -> Iterable[Path]:
        def clear_issue(path: str) -> None:
            if issue_clears is None:
                self.issues.clear(path)
            else:
                issue_clears.add(path)

        def record_issue(path: str, error_code: str, detail: str) -> None:
            if issue_records is None:
                self.issues.record(path, error_code, detail)
            else:
                issue_records.append((path, error_code, detail))

        discovered_candidates = 0
        last_reported_candidates = 0
        last_reported_at = 0.0

        def report_discovery(path: Path, *, force: bool = False) -> None:
            nonlocal last_reported_at, last_reported_candidates
            if on_discovery is None:
                return
            now = time.monotonic()
            if not force:
                candidate_delta = discovered_candidates - last_reported_candidates
                if (
                    candidate_delta < DISCOVERY_PROGRESS_CANDIDATE_STEP
                    and now - last_reported_at < DISCOVERY_PROGRESS_INTERVAL_SECONDS
                ):
                    return
            on_discovery(path, discovered_candidates)
            last_reported_candidates = discovered_candidates
            last_reported_at = now

        report_discovery(root, force=True)
        stack = [root]
        while stack:
            if self._cancel.is_set():
                raise IndexCancelled()
            directory = stack.pop()
            normalized_dir = self._normalize(directory)
            try:
                if self._is_excluded(directory):
                    clear_issue(normalized_dir)
                    stats.excluded += 1
                    continue
                # Keep DirEntry objects until type filtering is finished. On
                # Windows, FindFirstFile/FindNextFile already supplies most of
                # the metadata needed by is_dir()/is_file(), avoiding the extra
                # stat-family calls caused by turning entries into bare Paths
                # too early.
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
                clear_issue(normalized_dir)
                report_discovery(directory)
            except OSError as exc:
                stats.skipped += 1
                record_issue(normalized_dir, self._error_code(exc), str(exc))
                report_discovery(directory)
                continue

            for entry in entries:
                if self._cancel.is_set():
                    raise IndexCancelled()
                path = Path(entry.path)
                try:
                    if self._is_excluded(path):
                        clear_issue(self._normalize(path))
                        stats.excluded += 1
                        continue
                    if entry.is_dir():
                        if entry.name.casefold() not in self.ignored_dir_names:
                            stack.append(path)
                        continue
                    if not entry.is_file():
                        continue
                    if not self._is_supported_candidate(path):
                        continue
                    if self._is_file_pattern_excluded(path):
                        clear_issue(self._normalize(path))
                        stats.excluded += 1
                        continue
                    if normalized_candidates is not None:
                        normalized_candidates[str(path)] = self._normalize_discovered_candidate(
                            path,
                            normalized_directory=normalized_dir,
                            is_symlink=entry.is_symlink(),
                        )
                    discovered_candidates += 1
                    report_discovery(directory)
                    yield path
                except OSError as exc:
                    stats.skipped += 1
                    issue_path = self._normalize(path)
                    record_issue(issue_path, self._error_code(exc), str(exc))
                    continue

        report_discovery(root, force=True)
