from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .chunk_store import ChunkStore
from .chunk_writer import ChunkBatchWriter
from .chunks import iter_document_chunks
from .extraction_revision import current_extraction_revision
from .extraction_state import (
    ExtractionStatus,
    failed_state_is_deferred,
    retry_delay_seconds,
    status_for_error_code,
    status_for_extraction_result,
    unchanged_state_is_complete,
)
from .extractors import SUPPORTED_EXTENSIONS
from .file_exclusions import (
    FILE_EXCLUSION_PATTERNS_KEY,
    decode_file_exclusion_patterns,
    matches_file_exclusion,
    normalize_file_exclusion_patterns,
)
from .index_cleanup import remove_missing_under_root
from .index_health import record_successful_reconcile
from .index_issues import IndexIssueStore
from .index_priority import prioritize_index_candidates
from .search_db import SearchDatabase


DEFAULT_IGNORED_DIR_NAMES = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    "$recycle.bin",
    "system volume information",
}
FULL_SCAN_BATCH_SIZE = 128
FULL_SCAN_BATCH_TEXT_CHARS = 8_000_000
DISCOVERY_PROGRESS_INTERVAL_SECONDS = 0.15
DISCOVERY_PROGRESS_CANDIDATE_STEP = 250


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
    ) -> None:
        self.database = database
        self.chunk_store = ChunkStore(database.db_path)
        self.issues = IndexIssueStore(database.db_path)

        (
            configured_max_mb,
            configured_excluded,
            configured_file_patterns,
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
        self._cancel = threading.Event()

    def _load_runtime_settings(self) -> tuple[int, list[str], list[str]]:
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
                "WHERE key IN ('max_file_size_mb', 'excluded_paths', ?) ",
                (FILE_EXCLUSION_PATTERNS_KEY,),
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
        return max_mb, excluded, file_patterns

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _normalize(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

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
                INSERT INTO extraction_state(path, revision, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (path, int(revision), str(status), time.time()),
            )

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
            retry_after = now + retry_delay_seconds(status, failure_count)
            conn.execute(
                """
                INSERT INTO extraction_state(
                    path, revision, status, updated_at,
                    source_modified_time, source_size, failure_count, retry_after
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    revision=excluded.revision,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    source_modified_time=excluded.source_modified_time,
                    source_size=excluded.source_size,
                    failure_count=excluded.failure_count,
                    retry_after=excluded.retry_after
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
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, FileNotFoundError):
            return "file_not_found"
        if isinstance(exc, OSError):
            return "os_error"
        return type(exc).__name__

    @staticmethod
    def _is_supported_candidate(path: Path) -> bool:
        name = path.name
        return (
            not name.startswith("~$")
            and not name.endswith(".tmp")
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
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
        stats = IndexStats()
        seen_paths: set[str] = set()
        successful_paths: set[str] = set()
        index_state = self._load_index_state()

        discovery_issue_clears: set[str] = set()
        discovery_issue_records: list[tuple[str, str, str]] = []

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

        candidates = list(
            prioritize_index_candidates(
                self._iter_supported_files(
                    root,
                    stats,
                    issue_clears=discovery_issue_clears,
                    issue_records=discovery_issue_records,
                    on_discovery=discovery_callback,
                )
            )
        )
        if on_candidates_ready:
            on_candidates_ready(len(candidates))
        elif on_progress:
            on_progress(
                Path(f"扫描完成 · 共发现 {len(candidates):,} 个候选文件"),
                stats,
            )

        # Discovery stays outside the batched chunk writer. Healthy directory
        # traversal therefore performs no SQLite writes, while issue cleanup is
        # collapsed into at most two short metadata transactions before content
        # indexing starts. This preserves the single-writer invariant that fixed
        # the real Windows ``database is locked`` reports without paying one
        # connection/commit per visited directory.
        self.issues.clear_many(discovery_issue_clears)
        self.issues.record_many(discovery_issue_records)

        with ChunkBatchWriter(
            self.chunk_store,
            batch_size=FULL_SCAN_BATCH_SIZE,
            max_batch_text_chars=FULL_SCAN_BATCH_TEXT_CHARS,
        ) as writer:
            for path in candidates:
                if self._cancel.is_set():
                    writer.flush()
                    raise IndexCancelled()

                stats.scanned += 1
                normalized = self._normalize(path)
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
                    if on_progress and stats.scanned % 250 == 0:
                        on_progress(path, stats)
                except IndexCancelled:
                    writer.flush()
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
                    writer.flush()
                    stats.skipped += 1
                    error_code = self._error_code(exc)
                    self._record_failure_state(normalized, error_code)
                    self.issues.record(normalized, error_code, str(exc))

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
                    discovered_candidates += 1
                    report_discovery(directory)
                    yield path
                except OSError as exc:
                    stats.skipped += 1
                    issue_path = self._normalize(path)
                    record_issue(issue_path, self._error_code(exc), str(exc))
                    continue

        report_discovery(root, force=True)
