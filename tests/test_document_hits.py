from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.document_hits import document_hits
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.schema import CURRENT_SCHEMA_VERSION
from docseek.search_db import SearchDatabase
from docseek.structure_store import preview_location


class DocumentHitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "index.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, name="policy.pdf", count=65):
        args = dict(path=str(Path(self.temp.name) / name), filename=name, extension=".pdf",
                    modified_time=10, size=100,
                    chunks=[DocumentChunk(i, f"第 {i + 1} 页", f"客户经理 policy {i}") for i in range(count)])
        self.store.replace_document(**args)
        return ExactGroupedSearchEngine(self.store).search_page("客户经理").items[0]

    def test_all_matching_chunks_paginate_without_other_files(self):
        selected = self.seed()
        self.seed("other.pdf", 10)
        first, more = document_hits(self.store, selected, "客户经理 page:2")
        second, more2 = document_hits(self.store, selected, "客户经理 page:2", offset=30)
        last, more3 = document_hits(self.store, selected, "客户经理 page:2", offset=60)
        self.assertEqual((len(first), len(second), len(last)), (30, 30, 5))
        self.assertEqual((more, more2, more3), (True, True, False))
        self.assertEqual(len({item.chunk_id for item in first + second + last}), 65)
        self.assertTrue(all(item.path == selected.path for item in first + second + last))
        self.assertEqual(first[1].structure["page"], 2)
        self.assertIn("[[HIT]]", first[0].snippet)

    def test_structure_survives_label_change_and_delete_cleans_sidecar(self):
        selected = self.seed(count=2)
        with self.store.connect() as conn:
            conn.execute("UPDATE chunks SET location='Page label changed'")
        page = ExactGroupedSearchEngine(self.store).search_page("客户经理")
        self.assertEqual(preview_location(page.items[0]), "第 1 页")
        items, _ = document_hits(self.store, selected, "客户经理")
        self.assertEqual(preview_location(items[1]), "第 2 页")
        self.store.remove_document(selected.path)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunk_structure").fetchone()[0], 0)

    def test_v10_upgrade_is_additive_and_legacy_locations_still_work(self):
        selected = self.seed(count=2)
        with self.store.connect() as conn:
            original = [tuple(row) for row in conn.execute("SELECT id,content FROM chunks")]
            conn.execute("DROP TRIGGER chunk_structure_cleanup")
            conn.execute("DROP TABLE chunk_structure")
            conn.execute("PRAGMA user_version=10")
        upgraded = ChunkStore(self.db_path)
        with upgraded.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT id,content FROM chunks")], original)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunk_structure").fetchone()[0], 0)
        items, _ = document_hits(upgraded, selected, "客户经理")
        self.assertEqual(items[0].structure["page"], 1)

    def test_changed_index_rejects_stale_selected_result(self):
        selected = self.seed(count=2)
        with self.store.connect() as conn:
            conn.execute("UPDATE files SET modified_time=11")
        with self.assertRaisesRegex(ValueError, "重新搜索"):
            document_hits(self.store, selected, "客户经理")

    def test_batch_writer_persists_structure_and_rolls_back_sidecar(self):
        args = dict(path="sheet.xlsx", filename="sheet.xlsx", extension=".xlsx", modified_time=1, size=1)
        with ChunkBatchWriter(self.store) as writer:
            writer.replace_document(**args, chunks=[DocumentChunk(0, "工作表 明细 · 行 3-7", "客户经理")])
        with self.store.connect() as conn:
            row = conn.execute("SELECT sheet,row_start,row_end FROM chunk_structure").fetchone()
            self.assertEqual(tuple(row), ("明细", 3, 7))
        def reject():
            raise ValueError("reject")
        with self.assertRaises(ValueError):
            with ChunkBatchWriter(self.store) as writer:
                writer.replace_document(**args, chunks=[], validate_source=reject)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunk_structure").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
