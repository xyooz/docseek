from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.index_issues import IndexIssueStore
from docseek.indexer import DirectoryIndexer
from docseek.schema import CURRENT_SCHEMA_VERSION
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
                        CURRENT_SCHEMA_VERSION,
                    )
                self.assertEqual(reopened_issues.count(), 1)
            finally:
                writer.rollback()
                writer.close()
                reader._connection.rollback()
                reader.close()

    def test_nested_full_scan_does_not_self_lock_during_lazy_discovery(self) -> None:
        """Directory issue cleanup must finish before the batched writer starts.

        The real Windows failure appeared on multi-level office trees: indexing
        a fast-lane file opened a batch transaction, then lazy discovery entered
        a child directory and tried to clear an old issue using another SQLite
        connection. That second writer waited on DocSeek's own first writer and
        eventually raised ``database is locked``.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            nested = root / "nested"
            nested.mkdir(parents=True)
            (root / "root.txt").write_text("根目录 信贷", encoding="utf-8")
            (nested / "child.txt").write_text("子目录 客户经理", encoding="utf-8")

            db_path = base / "docseek.db"
            database = SearchDatabase(db_path)
            ChunkStore(db_path)
            issues = IndexIssueStore(db_path)
            issues.record(str(nested.resolve()), "os_error", "stale directory issue")

            stats = DirectoryIndexer(database).scan(root)

            self.assertEqual(stats.indexed, 2)
            self.assertEqual(issues.count(), 0)
            store = ChunkStore(db_path)
            self.assertEqual([row.filename for row in store.search("信贷")], ["root.txt"])
            self.assertEqual([row.filename for row in store.search("客户经理")], ["child.txt"])


if __name__ == "__main__":
    unittest.main()
