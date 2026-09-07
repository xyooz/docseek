from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

from .chunk_store import ChunkStore


class _BorrowedConnection(AbstractContextManager[sqlite3.Connection]):
    """Yield a persistent connection without closing it at the end of a query."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


_registry_lock = threading.Lock()
_registered_stores: set[PersistentSearchStore] = set()


class PersistentSearchStore(ChunkStore):
    """Read-only ChunkStore view backed by one long-lived SQLite connection.

    Normal ``ChunkStore`` instances intentionally use short-lived connections
    because they also perform indexing writes and schema work. Interactive
    search has a different access pattern: many small read queries are issued
    while the user types. Re-running connection setup and per-connection
    PRAGMAs for every keystroke adds a measurable fixed latency on Windows.

    This class reuses one connection for the lifetime of a search worker
    thread. It deliberately skips ``ChunkStore.__init__`` because the main
    application has already initialized/migrated the database before search
    workers are started. ``PRAGMA query_only`` protects this session from
    accidental writes.

    Connections opt out of sqlite3's same-thread close restriction so the GUI
    can release worker-thread sessions during an orderly shutdown. Normal
    queries are still executed only by their worker thread and are serialized
    by ``SearchWorker``; cross-thread access is used for ``close()`` only after
    the search pool has finished its in-flight work.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.normalized_path = str(self.db_path.resolve())
        self._closed = False
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")
        self._connection.execute("PRAGMA temp_store=MEMORY")
        self._connection.execute("PRAGMA cache_size=-32768")
        self._connection.execute("PRAGMA busy_timeout=10000")
        with _registry_lock:
            _registered_stores.add(self)

    @property
    def closed(self) -> bool:
        return self._closed

    def connect(self) -> AbstractContextManager[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("persistent search store is closed")
        return _BorrowedConnection(self._connection)

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True
        with _registry_lock:
            _registered_stores.discard(self)


_thread_state = threading.local()


def get_thread_search_store(db_path: Path) -> PersistentSearchStore:
    """Return the persistent search store owned by the current worker thread."""
    normalized = str(Path(db_path).resolve())
    store = getattr(_thread_state, "store", None)
    store_path = getattr(_thread_state, "store_path", None)
    if store is not None and not store.closed and store_path == normalized:
        return store

    if store is not None:
        store.close()

    store = PersistentSearchStore(Path(db_path))
    _thread_state.store = store
    _thread_state.store_path = normalized
    return store


def close_thread_search_store() -> None:
    """Close the current thread's cached search connection."""
    store = getattr(_thread_state, "store", None)
    if store is not None:
        store.close()
    _thread_state.store = None
    _thread_state.store_path = None


def close_persistent_search_stores(db_path: Path | None = None) -> int:
    """Close cached search connections, including those owned by worker threads.

    Callers must first ensure no search query is currently using the target
    sessions (the desktop window does this with ``QThreadPool.waitForDone``).
    ``check_same_thread=False`` is intentionally used only to make this
    shutdown operation possible on Windows, where an otherwise-idle cached
    SQLite handle keeps the database file locked.
    """
    normalized = str(Path(db_path).resolve()) if db_path is not None else None
    with _registry_lock:
        stores = [
            store
            for store in _registered_stores
            if normalized is None or store.normalized_path == normalized
        ]

    for store in stores:
        store.close()
    return len(stores)
