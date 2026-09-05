from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.index_cleanup import remove_missing_under_root
from docseek.search_db import SearchDatabase


class CountingChunkStore(ChunkStore):
    def __init__(self, db_path: Path) -> None:
        self.connect_calls = 0
        super().__init__(db_path)
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return super().connect()


class StaleIndexCleanupTests(unittest.TestCase):
    def test_multiple_missing_files_are_removed_in_one_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "docs"
            root.mkdir()
            db_path = Path(temp_dir) / "docseek.db"
            SearchDatabase(db_path)
            store = CountingChunkStore(db_path)

            keep = root / "keep.txt"
            stale_a = root / "stale-a.txt"
            stale_b = root / "stale-b.txt"
            for index, (path, text) in enumerate(
                (
                    (keep, "保留文件 客户经理"),
                    (stale_a, "待删除文件 信贷旧记录"),
                    (stale_b, "待删除文件 身份证有效期旧记录"),
                )
            ):
                store.replace_document(
                    path=str(path),
                    filename=path.name,
                    extension=".txt",
                    modified_time=float(index + 1),
                    size=len(text.encode("utf-8")),
                    chunks=[DocumentChunk(0, "行 1", text)],
                )

            store.connect_calls = 0
            removed = remove_missing_under_root(store, str(root), {str(keep)})

            self.assertEqual(removed, 2)
            self.assertEqual(store.connect_calls, 1)
            self.assertEqual([row.filename for row in store.search("客户经理")], ["keep.txt"])
            self.assertEqual(store.search("信贷旧记录"), [])
            self.assertEqual(store.search("身份证有效期旧记录"), [])
            with store.connect() as conn:
                remaining = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
