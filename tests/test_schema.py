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
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
            file_id_index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_chunks_file_id'"
            ).fetchone()

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertIn("file_id", columns)
        self.assertNotIn("path", columns)
        self.assertIsNotNone(file_id_index)

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
            original_chunk_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO chunk_lookup(path, ordinal, fts_rowid) VALUES (?, 0, ?)",
                (path, original_chunk_id),
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
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
            mapped = conn.execute(
                """
                SELECT c.id AS chunk_id, c.file_id, f.id AS expected_file_id
                FROM chunks c JOIN files f ON f.id = c.file_id
                """
            ).fetchone()

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(chunk_count, 1)
        self.assertIsNone(old_table)
        self.assertNotIn("path", columns)
        self.assertIsNotNone(mapped)
        self.assertEqual(int(mapped["chunk_id"]), original_chunk_id)
        self.assertEqual(int(mapped["file_id"]), int(mapped["expected_file_id"]))
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
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
            mapped = conn.execute("SELECT id, file_id FROM chunks").fetchone()

        self.assertIsNone(tri)
        self.assertNotIn("prefix=", base_sql)
        self.assertNotIn("path", columns)
        self.assertIsNotNone(mapped)
        self.assertEqual(int(mapped["id"]), rowid)
        self.assertIsNotNone(mapped["file_id"])
        rows = migrated.search("客户经理")
        self.assertEqual([row.filename for row in rows], ["manual.pdf"])

    def test_v4_chunks_are_backfilled_then_compacted_without_reparsing(self) -> None:
        SearchDatabase(self.db_path)
        path = r"C:\docs\v4.pdf"
        content = "客户经理信贷业务办理规范"
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
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
                    filename_tokens,
                    content_tokens,
                    content='',
                    tokenize='unicode61'
                );
                """
            )
            file_cursor = conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, 'v4.pdf', '.pdf', 2.0, 456, NULL)
                """,
                (path,),
            )
            expected_file_id = int(file_cursor.lastrowid)
            chunk_cursor = conn.execute(
                "INSERT INTO chunks(path, ordinal, location, content) VALUES (?, 0, '第 2 页', ?)",
                (path, content),
            )
            chunk_id = int(chunk_cursor.lastrowid)
            conn.execute(
                "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, 'v4.pdf', ?)",
                (chunk_id, content),
            )
            conn.execute(
                """
                INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens)
                VALUES (?, ?, ?)
                """,
                (
                    chunk_id,
                    ChunkStore._cjk_bigrams("v4.pdf"),
                    ChunkStore._cjk_bigrams(content),
                ),
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()

        migrated = ChunkStore(self.db_path)
        with migrated.connect() as conn:
            row = conn.execute(
                "SELECT id, file_id FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            columns = {
                str(item[1]) for item in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(int(row["id"]), chunk_id)
        self.assertEqual(int(row["file_id"]), expected_file_id)
        self.assertNotIn("path", columns)
        result = migrated.search("客户经理")
        self.assertEqual([item.filename for item in result], ["v4.pdf"])
        self.assertEqual(result[0].location, "第 2 页")

    def test_v5_path_column_is_removed_without_rebuilding_fts(self) -> None:
        SearchDatabase(self.db_path)
        path = r"C:\docs\v5.pdf"
        content = "跨境业务客户经理操作手册"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE chunks(
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    content TEXT NOT NULL,
                    file_id INTEGER,
                    UNIQUE(path, ordinal)
                );
                CREATE INDEX idx_chunks_path ON chunks(path);
                CREATE INDEX idx_chunks_file_id ON chunks(file_id);
                CREATE VIRTUAL TABLE chunk_index USING fts5(
                    filename,
                    content,
                    content='',
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
                    filename_tokens,
                    content_tokens,
                    content='',
                    tokenize='unicode61'
                );
                """
            )
            file_cursor = conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, 'v5.pdf', '.pdf', 5.0, 789, NULL)
                """,
                (path,),
            )
            file_id = int(file_cursor.lastrowid)
            chunk_cursor = conn.execute(
                """
                INSERT INTO chunks(path, file_id, ordinal, location, content)
                VALUES (?, ?, 7, '第 8 页', ?)
                """,
                (path, file_id, content),
            )
            chunk_id = int(chunk_cursor.lastrowid)
            conn.execute(
                "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, 'v5.pdf', ?)",
                (chunk_id, content),
            )
            conn.execute(
                """
                INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens)
                VALUES (?, ?, ?)
                """,
                (
                    chunk_id,
                    ChunkStore._cjk_bigrams("v5.pdf"),
                    ChunkStore._cjk_bigrams(content),
                ),
            )
            conn.execute("PRAGMA user_version = 5")
            conn.commit()

        migrated = ChunkStore(self.db_path)
        with migrated.connect() as conn:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
            row = conn.execute(
                "SELECT id, file_id, ordinal, location, content FROM chunks"
            ).fetchone()
            fts_row = conn.execute(
                "SELECT rowid FROM chunk_index WHERE rowid = ?", (chunk_id,)
            ).fetchone()

        self.assertNotIn("path", columns)
        self.assertEqual(int(row["id"]), chunk_id)
        self.assertEqual(int(row["file_id"]), file_id)
        self.assertEqual(int(row["ordinal"]), 7)
        self.assertEqual(str(row["location"]), "第 8 页")
        self.assertEqual(str(row["content"]), content)
        self.assertIsNotNone(fts_row)
        result = migrated.search("跨境业务")
        self.assertEqual([item.filename for item in result], ["v5.pdf"])
        self.assertEqual(result[0].location, "第 8 页")

    def test_new_chunks_store_uses_only_integer_file_id(self) -> None:
        SearchDatabase(self.db_path)
        store = ChunkStore(self.db_path)
        path = r"C:\docs\new.pdf"

        store.replace_document(
            path=path,
            filename="new.pdf",
            extension=".pdf",
            modified_time=3.0,
            size=123,
            chunks=[DocumentChunk(0, "第 1 页", "信贷客户经理")],
        )
        with store.connect() as conn:
            row = conn.execute(
                """
                SELECT c.file_id, f.id
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE f.path = ?
                """,
                (path,),
            ).fetchone()
            columns = {
                str(item[1]) for item in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }

        self.assertIsNotNone(row)
        self.assertEqual(int(row["file_id"]), int(row["id"]))
        self.assertNotIn("path", columns)

    def test_newer_database_is_rejected(self) -> None:
        SearchDatabase(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 10}")
            conn.commit()

        with self.assertRaises(UnsupportedSchemaVersion):
            ChunkStore(self.db_path)


if __name__ == "__main__":
    unittest.main()
