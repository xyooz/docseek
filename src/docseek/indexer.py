from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .extractors import SUPPORTED_EXTENSIONS, extract_text
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
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0
    excluded: int = 0

    def merge(self, other: "IndexStats") -> None:
        self.scanned += other.scanned
        self.indexed += other.indexed
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        self.removed += other.removed
        self.excluded += other.excluded


class IndexCancelled(Exception):
    pass


class DirectoryIndexer:
    """Incremental local indexer tuned for ordinary office folders."""

    def __init__(
        self,
        database: SearchDatabase,
        *,
        max_file_size: int | None = None,
        ignored_dir_names: set[str] | None = None,
        excluded_paths: list[str] | None = None,
    ) -> None:
        self.database = database
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
                    self.database.record_index_error(normalized, "file_too_large")
                    continue

                if self.database.is_unchanged(
                    normalized,
                    modified_time=stat.st_mtime,
                    size=stat.st_size,
                ):
                    stats.unchanged += 1
                    if on_progress and stats.scanned % 250 == 0:
                        on_progress(path, stats)
                    continue

                if on_progress:
                    on_progress(path, stats)

                content = extract_text(path)
                self.database.upsert_document(
                    path=normalized,
                    filename=path.name,
                    extension=path.suffix.lower(),
                    modified_time=stat.st_mtime,
                    size=stat.st_size,
                    content=content,
                )
                stats.indexed += 1
            except IndexCancelled:
                raise
            except Exception as exc:
                stats.skipped += 1
                self.database.record_index_error(normalized, type(exc).__name__)

        stats.removed = self.database.remove_missing_under_root(str(root), seen_paths)
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
            try:
                if self._is_excluded(directory):
                    stats.excluded += 1
                    continue
                entries = list(directory.iterdir())
            except OSError:
                continue

            for path in entries:
                if self._cancel.is_set():
                    raise IndexCancelled()
                try:
                    if self._is_excluded(path):
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
                except OSError:
                    continue
