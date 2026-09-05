from __future__ import annotations

import unittest

from docseek.app import result_entry_row


class KeyboardNavigationTests(unittest.TestCase):
    def test_empty_results_have_no_entry_row(self) -> None:
        self.assertIsNone(result_entry_row(0, move_down=True))
        self.assertIsNone(result_entry_row(0, move_down=False))

    def test_down_from_search_enters_first_result(self) -> None:
        self.assertEqual(result_entry_row(5, move_down=True), 0)

    def test_up_from_search_enters_last_result(self) -> None:
        self.assertEqual(result_entry_row(5, move_down=False), 4)


if __name__ == "__main__":
    unittest.main()
