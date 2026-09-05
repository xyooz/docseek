from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.search_db import SearchDatabase
from docseek.search_presets import SearchState, SearchStateStore, search_state_label
from docseek.search_sort import SORT_FILENAME, SORT_MODIFIED


class SearchStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        self.database = SearchDatabase(self.db_path)
        self.store = SearchStateStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_history_deduplicates_and_moves_repeated_state_to_front(self) -> None:
        first = SearchState("信贷制度", ".pdf")
        second = SearchState("客户经理", ".docx", SORT_MODIFIED)
        self.store.record_history(first)
        self.store.record_history(second)
        self.store.record_history(first)

        history = self.store.history()
        self.assertEqual([item.key for item in history], [first.key, second.key])

    def test_history_is_capped_and_normalizes_extensions(self) -> None:
        for index in range(self.store.MAX_HISTORY + 5):
            self.store.record_history(SearchState(f"查询{index}", "PDF"))

        history = self.store.history()
        self.assertEqual(len(history), self.store.MAX_HISTORY)
        self.assertEqual(history[0].extension, ".pdf")
        self.assertEqual(history[0].query, f"查询{self.store.MAX_HISTORY + 4}")

    def test_saved_search_toggle_is_independent_from_history(self) -> None:
        state = SearchState('客户 path:"制度 文件"', ".xlsx", SORT_FILENAME)
        self.store.record_history(state)

        self.assertTrue(self.store.toggle_saved(state))
        self.assertTrue(self.store.is_saved(state))
        self.assertEqual(self.store.saved()[0].key, state.normalized().key)
        self.assertEqual(len(self.store.history()), 1)

        self.assertFalse(self.store.toggle_saved(state))
        self.assertFalse(self.store.is_saved(state))
        self.assertEqual(self.store.saved(), [])
        self.assertEqual(len(self.store.history()), 1)

    def test_clear_history_does_not_clear_saved_searches(self) -> None:
        state = SearchState("身份证有效期", ".pdf")
        self.store.record_history(state)
        self.store.toggle_saved(state)

        self.store.clear_history()

        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.saved(), [state.normalized()])

    def test_type_only_state_is_meaningful_but_sort_only_is_not(self) -> None:
        type_only = SearchState("", ".pdf", SORT_MODIFIED)
        sort_only = SearchState("", None, SORT_MODIFIED)

        self.store.record_history(type_only)
        self.store.record_history(sort_only)

        self.assertEqual(self.store.history(), [type_only.normalized()])

    def test_corrupt_and_unknown_saved_values_are_ignored(self) -> None:
        self.database._set_setting(self.store.SAVED_KEY, "not-json")
        self.assertEqual(self.store.saved(), [])

        self.database._set_setting(
            self.store.SAVED_KEY,
            '[{"query":"有效","sort_mode":"unknown"},{"query":123}]',
        )
        self.assertEqual(self.store.saved(), [])

    def test_labels_include_ui_state_without_becoming_too_long(self) -> None:
        state = SearchState("这是一个非常非常非常非常非常非常非常非常非常非常长的查询条件", ".xlsx", SORT_FILENAME)
        label = search_state_label(state, max_query_chars=12)
        self.assertIn("…", label)
        self.assertIn("XLSX", label)
        self.assertIn("文件名排序", label)


if __name__ == "__main__":
    unittest.main()
