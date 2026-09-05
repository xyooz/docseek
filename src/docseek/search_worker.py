from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, Signal

from .chunk_store import ChunkStore, SearchPage
from .exact_search import ExactGroupedSearchEngine
from .search_session import get_thread_search_store
from .search_sort import SORT_RELEVANCE


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
    sort_mode: str = SORT_RELEVANCE
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


_query_lock = threading.Lock()
_generation_lock = threading.Lock()
_latest_generations: weakref.WeakKeyDictionary[ChunkStore, int] = weakref.WeakKeyDictionary()


def _publish_generation(store: ChunkStore, generation: int) -> None:
    with _generation_lock:
        current = _latest_generations.get(store, -1)
        if generation > current:
            _latest_generations[store] = generation


def _is_stale(store: ChunkStore, generation: int) -> bool:
    with _generation_lock:
        return generation < _latest_generations.get(store, generation)


class SearchWorker(QRunnable):
    """Execute one exact grouped search page without blocking the GUI thread.

    The first query on a worker thread creates a read-only persistent SQLite
    session. Later queries reuse that connection and its page cache instead of
    paying connection/PRAGMA setup on every keystroke.

    Interactive searches use ``ExactGroupedSearchEngine`` so file-level metadata
    work stays after chunk collapse and explicit structure hints can influence
    the best chunk without changing the persisted index schema.

    Searches are serialized. When the user keeps typing, a newer generation is
    published for that window's ChunkStore immediately. Older queued workers
    check the value again after acquiring the query lock and become no-ops
    instead of running stale FTS statements.
    """

    def __init__(self, store: ChunkStore, request: SearchRequest) -> None:
        super().__init__()
        self.store = store
        self.request = request
        self.signals = SearchWorkerSignals()
        _publish_generation(store, request.generation)

    def _emit_stale(self, started: float) -> None:
        self.signals.finished.emit(
            SearchResponse(
                request=self.request,
                page=SearchPage(items=[], total_count=0),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        )

    def run(self) -> None:
        started = time.perf_counter()
        if _is_stale(self.store, self.request.generation):
            self._emit_stale(started)
            return

        with _query_lock:
            # Another request for the same window may have been created while
            # this worker waited for the previous exact query to finish.
            if _is_stale(self.store, self.request.generation):
                self._emit_stale(started)
                return

            try:
                search_store = get_thread_search_store(self.store.db_path)
                engine = ExactGroupedSearchEngine(search_store)
                page = engine.search_page(
                    self.request.query,
                    limit=self.request.limit,
                    offset=self.request.offset,
                    extension=self.request.extension,
                    path_contains=self.request.path_contains,
                    modified_after=self.request.modified_after,
                    modified_before=self.request.modified_before,
                    min_size=self.request.min_size,
                    max_size=self.request.max_size,
                    sort_mode=self.request.sort_mode,
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
