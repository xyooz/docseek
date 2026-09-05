from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, Signal

from .chunk_store import ChunkStore, SearchPage


@dataclass(frozen=True, slots=True)
class SearchRequest:
    generation: int
    query: str
    limit: int
    offset: int
    extension: str | None = None
    path_contains: str | None = None
    modified_after: float | None = None
    modified_before: float | None = None
    min_size: int | None = None
    max_size: int | None = None
    select_first: bool = False
    is_filter_only: bool = False


@dataclass(slots=True)
class SearchResponse:
    request: SearchRequest
    page: SearchPage
    elapsed_ms: float


class SearchWorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(int, str)


class SearchWorker(QRunnable):
    """Execute one exact search page on a background Qt thread.

    Cancellation is intentionally generation-based: SQLite finishes an already
    running statement, but the UI ignores any response whose generation is no
    longer current. This keeps the exact search implementation simple while
    preventing stale results from replacing a newer query.
    """

    def __init__(self, store: ChunkStore, request: SearchRequest) -> None:
        super().__init__()
        self.store = store
        self.request = request
        self.signals = SearchWorkerSignals()

    def run(self) -> None:
        started = time.perf_counter()
        try:
            page = self.store.search_page(
                self.request.query,
                limit=self.request.limit,
                offset=self.request.offset,
                extension=self.request.extension,
                path_contains=self.request.path_contains,
                modified_after=self.request.modified_after,
                modified_before=self.request.modified_before,
                min_size=self.request.min_size,
                max_size=self.request.max_size,
            )
        except Exception as exc:
            self.signals.failed.emit(self.request.generation, str(exc))
            return

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.signals.finished.emit(
            SearchResponse(
                request=self.request,
                page=page,
                elapsed_ms=elapsed_ms,
            )
        )
