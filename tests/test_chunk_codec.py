from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_codec import (
    decode_chunk_content,
    encode_chunk_content,
    is_compressed_chunk_content,
)
from docseek.chunk_store import ChunkStore
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.schema import CURRENT_SCHEMA_VERSION
from docseek.search_db import SearchDatabase


class ChunkCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_codec_reads_legacy_text_and_versioned_blob(self) -> None:
        text = "客户经理办理信贷业务，身份证有效期需要更新。"
        encoded = encode_chunk_content(text)

        self.assertTrue(is_compressed_chunk_content(encoded))
        self.assertEqual(decode_chunk_content(encoded), text)
        self.assertEqual(decode_chunk_content(memoryview(encoded)), text)
        self.assertEqual(decode_chunk_content(text), text)

    def test_corrupt_versioned_blob_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_chunk_content(b"DSZ1not-a-zlib-stream")

    def test_single_document_write_is_compressed_and_replace_keeps_fts_correct(self) -> None:
        path = r"C:\docs\credit.pdf"
        self.store.replace_document(
            path=path,
            filename="credit.pdf",
            extension=".pdf",
            modified_time=1.0,
            size=100,
            chunks=[DocumentChunk(0, "第 1 页", "客户经理办理信贷业务")],
        )

        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT typeof(c.content) AS storage_type, c.content
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE f.path = ?
                """,
                (path,),
            ).fetchone()
        self.assertEqual(str(row["storage_type"]), "blob")
        self.assertTrue(is_compressed_chunk_content(row["content"]))
        self.assertEqual(decode_chunk_content(row["content"]), "客户经理办理信贷业务")
        self.assertIn("[[HIT]]信贷[[/HIT]]", self.store.search("信贷")[0].snippet)

        # Replacing a compressed document exercises contentless-FTS deletion:
        # DocSeek must decode the old raw copy before issuing the FTS5 delete.
        self.store.replace_document(
            path=path,
            filename="credit.pdf",
            extension=".pdf",
            modified_time=2.0,
            size=120,
            chunks=[DocumentChunk(0, "第 1 页", "跨境业务操作说明")],
        )
        self.assertEqual(self.store.search("信贷"), [])
        self.assertEqual([row.filename for row in self.store.search("跨境业务")], ["credit.pdf"])

        self.store.remove_document(path)
        self.assertEqual(self.store.search("跨境业务"), [])

    def test_batch_writer_stores_compressed_raw_copy(self) -> None:
        path = r"C:\docs\batch.txt"
        with ChunkBatchWriter(self.store, batch_size=32) as writer:
            writer.replace_document(
                path=path,
                filename="batch.txt",
                extension=".txt",
                modified_time=1.0,
                size=100,
                chunks=[DocumentChunk(0, "行 1", "批量索引客户经理")],
            )

        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT typeof(c.content) AS storage_type, c.content
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE f.path = ?
                """,
                (path,),
            ).fetchone()
        self.assertEqual(str(row["storage_type"]), "blob")
        self.assertEqual(decode_chunk_content(row["content"]), "批量索引客户经理")
        self.assertEqual([row.filename for row in self.store.search("客户经理")], ["batch.txt"])

    def test_v6_text_rows_upgrade_logically_without_bulk_rewrite(self) -> None:
        path = r"C:\docs\legacy.pdf"
        content = "旧版索引仍然可以检索客户经理信贷内容"
        self.store.replace_document(
            path=path,
            filename="legacy.pdf",
            extension=".pdf",
            modified_time=1.0,
            size=100,
            chunks=[DocumentChunk(0, "第 1 页", content)],
        )

        # Simulate the on-disk state of a v6 database: raw chunk content was
        # plain TEXT, while the two contentless FTS indexes already existed.
        # ChunkStore.connect() closes the handle on context-manager exit, which
        # matters on Windows where an open sqlite handle locks temp-file cleanup.
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE chunks SET content = ?
                WHERE file_id = (SELECT id FROM files WHERE path = ?)
                """,
                (content, path),
            )
            conn.execute("PRAGMA user_version = 6")

        reopened = ChunkStore(self.db_path)
        with reopened.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            row = conn.execute(
                """
                SELECT typeof(c.content) AS storage_type, c.content
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE f.path = ?
                """,
                (path,),
            ).fetchone()

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(str(row["storage_type"]), "text")
        self.assertEqual(decode_chunk_content(row["content"]), content)
        results = reopened.search("信贷")
        self.assertEqual([row.filename for row in results], ["legacy.pdf"])
        self.assertIn("[[HIT]]信贷[[/HIT]]", results[0].snippet)


if __name__ == "__main__":
    unittest.main()
