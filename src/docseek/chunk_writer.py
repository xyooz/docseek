from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Callable
from types import TracebackType
from typing import Self

from .chunk_codec import encode_chunk_content
from .chunk_spool import DocumentChunkSpool
from .chunk_store import ChunkStore
from .chunks import DocumentChunk
from .document_types import DIRECT_SUPPORTED_EXTENSIONS
from .extraction_state import status_for_extraction_result


# Specialized parsers for modern Office/PDF formats are normally reliable, but
# they can still spend seconds decoding a large workbook or document. Never do
# that filesystem/parser work while SQLite's single WAL writer slot is held.
_SPOOL_BEFORE_WRITE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})

# Document SAVEPOINTs are strictly sequential and never nested. Reuse one fixed
# name so sqlite3 can reuse the same prepared statements instead of parsing a
# unique SAVEPOINT/RELEASE pair for every file in a large full scan. The
# per-document rollback boundary is unchanged.
_DOCUMENT_SAVEPOINT = "docseek_document"
_SAVEPOINT_SQL = f"SAVEPOINT {_DOCUMENT_SAVEPOINT}"
_ROLLBACK_TO_SAVEPOINT_SQL = f"ROLLBACK TO {_DOCUMENT_SAVEPOINT}"
_RELEASE_SAVEPOINT_SQL = f"RELEASE {_DOCUMENT_SAVEPOINT}"


