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

    def test_existing_progress_channel_sees_discovery_and_final_candidate_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "docs"
            root.mkdir()
            (root / "制度.txt").write_text("客户经理管理办法", encoding="utf-8")
            (root / "图片.bin").write_bytes(b"not indexed")

            labels: list[str] = []
            stats = DirectoryIndexer(SearchDatabase(base / "docseek.db")).scan(
                root,
                on_progress=lambda path, _current: labels.append(path.name),
            )

            self.assertEqual(stats.scanned, 1)
            self.assertEqual(stats.indexed, 1)
            self.assertTrue(
                any(label.startswith("正在扫描目录 · 已发现 0 个候选文件") for label in labels)
            )
            self.assertIn("扫描完成 · 共发现 1 个候选文件", labels)
            self.assertIn("制度.txt", labels)


if __name__ == "__main__":
    unittest.main()
