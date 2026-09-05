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

    def test_fast_lane_streams_before_discovery_finishes(self) -> None:
        discovered: list[str] = []

        def candidates():
            for name in ("first.xls", "one.docx", "second.doc", "two.pdf", "third.ppt"):
                discovered.append(name)
                yield Path(name)

        ordered = prioritize_index_candidates(candidates())
        self.assertEqual(next(ordered).name, "one.docx")
        self.assertEqual(discovered, ["first.xls", "one.docx"])

        self.assertEqual(next(ordered).name, "two.pdf")
        self.assertEqual(
            discovered,
            ["first.xls", "one.docx", "second.doc", "two.pdf"],
        )

        # Once discovery is exhausted, compatibility files replay in stable
        # encounter order rather than being lost or arbitrarily reordered.
        self.assertEqual(
            [path.name for path in ordered],
            ["first.xls", "second.doc", "third.ppt"],
        )

    def test_all_compatibility_candidates_are_preserved(self) -> None:
        candidates = [Path("a.xls"), Path("b.doc"), Path("c.ppt")]
        self.assertEqual(list(prioritize_index_candidates(candidates)), candidates)


if __name__ == "__main__":
    unittest.main()
