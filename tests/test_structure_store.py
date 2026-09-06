from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from docseek.chunks import DocumentChunk
from docseek.structure_store import FIELDS, structure_for_chunk, write_structure


class StructureStoreTests(unittest.TestCase):
    @staticmethod
    def _connection() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        columns = ", ".join(
            ["chunk_id INTEGER PRIMARY KEY", "kind TEXT", "title TEXT"]
            + [f"{name} INTEGER" for name in FIELDS[2:] if name != "sheet"]
            + ["sheet TEXT"]
        )
        # Recreate the production column order explicitly; SQLite does not care
        # about declaration order, but the INSERT names must all exist.
        conn.execute(f"CREATE TABLE chunk_structure({columns})")
        return conn

    def test_direct_text_write_skips_docir_object_lifting(self) -> None:
        conn = self._connection()
        chunk = DocumentChunk(0, "行 101-180", "正文")
        try:
            with patch(
                "docseek.structure_store.block_from_chunk",
                side_effect=AssertionError("direct text must use the compact structure path"),
            ):
                write_structure(conn, 7, r"C:\docs\notes.txt", ".txt", chunk)

            row = conn.execute(
                "SELECT kind, line_start, line_end, title, page, sheet "
                "FROM chunk_structure WHERE chunk_id = 7"
            ).fetchone()
            self.assertEqual(row[:3], ("text-range", 101, 180))
            self.assertEqual(row[3:], (None, None, None))
        finally:
            conn.close()

    def test_direct_text_structure_helper_matches_docir_contract(self) -> None:
        values = structure_for_chunk(
            r"C:\docs\notes.md",
            ".MD",
            DocumentChunk(2, "行 3-9", "正文"),
        )
        self.assertEqual(values["kind"], "text-range")
        self.assertEqual(values["line_start"], 3)
        self.assertEqual(values["line_end"], 9)
        self.assertIsNone(values["title"])

    def test_malformed_direct_text_location_falls_back_to_docir(self) -> None:
        values = structure_for_chunk(
            r"C:\docs\notes.txt",
            ".txt",
            DocumentChunk(0, "自定义位置", "正文"),
        )
        self.assertEqual(values["kind"], "generic")
        self.assertIsNone(values["title"])


if __name__ == "__main__":
    unittest.main()
