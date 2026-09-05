from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


class ChunkBatchWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _broken_chunks():
        yield DocumentChunk(0, "块 1", "这部分不应该留下")
        raise ValueError("synthetic parser failure")

    def test_failed_document_rolls_back_without_losing_batch_neighbors(self) -> None:
        with ChunkBatchWriter(self.store, batch_size=32) as writer:
            writer.replace_document(
                path=r"C:\docs\before.txt",
                filename="before.txt",
                extension=".txt",
                modified_time=1.0,
                size=10,
                chunks=[DocumentChunk(0, "行 1", "批量写入之前 信贷")],
            )

            with self.assertRaises(ValueError):
                writer.replace_document(
                    path=r"C:\docs\broken.txt",
                    filename="broken.txt",
                    extension=".txt",
                    modified_time=2.0,
                    size=10,
                    chunks=self._broken_chunks(),
                )

            writer.replace_document(
                path=r"C:\docs\after.txt",
                filename="after.txt",
                extension=".txt",
                modified_time=3.0,
                size=10,
                chunks=[DocumentChunk(0, "行 1", "批量写入之后 客户经理")],
            )

        self.assertEqual([row.filename for row in self.store.search("信贷")], ["before.txt"])
        self.assertEqual([row.filename for row in self.store.search("客户经理")], ["after.txt"])
        self.assertEqual(self.store.search("不应该留下"), [])
        with self.store.connect() as conn:
            broken = conn.execute(
                "SELECT 1 FROM files WHERE path = ? LIMIT 1",
                (r"C:\docs\broken.txt",),
            ).fetchone()
        self.assertIsNone(broken)

    def test_extracted_text_budget_flushes_large_batch(self) -> None:
        path = r"C:\docs\large.txt"
        with ChunkBatchWriter(
            self.store,
            batch_size=128,
            max_batch_text_chars=5,
        ) as writer:
            writer.replace_document(
                path=path,
                filename="large.txt",
                extension=".txt",
                modified_time=1.0,
                size=2,
                chunks=[DocumentChunk(0, "行 1", "超过五个字符的正文")],
            )
            self.assertEqual(writer.pending_documents, 0)
            self.assertEqual(writer.pending_text_chars, 0)

            # A separate connection can see it immediately, proving the text
            # budget committed the batch instead of only resetting counters.
            with self.store.connect() as conn:
                visible = conn.execute(
                    "SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)
                ).fetchone()
            self.assertIsNotNone(visible)


if __name__ == "__main__":
    unittest.main()
