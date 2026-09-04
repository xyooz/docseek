from __future__ import annotations

import unittest

from docseek.query_parser import parse_query


class QueryParserTests(unittest.TestCase):
    def test_plain_terms(self) -> None:
        parsed = parse_query("客户经理 信贷")
        self.assertEqual(parsed.terms, ("客户经理", "信贷"))
        self.assertIsNone(parsed.extension)
        self.assertIsNone(parsed.path_contains)

    def test_extension_and_path_filters(self) -> None:
        parsed = parse_query('信贷 ext:PDF path:"业务 制度"')
        self.assertEqual(parsed.terms, ("信贷",))
        self.assertEqual(parsed.extension, ".pdf")
        self.assertEqual(parsed.path_contains, "业务 制度")

    def test_quoted_phrase_is_kept_as_one_term(self) -> None:
        parsed = parse_query('"customer manager" manual')
        self.assertEqual(parsed.terms, ("customer manager", "manual"))

    def test_unknown_filter_is_normal_text(self) -> None:
        parsed = parse_query("客户 owner:zhang")
        self.assertEqual(parsed.terms, ("客户", "owner:zhang"))

    def test_unmatched_quote_falls_back_instead_of_failing(self) -> None:
        parsed = parse_query('客户 "经理')
        self.assertTrue(parsed.terms)


if __name__ == "__main__":
    unittest.main()