class ChunkBatchWriter:
    """Batch document index writes while preserving per-document rollback.

    FTS5 creates index segments at transaction boundaries. During a full scan,
    committing every small text file causes many small segments and many fsyncs.
    This writer therefore batches cheap text-document writes, but slow Office,
    PDF and compatibility parsing is first captured into a bounded spool before
    SQLite acquires its writer transaction.

    Each database mutation is wrapped in a SAVEPOINT so a broken file cannot
    corrupt or roll back already committed neighbors. Batches are bounded by
    both document count and extracted-text characters.

    A single large document remains atomic. Modern Office/PDF and compatibility
    documents are committed immediately after replay so the writer lock is not
    left open while the next document is decoded. Small spool payloads stay in
    memory; larger ones automatically roll to a temporary file.
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

    def discard_pending(self) -> None:
        """Rollback the current in-memory batch without touching committed work.

        Cancellation is a control-flow decision, not a successful end of a
        scan.  Committing here can make a stop request wait on a large SQLite
        flush and can expose a half-finished batch.  The next scan can safely
        rebuild anything that was not committed before cancellation.
        """
        conn = self._require_connection()
        if self._transaction_open:
            conn.rollback()
            self._transaction_open = False
        self.pending_documents = 0
        self.pending_text_chars = 0

    # Short internal spelling for call sites handling a cancellation path.
    def abort(self) -> None:
        self.discard_pending()

    def replace_document(
        self,
        *,
        path: str,
        filename: str,
        extension: str,
        modified_time: float,
        size: int,
        chunks: Iterable[DocumentChunk],
        extraction_revision: int | None = None,
        validate_source: Callable[[], None] | None = None,
    ) -> int:
        normalized_extension = extension.lower()
        compatibility_format = normalized_extension not in DIRECT_SUPPORTED_EXTENSIONS
        spool_before_write = (
            compatibility_format
            or normalized_extension in _SPOOL_BEFORE_WRITE_EXTENSIONS
        )
        spool: DocumentChunkSpool | None = None

        try:
            if spool_before_write:
                # The preceding small-text batch may still own SQLite's writer
                # slot. Commit it *before* consuming an Office/PDF/legacy parser
                # so slow filesystem/decoder work cannot block unrelated UI
                # metadata writes such as search history or saved settings.
                self.flush()
                spool = DocumentChunkSpool()
                spool.capture(chunks)
                chunks = spool.iter_chunks()

            conn = self._require_connection()
            # Spool-backed formats finish filesystem/parser work before SQLite
            # mutation begins, so validate that captured payload once before the
            # write and once again after replay. Cheap text formats are consumed
            # lazily inside the SAVEPOINT; their final validation already covers
            # the entire interval from DirectoryIndexer's initial stat through
            # parsing, making an extra pre-parse stat redundant.
            if spool_before_write and validate_source is not None:
                validate_source()
            self._ensure_transaction(conn)
            conn.execute(_SAVEPOINT_SQL)

            count = 0
            document_text_chars = 0
            try:
                # Most first-index work is genuinely new. Insert it without an
                # update branch so SQLite tells us through rowcount whether the
                # path already existed. New rows can use lastrowid immediately
                # and skip both the file-id SELECT and the otherwise pointless
                # stale-chunk SELECT/DELETE. Existing rows keep the established
                # atomic replacement path below.
                insert_cursor = conn.execute(
                    """
                    INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(path) DO NOTHING
                    """,
                    (path, filename, extension, modified_time, size),
                )
                if insert_cursor.rowcount == 1:
                    file_id = int(insert_cursor.lastrowid)
                else:
                    conn.execute(
                        """
                        UPDATE files
                        SET filename = ?, extension = ?, modified_time = ?,
                            size = ?, last_error = NULL
                        WHERE path = ?
                        """,
                        (filename, extension, modified_time, size, path),
                    )
                    file_id = self.store._file_id_for_path(conn, path)
                    if file_id is None:
                        raise RuntimeError(f"无法为索引文件分配 file_id：{path}")
                    self.store._delete_chunks(conn, path, file_id=file_id)

                # The filename is identical for every chunk in this document.
                # Tokenize it once instead of once per PDF page / Excel chunk.
                filename_tokens = self.store._cjk_bigrams(filename)
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
                    from .structure_store import write_structure
                    write_structure(conn, chunk_id, path, extension, chunk)
                    conn.execute(
                        "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
                        (chunk_id, filename, chunk.content),
                    )
                    conn.execute(
                        """
                        INSERT INTO chunk_index_cjk2(
                            rowid, filename_tokens, content_tokens
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            chunk_id,
                            filename_tokens,
                            self.store._cjk_bigrams(chunk.content),
                        ),
                    )
                    count += 1

                if validate_source is not None:
                    validate_source()
                if extraction_revision is not None:
                    status = status_for_extraction_result(extension, count)
                    conn.execute(
                        """
                        INSERT INTO extraction_state(
                            path, revision, status, updated_at, owner_pid, started_at
                        )
                        VALUES (?, ?, ?, ?, NULL, NULL)
                        ON CONFLICT(path) DO UPDATE SET
                            revision=excluded.revision,
                            status=excluded.status,
                            updated_at=excluded.updated_at,
                            owner_pid=NULL,
                            started_at=NULL,
                            retry_after=0
                        """,
                        (path, int(extraction_revision), str(status), time.time()),
                    )
            except Exception:
                conn.execute(_ROLLBACK_TO_SAVEPOINT_SQL)
                conn.execute(_RELEASE_SAVEPOINT_SQL)
                raise
            else:
                conn.execute(_RELEASE_SAVEPOINT_SQL)

            self.pending_documents += 1
            self.pending_text_chars += document_text_chars
            hit_document_limit = self.pending_documents >= self.batch_size
            hit_text_limit = (
                self.max_batch_text_chars is not None
                and self.pending_text_chars >= self.max_batch_text_chars
            )
            if spool_before_write or hit_document_limit or hit_text_limit:
                # Spool-backed documents are committed immediately so they do
                # not leave a writer lock behind while the next file is parsed.
                self.flush()
            return count
        finally:
            if spool is not None:
                spool.close()

    def _ensure_transaction(self, conn: sqlite3.Connection) -> None:
        if not self._transaction_open:
            conn.execute("BEGIN IMMEDIATE")
            self._transaction_open = True

    def _require_connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("ChunkBatchWriter must be used as a context manager")
        return self.conn
