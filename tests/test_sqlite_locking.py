from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.index_issues import IndexIssueStore
from docseek.search_db import SearchDatabase
from docseek.search_session import PersistentSearchStore


class _FakeConnection:
    def __init__(self) -> None:
        self.row_factory = None
        self.statements: list[str] = []

    def execute(self, sql: str, *_args, **_kwargs):
        self.statements.append(sql)
        return self


class SqliteLockingTests(unittest.TestCase):
    def test_normal_metadata_connection_does_not_reissue_journal_mode(self) -> None:
        fake = _FakeConnection()
        database = object.__new__(SearchDatabase)
        database.db_path = Path("unused.db")

        with patch("docseek.search_db.sqlite3.connect", return_value=fake):
            self.assertIs(database.connect(), fake)

        normalized = [statement.strip().casefold() for statement in fake.statements]
        self.assertIn("pragma busy_timeout=10000", normalized)
        self.assertFalse(any("journal_mode" in statement for statement in normalized))

    def test_metadata_reads_succeed_while_chunk_writer_holds_wal_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            database = SearchDatabase(db_path)
            store = ChunkStore(db_path)
            database.add_index_root(str(Path(temp_dir) / "docs"))

            writer = store.connect()
            try:
                writer.execute("BEGIN IMMEDIATE")
                roots = database.get_index_roots()
                self.assertEqual(len(roots), 1)
            finally:
                writer.rollback()
                writer.close()

    def test_runtime_reopens_do_not_reinitialize_schema_with_active_reader_and_writer(self) -> None:
        """Regression for real Windows portable ``database is locked`` reports.

        Interactive search intentionally keeps a read connection alive, while
        indexing owns SQLite's single WAL writer slot in bounded transactions.
        Reconstructing runtime stores in that state must only inspect the
        already-current schema; it must not reissue journal-mode or DDL setup.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "docseek.db"
            database = SearchDatabase(db_path)
            store = ChunkStore(db_path)
            issues = IndexIssueStore(db_path)
            database.add_index_root(str(Path(temp_dir) / "docs"))
            issues.record("seed", "os_error", "seed")

            reader = PersistentSearchStore(db_path)
            writer = store.connect()
            try:
                # Hold a real WAL read snapshot instead of merely keeping an
                # idle handle open, then acquire the independent writer slot.
                reader._connection.execute("BEGIN")
                reader._connection.execute("SELECT COUNT(*) FROM files").fetchone()
                writer.execute("BEGIN IMMEDIATE")

                reopened_database = SearchDatabase(db_path)
                reopened_store = ChunkStore(db_path)
                reopened_issues = IndexIssueStore(db_path)

                self.assertEqual(reopened_database.get_index_roots(), database.get_index_roots())
                with reopened_store.connect() as conn:
                    self.assertEqual(
                        conn.execute("PRAGMA user_version").fetchone()[0],
                        10,
                    )
                self.assertEqual(reopened_issues.count(), 1)
            finally:
                writer.rollback()
                writer.close()
                reader._connection.rollback()
                reader.close()


if __name__ == "__main__":
    unittest.main()
