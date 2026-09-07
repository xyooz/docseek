from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.document_types import KNOWN_DOCUMENT_EXTENSIONS
from docseek.index_cleanup import remove_disabled_extensions
from docseek.index_formats import (
    COMMON_LOCAL_EXTENSIONS,
    DEFAULT_ENABLED_INDEX_EXTENSIONS,
    OFFICE_WPS_EXTENSIONS,
    IndexFormatStore,
    decode_enabled_extensions,
)
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class IndexFormatSettingsTests(unittest.TestCase):
    def test_first_user_created_index_defaults_to_common_office_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SearchDatabase(Path(directory) / "docseek.db")
            formats = IndexFormatStore(database)
            self.assertFalse(formats.has_explicit_setting())
            enabled = formats.ensure_new_index_default()

        self.assertEqual(enabled, DEFAULT_ENABLED_INDEX_EXTENSIONS)
        self.assertEqual(enabled, OFFICE_WPS_EXTENSIONS | COMMON_LOCAL_EXTENSIONS)
        self.assertIn(".docx", enabled)
        self.assertIn(".etx", enabled)
        self.assertIn(".pdf", enabled)
        self.assertIn(".txt", enabled)
        self.assertIn(".md", enabled)
        self.assertIn(".html", enabled)
        self.assertNotIn(".xml", enabled)

    def test_database_without_setting_keeps_legacy_all_format_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SearchDatabase(Path(directory) / "docseek.db")
            enabled = IndexFormatStore(database).enabled_extensions()

        self.assertEqual(enabled, KNOWN_DOCUMENT_EXTENSIONS)

    def test_malformed_setting_falls_back_safely(self) -> None:
        self.assertEqual(
            decode_enabled_extensions("not-json"),
            KNOWN_DOCUMENT_EXTENSIONS,
        )

    def test_disabled_format_is_not_discovered_and_stale_index_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "docs"
            root.mkdir()
            target = root / "slow.xml"
            target.write_text("不再需要索引的 XML 内容", encoding="utf-8")
            database = SearchDatabase(base / "docseek.db")
            store = ChunkStore(database.db_path)

            DirectoryIndexer(database, enabled_extensions={".txt"}).scan(root)
            self.assertEqual(database.count_files(), 0)

            text = root / "keep.txt"
            text.write_text("先建立的测试索引", encoding="utf-8")
            DirectoryIndexer(database, enabled_extensions={".txt"}).scan(root)
            self.assertEqual(database.count_files(), 1)

            removed = remove_disabled_extensions(store, {".docx"})
            self.assertEqual(removed, 1)
            self.assertEqual(database.count_files(), 0)


if __name__ == "__main__":
    unittest.main()
