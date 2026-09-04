from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .extractors import SUPPORTED_EXTENSIONS, extract_text
from .search_db import SearchDatabase


@dataclass(slots=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0


class IndexCancelled(Exception):
    pass


class DirectoryIndexer:
    """Incremental local indexer.

    The indexer only reparses files whose size or modification time changed.
    Missing files under the indexed root are removed after a successful scan.
    """

    def __init__(
        self,
        database: SearchDatabase,
        *,
        max_file_size: int = 200 * 1024 * 1024,
    ) -> None:
        self.database = database
        self.max_file_size = max_file_size
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

        for path in self._iter_supported_files(root):
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
        self.database.save_index_root(str(root))
        return stats

    def _iter_supported_files(self, root: Path) -> Iterable[Path]:
        for path in root.rglob("*"):
            if self._cancel.is_set():
                raise IndexCancelled()
            try:
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    yield path
            except OSError:
                continue
