from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from types import TracebackType
from typing import Self

from .chunk_codec import encode_chunk_content
from .chunk_store import ChunkStore
from .chunks import DocumentChunk


class ChunkBatchWriter:
    """Batch document index writes while preserving per-document rollback.

    FTS5 creates index segments at transaction boundaries. During a full scan,
    committing every file causes many small segments and many fsyncs. This
    writer keeps one explicit write transaction open across a bounded number
    of changed documents, but wraps each document in a SAVEPOINT so a broken
    parser or file cannot corrupt or roll back the rest of the batch.

    Batches are bounded by both document count and extracted-text characters.
    The latter matters for compressed Office files whose on-disk size can be
    much smaller than the text written into the two FTS indexes. A single large
    document remains atomic, but it is committed before another document is
    added once the text budget has been crossed.

    The write transaction is acquired lazily. An unchanged-only reconciliation
    scan therefore does not hold SQLite's writer lock for the duration of the
    filesystem walk. Watcher-driven single-file updates intentionally keep
    using ``ChunkStore.replace_document`` for simple per-file atomicity.
    """

    def __init__(
        self,
        store: ChunkStore,
        *,
        batch_size: int = 32,
        max_batch_text_chars: int | None = 8_000_000,
    ) -> None:
        self.store = store
        self.batch_size = max(1, int(batch_size))
        self.max_batch_text_chars = (
            None
            if max_batch_text_chars is None
            else max(1, int(max_batch_text_chars))
        )
        self.conn: sqlite3.Connection | None = None
        self.pending_documents = 0
        self.pending_text_chars = 0
        self._savepoint_id = 0
        self._transaction_open = False

    def __enter__(self) -> Self:
        if self.conn is not None:
            raise RuntimeError("ChunkBatchWriter is already open")
        self.conn = self.store.connect()
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
            if self._transaction_open:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
        finally:
            self._transaction_open = False
            self.pending_documents = 0
            self.pending_text_chars = 0
            conn.close()
        return False

    def flush(self) -> None:
        """Commit the current batch and release the SQLite writer lock."""
        conn = self._require_connection()
        if self._transaction_open:
            conn.commit()
            self._transaction_open = False
        self.pending_documents = 0
        self.pending_text_chars = 0

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
        self._ensure_transaction(conn)
        self._savepoint_id += 1
        savepoint = f"docseek_document_{self._savepoint_id}"
        conn.execute(f"SAVEPOINT {savepoint}")

        count = 0
        document_text_chars = 0
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
            file_id = self.store._file_id_for_path(conn, path)
            if file_id is None:
                raise RuntimeError(f"无法为索引文件分配 file_id：{path}")

            self.store._delete_chunks(conn, path, file_id=file_id)
            for chunk in chunks:
                document_text_chars += len(chunk.content)
                cursor = conn.execute(
                    """
                    INSERT INTO chunks(file_id, ordinal, location, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        chunk.ordinal,
                        chunk.location,
                        encode_chunk_content(chunk.content),
                    ),
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
        self.pending_text_chars += document_text_chars
        hit_document_limit = self.pending_documents >= self.batch_size
        hit_text_limit = (
            self.max_batch_text_chars is not None
            and self.pending_text_chars >= self.max_batch_text_chars
        )
        if hit_document_limit or hit_text_limit:
            self.flush()
        return count

    def _ensure_transaction(self, conn: sqlite3.Connection) -> None:
        if not self._transaction_open:
            conn.execute("BEGIN IMMEDIATE")
            self._transaction_open = True

    def _require_connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("ChunkBatchWriter must be used as a context manager")
        return self.conn
