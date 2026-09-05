from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from docseek.chunk_store import ChunkStore
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

    def test_v2_index_is_migrated_without_reparsing_documents(self) -> None:
        SearchDatabase(self.db_path)
        path = r"C:\docs\credit.pdf"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE chunk_fts USING fts5(
                    path UNINDEXED,
                    ordinal UNINDEXED,
                    location UNINDEXED,
                    filename,
                    content,
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4'
                );
                CREATE VIRTUAL TABLE chunk_fts_cjk2 USING fts5(
                    path UNINDEXED,
                    ordinal UNINDEXED,
                    location UNINDEXED,
                    filename_tokens,
                    content_tokens,
                    tokenize='unicode61'
                );
                CREATE TABLE chunk_lookup(
                    path TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    fts_rowid INTEGER NOT NULL,
                    PRIMARY KEY(path, ordinal)
                ) WITHOUT ROWID;
                """
            )
            conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, 'credit.pdf', '.pdf', 1.0, 123, NULL)
                """,
                (path,),
            )
            cursor = conn.execute(
                """
                INSERT INTO chunk_fts(path, ordinal, location, filename, content)
                VALUES (?, 0, '第 1 页', 'credit.pdf', '客户经理办理信贷业务')
                """,
                (path,),
            )
            conn.execute(
                "INSERT INTO chunk_lookup(path, ordinal, fts_rowid) VALUES (?, 0, ?)",
                (path, int(cursor.lastrowid)),
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()

        migrated = ChunkStore(self.db_path)
        with migrated.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            old_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_fts'"
            ).fetchone()

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(chunk_count, 1)
        self.assertIsNone(old_table)
        rows = migrated.search("信贷")
        self.assertEqual([row.filename for row in rows], ["credit.pdf"])
        self.assertIn("[[HIT]]信贷[[/HIT]]", rows[0].snippet)

    def test_v3_index_drops_trigram_and_unused_prefix_copy(self) -> None:
        SearchDatabase(self.db_path)
        path = r"C:\docs\manual.pdf"
        content = "农业银行客户经理办理信贷业务"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE chunks(
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(path, ordinal)
                );
                CREATE INDEX idx_chunks_path ON chunks(path);
                CREATE VIRTUAL TABLE chunk_index USING fts5(
                    filename,
                    content,
                    content='',
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4'
                );
                CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
                    filename_tokens,
                    content_tokens,
                    content='',
                    tokenize='unicode61'
                );
                CREATE VIRTUAL TABLE chunk_index_tri USING fts5(
                    filename,
                    content,
                    content='',
                    tokenize='trigram case_sensitive 0'
                );
                """
            )
            conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, 'manual.pdf', '.pdf', 1.0, 123, NULL)
                """,
                (path,),
            )
            cursor = conn.execute(
                "INSERT INTO chunks(path, ordinal, location, content) VALUES (?, 0, '第 1 页', ?)",
                (path, content),
            )
            rowid = int(cursor.lastrowid)
            bigrams = "农业 业银 银行 行客 客户 户经 经理 理办 办理 理信 信贷 贷业 业务"
            conn.execute(
                "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, 'manual.pdf', ?)",
                (rowid, content),
            )
            conn.execute(
                "INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens) VALUES (?, '', ?)",
                (rowid, bigrams),
            )
            conn.execute(
                "INSERT INTO chunk_index_tri(rowid, filename, content) VALUES (?, 'manual.pdf', ?)",
                (rowid, content),
            )
            conn.execute("PRAGMA user_version = 3")
            conn.commit()

        migrated = ChunkStore(self.db_path)
        with migrated.connect() as conn:
            tri = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_index_tri'"
            ).fetchone()
            base_sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_index'"
                ).fetchone()[0]
            )

        self.assertIsNone(tri)
        self.assertNotIn("prefix=", base_sql)
        rows = migrated.search("客户经理")
        self.assertEqual([row.filename for row in rows], ["manual.pdf"])

    def test_newer_database_is_rejected(self) -> None:
        SearchDatabase(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 10}")
            conn.commit()

        with self.assertRaises(UnsupportedSchemaVersion):
            ChunkStore(self.db_path)


if __name__ == "__main__":
    unittest.main()
