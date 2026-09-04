from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class DirectoryIndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.root.mkdir()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")
        self.chunks = ChunkStore(self.db.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_excluded_directory_is_not_indexed(self) -> None:
        visible = self.root / "visible.txt"
        visible.write_text("公开工作资料 信贷", encoding="utf-8")
        private_dir = self.root / "private"
        private_dir.mkdir()
        (private_dir / "secret.txt").write_text("不应索引的内部资料", encoding="utf-8")

        self.db.add_excluded_path(str(private_dir))
        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        self.assertGreaterEqual(stats.excluded, 1)
        self.assertEqual([row.filename for row in self.chunks.search("信贷")], ["visible.txt"])
        self.assertEqual(self.chunks.search("内部资料"), [])

    def test_second_scan_skips_unchanged_file(self) -> None:
        target = self.root / "guide.txt"
        target.write_text("客户服务操作指引", encoding="utf-8")

        first = DirectoryIndexer(self.db).scan(self.root)
        second = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(first.indexed, 1)
        self.assertGreaterEqual(first.chunks, 1)
        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.unchanged, 1)

    def test_pre_chunk_file_is_migrated_even_when_unchanged(self) -> None:
        target = self.root / "legacy.txt"
        target.write_text("历史制度材料 信贷", encoding="utf-8")
        stat = target.stat()
        self.db.upsert_document(
            path=str(target.resolve()),
            filename=target.name,
            extension=".txt",
            modified_time=stat.st_mtime,
            size=stat.st_size,
            content="历史制度材料 信贷",
        )

        stats = DirectoryIndexer(self.db).scan(self.root)

        self.assertEqual(stats.indexed, 1)
        rows = self.chunks.search("信贷")
        self.assertEqual([row.filename for row in rows], ["legacy.txt"])
        self.assertTrue(rows[0].location.startswith("行 "))


if __name__ == "__main__":
    unittest.main()
