from __future__ import annotations

import unittest
from pathlib import Path

from docseek.index_priority import is_fast_lane_path, prioritize_index_candidates


class IndexPriorityTests(unittest.TestCase):
    def test_modern_formats_are_fast_lane(self) -> None:
        for name in (
            "a.docx",
            "b.xlsx",
            "c.pptx",
            "d.pdf",
            "e.txt",
            "f.md",
            "g.csv",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_fast_lane_path(Path(name)))

        for name in ("old.xls", "old.doc", "old.ppt", "sheet.ods"):
            with self.subTest(name=name):
                self.assertFalse(is_fast_lane_path(Path(name)))

    def test_discovery_finishes_before_index_lane_replay(self) -> None:
        discovered: list[str] = []

        def candidates():
            for name in ("first.xls", "one.docx", "second.doc", "two.pdf", "third.ppt"):
                discovered.append(name)
                yield Path(name)

        ordered = prioritize_index_candidates(candidates())
        self.assertEqual(next(ordered).name, "one.docx")
        # The complete filesystem discovery snapshot is collected before any
        # candidate is handed to the batched SQLite writer. This prevents the
        # lazy directory walker from performing issue-metadata writes while an
        # index transaction is already open.
        self.assertEqual(
            discovered,
            ["first.xls", "one.docx", "second.doc", "two.pdf", "third.ppt"],
        )

        self.assertEqual(next(ordered).name, "two.pdf")
        self.assertEqual(
            [path.name for path in ordered],
            ["first.xls", "second.doc", "third.ppt"],
        )

    def test_all_compatibility_candidates_are_preserved(self) -> None:
        candidates = [Path("a.xls"), Path("b.doc"), Path("c.ppt")]
        self.assertEqual(list(prioritize_index_candidates(candidates)), candidates)


if __name__ == "__main__":
    unittest.main()
