from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.search_db import SearchDatabase


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


if __name__ == "__main__":
    unittest.main()
