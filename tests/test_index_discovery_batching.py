from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class IndexDiscoveryBatchingTests(unittest.TestCase):
    def test_full_scan_batches_discovery_issue_metadata_before_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            nested = root / "部门" / "项目" / "资料"
            nested.mkdir(parents=True)
            document = nested / "制度.txt"
            document.write_text("客户经理 信贷 业务制度", encoding="utf-8")

            database = SearchDatabase(base / "docseek.db")
            indexer = DirectoryIndexer(database)

            # Healthy discovery must not perform one SQLite issue write per
            # directory/path. Those writes are collected in memory and applied
            # through clear_many/record_many before the content writer starts.
            with (
                patch.object(
                    indexer.issues,
                    "clear",
                    side_effect=AssertionError("per-path issue clear during discovery"),
                ),
                patch.object(
                    indexer.issues,
                    "record",
                    side_effect=AssertionError("per-path issue record during healthy discovery"),
                ),
                patch.object(
                    indexer.issues,
                    "clear_many",
                    wraps=indexer.issues.clear_many,
                ) as clear_many,
                patch.object(
                    indexer.issues,
                    "record_many",
                    wraps=indexer.issues.record_many,
                ) as record_many,
            ):
                stats = indexer.scan(root)

            self.assertEqual(stats.indexed, 1)
            self.assertGreaterEqual(clear_many.call_count, 1)
            self.assertEqual(record_many.call_count, 1)
            self.assertEqual(
                [row.filename for row in ChunkStore(database.db_path).search("业务制度")],
                [document.name],
            )

    def test_empty_exclusion_list_skips_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = SearchDatabase(Path(temp_dir) / "docseek.db")
            indexer = DirectoryIndexer(database, excluded_paths=[])
            candidate = Path(temp_dir) / "does-not-need-to-exist"

            with patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("resolve should not run without exclusions"),
            ):
                self.assertFalse(indexer._is_excluded(candidate))


if __name__ == "__main__":
    unittest.main()
