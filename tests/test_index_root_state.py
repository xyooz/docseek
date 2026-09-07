from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.index_root_state import IndexRootStateStore, PAUSED_INDEX_ROOTS_KEY
from docseek.search_db import SearchDatabase


class IndexRootStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SearchDatabase(Path(self.temp_dir.name) / "docseek.db")
        self.root_a = Path(self.temp_dir.name) / "A"
        self.root_b = Path(self.temp_dir.name) / "B"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.db.add_index_root(str(self.root_a))
        self.db.add_index_root(str(self.root_b))
        self.store = IndexRootStateStore(self.db)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pause_keeps_root_but_removes_it_from_active_roots(self) -> None:
        self.assertTrue(self.store.set_paused(self.root_b, True))
        self.assertEqual(self.store.paused_roots(), [str(self.root_b.resolve())])
        self.assertEqual(self.store.active_roots(), [str(self.root_a.resolve())])
        self.assertEqual(self.db.get_index_roots(), [str(self.root_a.resolve()), str(self.root_b.resolve())])

        self.assertTrue(self.store.set_paused(self.root_b, False))
        self.assertEqual(self.store.paused_roots(), [])
        self.assertEqual(self.store.active_roots(), self.db.get_index_roots())

    def test_unknown_or_stale_paused_paths_do_not_disable_other_roots(self) -> None:
        stale = Path(self.temp_dir.name) / "removed"
        self.db._set_setting(
            PAUSED_INDEX_ROOTS_KEY,
            f'["{str(stale).replace(chr(92), chr(92) * 2)}"]',
        )
        self.assertEqual(self.store.paused_roots(), [])
        self.assertEqual(self.store.active_roots(), self.db.get_index_roots())
        self.assertFalse(self.store.set_paused(stale, True))

    def test_replace_paused_supports_dialog_roots_before_database_mutation(self) -> None:
        root_c = Path(self.temp_dir.name) / "C"
        root_c.mkdir()
        intended_roots = [str(self.root_a), str(root_c)]
        self.store.replace_paused([str(root_c)], known_roots=intended_roots)

        # C is not a configured index root yet, so it cannot affect active state.
        self.assertEqual(self.store.paused_roots(), [])
        self.db.remove_index_root(str(self.root_b))
        self.db.add_index_root(str(root_c))
        self.assertEqual(self.store.paused_roots(), [str(root_c.resolve())])
        self.assertEqual(self.store.active_roots(), [str(self.root_a.resolve())])

    def test_corrupt_pause_setting_is_ignored(self) -> None:
        self.db._set_setting(PAUSED_INDEX_ROOTS_KEY, "not-json")
        self.assertEqual(self.store.paused_roots(), [])
        self.assertEqual(self.store.active_roots(), self.db.get_index_roots())


if __name__ == "__main__":
    unittest.main()
