from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from types import TracebackType
from typing import Self

from .chunk_store import ChunkStore
from .chunks import DocumentChunk


class ChunkBatchWriter:
    """Batch document index writes while preserving per-document rollback.

    FTS5 creates index segments at transaction boundaries. During a full scan,
    committing every file causes many small segments and many fsyncs. This
    writer keeps one explicit write transaction open across a bounded number
    of documents, but wraps each document in a SAVEPOINT so a broken parser or
    file cannot corrupt or roll back the rest of the batch.

    Watcher-driven single-file updates intentionally keep using
    ``ChunkStore.replace_document`` for simple per-file atomicity.
    """

    def __init__(self, store: ChunkStore, *, batch_size: int = 32) -> None:
        self.store = store
        self.batch_size = max(1, int(batch_size))
        self.conn: sqlite3.Connection | None = None
        self.pending_documents = 0
        self._savepoint_id = 0

    def __enter__(self) -> Self:
        if self.conn is not None:
            raise RuntimeError("ChunkBatchWriter is already open")
        self.conn = self.store.connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        conn = self.conn
        self.conn = None
        if conn is None:
            return False
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()
        return False

    def flush(self) -> None:
        conn = self._require_connection()
        if not self.pending_documents:
            return
        conn.commit()
        self.pending_documents = 0
        conn.execute("BEGIN IMMEDIATE")

    def replace_document(
        self,
        *,
        path: str,
        filename: str,
        extension: str,
        modified_time: float,
        size: int,
        chunks: Iterable[DocumentChunk],
    ) -> int:
        conn = self._require_connection()
        self._savepoint_id += 1
        savepoint = f"docseek_document_{self._savepoint_id}"
        conn.execute(f"SAVEPOINT {savepoint}")

        count = 0
        try:
            conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    extension=excluded.extension,
                    modified_time=excluded.modified_time,
                    size=excluded.size,
                    last_error=NULL
                """,
                (path, filename, extension, modified_time, size),
            )
            self.store._delete_chunks(conn, path)
            for chunk in chunks:
                cursor = conn.execute(
                    "INSERT INTO chunks(path, ordinal, location, content) VALUES (?, ?, ?, ?)",
                    (path, chunk.ordinal, chunk.location, chunk.content),
                )
                chunk_id = int(cursor.lastrowid)
                self.store._insert_fts_rows(conn, chunk_id, filename, chunk.content)
                count += 1
        except Exception:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")

        self.pending_documents += 1
        if self.pending_documents >= self.batch_size:
            self.flush()
        return count

    def _require_connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("ChunkBatchWriter must be used as a context manager")
        return self.conn
