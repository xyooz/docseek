from __future__ import annotations

import unittest

from docseek.structure_ranking import StructureHints, location_boost, parse_structure_query


class StructureRankingTests(unittest.TestCase):
    def test_page_hint_is_removed_from_content_query(self) -> None:
        parsed = parse_structure_query("信贷业务 page:12")
        self.assertEqual(parsed.text, "信贷业务")
        self.assertEqual(parsed.hints.page, 12)
        self.assertTrue(parsed.hints.active)

    def test_slide_and_sheet_hints_are_supported(self) -> None:
        parsed = parse_structure_query('客户经理 slide:4 sheet:"年度 汇总"')
        self.assertEqual(parsed.text, "客户经理")
        self.assertEqual(parsed.hints.slide, 4)
        self.assertEqual(parsed.hints.sheet, "年度 汇总")

    def test_structure_only_query_keeps_legacy_text(self) -> None:
        parsed = parse_structure_query("page:3")
        self.assertEqual(parsed.text, "page:3")
        self.assertFalse(parsed.hints.active)

    def test_malformed_hint_remains_normal_text(self) -> None:
        parsed = parse_structure_query("信贷 page:abc")
        self.assertEqual(parsed.text, "信贷 page:abc")
        self.assertFalse(parsed.hints.active)

    def test_page_location_receives_boost(self) -> None:
        hints = StructureHints(page=2)
        self.assertEqual(location_boost("第 2 页", hints), -3.0)
        self.assertEqual(location_boost("第 1 页", hints), 0.0)

    def test_slide_location_receives_boost(self) -> None:
        hints = StructureHints(slide=8)
        self.assertEqual(location_boost("幻灯片 8", hints), -3.0)
        self.assertEqual(location_boost("幻灯片 7", hints), 0.0)

    def test_sheet_location_receives_boost(self) -> None:
        hints = StructureHints(sheet="客户 数据")
        self.assertEqual(location_boost("工作表 客户 数据 · 行 20-40", hints), -2.5)
        self.assertEqual(location_boost("工作表 汇总 · 行 20-40", hints), 0.0)


if __name__ == "__main__":
    unittest.main()
