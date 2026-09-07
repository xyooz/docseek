from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from docseek.schema import CURRENT_SCHEMA_VERSION, UnsupportedSchemaVersion
from docseek.search_db import SearchDatabase


class SearchDatabaseSchemaPreflightTests(unittest.TestCase):
    def test_newer_database_is_rejected_before_compatibility_schema_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "future.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE future_sentinel(value TEXT NOT NULL)")
                conn.execute("INSERT INTO future_sentinel(value) VALUES ('untouched')")
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
                conn.commit()

            with self.assertRaises(UnsupportedSchemaVersion):
                SearchDatabase(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                sentinel = conn.execute(
                    "SELECT value FROM future_sentinel"
                ).fetchone()

            self.assertEqual(version, CURRENT_SCHEMA_VERSION + 1)
            self.assertEqual(sentinel, ("untouched",))
            self.assertIn("future_sentinel", tables)
            self.assertNotIn("files", tables)
            self.assertNotIn("settings", tables)
            self.assertNotIn("file_fts", tables)
            self.assertNotIn("file_fts_cjk2", tables)
            self.assertNotIn("file_fts_tri", tables)


if __name__ == "__main__":
    unittest.main()
