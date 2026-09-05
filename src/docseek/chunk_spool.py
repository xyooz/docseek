from __future__ import annotations

import pickle
import tempfile
from collections.abc import Iterable, Iterator

from .chunks import DocumentChunk


DEFAULT_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024


class DocumentChunkSpool:
    """Replay extracted chunks without keeping a slow parser inside a DB write.

    Compatibility parsers for legacy Office formats may need a long time to
    open or decode a workbook. The spool is filled *before* SQLite acquires its
    writer transaction, then replayed quickly into the index. Small documents
    stay in memory; larger extracted text automatically rolls to a temporary
    file managed by Python and is removed on close.
    """

    def __init__(self, *, max_memory_bytes: int = DEFAULT_SPOOL_MEMORY_BYTES) -> None:
        self._file = tempfile.SpooledTemporaryFile(
            max_size=max(1, int(max_memory_bytes)),
            mode="w+b",
        )
        self._captured = False

    def capture(self, chunks: Iterable[DocumentChunk]) -> int:
        if self._captured:
            raise RuntimeError("DocumentChunkSpool can capture only once")

        count = 0
        for chunk in chunks:
            pickle.dump(
                (int(chunk.ordinal), str(chunk.location), str(chunk.content)),
                self._file,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            count += 1
        self._file.flush()
        self._captured = True
        return count

    def iter_chunks(self) -> Iterator[DocumentChunk]:
        if not self._captured:
            raise RuntimeError("DocumentChunkSpool must be captured before replay")
        self._file.seek(0)
        while True:
            try:
                ordinal, location, content = pickle.load(self._file)
            except EOFError:
                break
            yield DocumentChunk(int(ordinal), str(location), str(content))

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "DocumentChunkSpool":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False
