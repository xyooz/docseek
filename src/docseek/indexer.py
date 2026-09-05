from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .chunk_store import ChunkStore
from .chunk_writer import ChunkBatchWriter
from .chunks import iter_document_chunks
from .extraction_revision import current_extraction_revision
from .extractors import SUPPORTED_EXTENSIONS
from .index_cleanup import remove_missing_under_root
from .index_issues import IndexIssueStore
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


@dataclass(slots=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    chunks: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0
    excluded: int = 0

    def merge(self, other: "IndexStats") -> None:
        self.scanned += other.scanned
        self.indexed += other.indexed
        self.chunks += other.chunks
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        self.removed += other.removed
        self.excluded += other.excluded


class IndexCancelled(Exception):
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
    ) -> None:
        self.database = database
        self.chunk_store = ChunkStore(database.db_path)
        self.issues = IndexIssueStore(database.db_path)

        configured_max_mb, configured_excluded = self._load_runtime_settings()
        if max_file_size is None:
            max_file_size = configured_max_mb * 1024 * 1024
        self.max_file_size = max_file_size
        self.ignored_dir_names = {
            name.casefold() for name in (ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES)
        }
        raw_excluded = excluded_paths if excluded_paths is not None else configured_excluded
        self.excluded_paths = [Path(path).resolve() for path in raw_excluded]
        self._cancel = threading.Event()

    def _load_runtime_settings(self) -> tuple[int, list[str]]:
        """Read indexer settings with one lightweight metadata connection.

        The legacy SearchDatabase connection still owns compatibility FTS
        tables and historically executed journal-mode setup on every connect.
        Indexing is latency-sensitive, so runtime settings are read directly
        from the shared metadata table instead of paying those legacy costs for
        every watcher batch.
        """
        with self.chunk_store.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('max_file_size_mb', 'excluded_paths')"
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
        return max_mb, excluded

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _normalize(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

    def _has_chunk_index(self, path: str) -> bool:
        with self.chunk_store.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM files f
                JOIN chunks c ON c.file_id = f.id
                WHERE f.path = ?
                LIMIT 1
                """,
                (path,),
            ).fetchone()
        return row is not None

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
                    COALESCE(s.revision, 0) AS extraction_revision
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
        )

    def _load_index_state(self) -> dict[str, tuple[float, int, bool, int]]:
        """Load metadata/chunk presence/revision once for a reconciliation scan."""
        with self.chunk_store.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.path,
                    f.modified_time,
                    f.size,
                    EXISTS(
                        SELECT 1 FROM chunks c
                        WHERE c.file_id = f.id
                    ) AS has_chunk,
                    COALESCE(s.revision, 0) AS extraction_revision
                FROM files f
                LEFT JOIN extraction_state s ON s.path = f.path
                """
            ).fetchall()

        return {
            str(row["path"]): (
                float(row["modified_time"]),
                int(row["size"]),
                bool(row["has_chunk"]),
                int(row["extraction_revision"]),
            )
            for row in rows
        }

    def _record_extraction_revision(self, path: str, revision: int) -> None:
        with self.chunk_store.connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_state(path, revision)
                VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET revision=excluded.revision
                """,
                (path, int(revision)),
            )

    def _remove_indexed_path(self, path: str, stats: IndexStats) -> None:
        if self._has_file_record(path):
            self.chunk_store.remove_document(path)
            stats.removed += 1

    @staticmethod
    def _error_code(exc: Exception) -> str:
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

    def _index_existing_file(
        self,
        path: Path,
        normalized: str,
        stats: IndexStats,
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
        on_detail: Callable[[Path, str, int], None] | None = None,
        prefetched_state: tuple[float, int, bool, int] | None = None,
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
            unchanged = (
                prefetched_state is not None
                and float(prefetched_state[0]) == float(stat.st_mtime)
                and int(prefetched_state[1]) == int(stat.st_size)
                and int(prefetched_state[3]) >= extraction_revision
            )
            has_chunk = bool(prefetched_state[2]) if prefetched_state is not None else False
        else:
            unchanged = self._is_unchanged(
                normalized,
                modified_time=stat.st_mtime,
                size=stat.st_size,
                extraction_revision=extraction_revision,
            )
            has_chunk = self._has_chunk_index(normalized) if unchanged else False

        if unchanged and has_chunk:
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
                # The current desktop worker already transports file progress
                # as a Path plus counters. Preserve that compatibility while
                # exposing useful row-level XLSX progress immediately; a future
                # UI can opt into the structured on_detail callback directly.
                display = Path(f"{path.name} · {location} · 已读取 {current:,} 行")
                on_progress(display, stats)

        common_args = {
            "path": normalized,
            "filename": path.name,
            "extension": path.suffix.lower(),
            "modified_time": stat.st_mtime,
            "size": stat.st_size,
            "chunks": iter_document_chunks(
                path,
                on_progress=report_detail
                if on_detail or on_progress or path.suffix.lower() == ".xlsx"
                else None,
            ),
        }
        if writer is not None:
            chunk_count = writer.replace_document(
                **common_args,
                extraction_revision=extraction_revision,
            )
        else:
            chunk_count = self.chunk_store.replace_document(**common_args)
            self._record_extraction_revision(normalized, extraction_revision)

        if clear_issue:
            self.issues.clear(normalized)
        stats.indexed += 1
        stats.chunks += chunk_count
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
            except FileNotFoundError:
                self._remove_indexed_path(normalized, stats)
                self.issues.clear(normalized)
            except Exception as exc:
                stats.skipped += 1
                self.issues.record(normalized, self._error_code(exc), str(exc))

        return stats

    def scan(
        self,
        root: Path,
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
        on_detail: Callable[[Path, str, int], None] | None = None,
    ) -> IndexStats:
        root = root.resolve()
        stats = IndexStats()
        seen_paths: set[str] = set()
        successful_paths: set[str] = set()
        index_state = self._load_index_state()

        with ChunkBatchWriter(
            self.chunk_store,
            batch_size=FULL_SCAN_BATCH_SIZE,
            max_batch_text_chars=FULL_SCAN_BATCH_TEXT_CHARS,
        ) as writer:
            for path in self._iter_supported_files(root, stats):
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
                except Exception as exc:
                    writer.flush()
                    stats.skipped += 1
                    self.issues.record(normalized, self._error_code(exc), str(exc))

        self.issues.clear_many(successful_paths)
        stats.removed += remove_missing_under_root(self.chunk_store, str(root), seen_paths)
        self.issues.clear_under_root_if_missing(str(root), seen_paths)
        self.database.add_index_root(str(root))
        return stats

    def _is_excluded(self, path: Path) -> bool:
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

    def _iter_supported_files(self, root: Path, stats: IndexStats) -> Iterable[Path]:
        stack = [root]
        while stack:
            if self._cancel.is_set():
                raise IndexCancelled()
            directory = stack.pop()
            normalized_dir = self._normalize(directory)
            try:
                if self._is_excluded(directory):
                    self.issues.clear(normalized_dir)
                    stats.excluded += 1
                    continue
                entries = list(directory.iterdir())
                self.issues.clear(normalized_dir)
            except OSError as exc:
                stats.skipped += 1
                self.issues.record(normalized_dir, self._error_code(exc), str(exc))
                continue

            for path in entries:
                if self._cancel.is_set():
                    raise IndexCancelled()
                try:
                    if self._is_excluded(path):
                        self.issues.clear(self._normalize(path))
                        stats.excluded += 1
                        continue
                    if path.is_dir():
                        if path.name.casefold() not in self.ignored_dir_names:
                            stack.append(path)
                        continue
                    if not path.is_file():
                        continue
                    if self._is_supported_candidate(path):
                        yield path
                except OSError as exc:
                    stats.skipped += 1
                    issue_path = self._normalize(path)
                    self.issues.record(issue_path, self._error_code(exc), str(exc))
                    continue
