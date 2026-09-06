from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class IndexDiscoveryProgressTests(unittest.TestCase):
    def test_discovery_reports_candidate_total_before_content_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            nested = root / "部门" / "项目"
            nested.mkdir(parents=True)
            (root / "制度.txt").write_text("客户经理管理办法", encoding="utf-8")
            (nested / "清单.txt").write_text("贷后风险排查清单", encoding="utf-8")
            (nested / "忽略.bin").write_bytes(b"not indexed")

            indexer = DirectoryIndexer(SearchDatabase(base / "docseek.db"))
            events: list[tuple[str, int]] = []

            stats = indexer.scan(
                root,
                on_discovery=lambda _path, count: events.append(("discovery", count)),
                on_candidates_ready=lambda count: events.append(("ready", count)),
                on_progress=lambda _path, current: events.append(
                    ("index", current.scanned)
                ),
            )

            discovery_counts = [value for kind, value in events if kind == "discovery"]
            self.assertTrue(discovery_counts)
            self.assertEqual(discovery_counts[0], 0)
            self.assertEqual(discovery_counts[-1], 2)
            self.assertEqual(max(discovery_counts), 2)

            ready_index = events.index(("ready", 2))
            first_index = next(
                index for index, event in enumerate(events) if event[0] == "index"
            )
            self.assertLess(ready_index, first_index)
            self.assertEqual(stats.scanned, 2)
            self.assertEqual(stats.indexed, 2)


if __name__ == "__main__":
    unittest.main()
