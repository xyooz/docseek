from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .chunk_store import ChunkStore
from .chunks import iter_document_chunks
from .extractors import SUPPORTED_EXTENSIONS
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
        if max_file_size is None:
            max_file_size = database.get_max_file_size_mb() * 1024 * 1024
        self.max_file_size = max_file_size
        self.ignored_dir_names = {
            name.casefold() for name in (ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES)
        }
        raw_excluded = excluded_paths if excluded_paths is not None else database.get_excluded_paths()
        self.excluded_paths = [Path(path).resolve() for path in raw_excluded]
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _has_chunk_index(self, path: str) -> bool:
        with self.chunk_store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM chunk_fts WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, FileNotFoundError):
            return "file_not_found"
        if isinstance(exc, OSError):
            return "os_error"
        return type(exc).__name__

    def scan(
        self,
        root: Path,
        *,
        on_progress: Callable[[Path, IndexStats], None] | None = None,
    ) -> IndexStats:
        root = root.resolve()
        stats = IndexStats()
        seen_paths: set[str] = set()

        for path in self._iter_supported_files(root, stats):
            if self._cancel.is_set():
                raise IndexCancelled()

            stats.scanned += 1
            normalized = str(path.resolve())
            seen_paths.add(normalized)

            try:
                stat = path.stat()
                if stat.st_size > self.max_file_size:
                    stats.skipped += 1
                    actual_mb = stat.st_size / (1024 * 1024)
                    limit_mb = self.max_file_size / (1024 * 1024)
                    self.issues.record(
                        normalized,
                        "file_too_large",
                        f"文件大小 {actual_mb:.1f} MB，当前索引上限 {limit_mb:.0f} MB",
                    )
                    continue

                unchanged = self.database.is_unchanged(
                    normalized,
                    modified_time=stat.st_mtime,
                    size=stat.st_size,
                )
                # Existing pre-chunk databases must be migrated even when the
                # source file itself has not changed.
                if unchanged and self._has_chunk_index(normalized):
                    self.issues.clear(normalized)
                    stats.unchanged += 1
                    if on_progress and stats.scanned % 250 == 0:
                        on_progress(path, stats)
                    continue

                if on_progress:
                    on_progress(path, stats)

                chunk_count = self.chunk_store.replace_document(
                    path=normalized,
                    filename=path.name,
                    extension=path.suffix.lower(),
                    modified_time=stat.st_mtime,
                    size=stat.st_size,
                    chunks=iter_document_chunks(path),
                )
                self.issues.clear(normalized)
                stats.indexed += 1
                stats.chunks += chunk_count
            except IndexCancelled:
                raise
            except Exception as exc:
                stats.skipped += 1
                self.issues.record(normalized, self._error_code(exc), str(exc))

        stats.removed = self.chunk_store.remove_missing_under_root(str(root), seen_paths)
        self.issues.clear_under_root_if_missing(str(root), seen_paths)
        self.database.add_index_root(str(root))
        return stats

    def _is_excluded(self, path: Path) -> bool:
        resolved = path.resolve()
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
            normalized_dir = str(directory.resolve())
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
                        self.issues.clear(str(path.resolve()))
                        stats.excluded += 1
                        continue
                    if path.is_dir():
                        if path.name.casefold() not in self.ignored_dir_names:
                            stack.append(path)
                        continue
                    if not path.is_file():
                        continue
                    name = path.name
                    if name.startswith("~$") or name.endswith(".tmp"):
                        continue
                    if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                        yield path
                except OSError as exc:
                    stats.skipped += 1
                    try:
                        issue_path = str(path.resolve())
                    except OSError:
                        issue_path = str(path)
                    self.issues.record(issue_path, self._error_code(exc), str(exc))
                    continue
