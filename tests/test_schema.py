from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.schema import CURRENT_SCHEMA_VERSION, UnsupportedSchemaVersion
from docseek.search_db import SearchDatabase


class SchemaVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_chunk_store_marks_fresh_database_current(self) -> None:
        SearchDatabase(self.db_path)
        ChunkStore(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    def test_v1_index_is_migrated_without_reparsing_documents(self) -> None:
        SearchDatabase(self.db_path)
        store = ChunkStore(self.db_path)
        store.replace_document(
            path=r"C:\\docs\\credit.pdf",
            filename="credit.pdf",
            extension=".pdf",
            modified_time=1.0,
            size=123,
            chunks=[DocumentChunk(0, "第 1 页", "客户经理办理信贷业务")],
        )

        # Simulate the v1 layout: FTS content exists but the rowid lookup did not.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM chunk_lookup")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

        migrated = ChunkStore(self.db_path)
        with migrated.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            lookup_count = int(conn.execute("SELECT COUNT(*) FROM chunk_lookup").fetchone()[0])

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(lookup_count, 1)
        rows = migrated.search("信贷")
        self.assertEqual([row.filename for row in rows], ["credit.pdf"])
        self.assertIn("[[HIT]]信贷[[/HIT]]", rows[0].snippet)

    def test_newer_database_is_rejected(self) -> None:
        SearchDatabase(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 10}")
            conn.commit()

        with self.assertRaises(UnsupportedSchemaVersion):
            ChunkStore(self.db_path)


if __name__ == "__main__":
    unittest.main()
