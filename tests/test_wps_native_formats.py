from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from docseek.chunk_store import ChunkStore
from docseek.document_adapters import TikaNativeAdapter
from docseek.document_types import SupportMode, get_format_capability
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase
from tools.wps_fixtures import (
    fixture_bytes as reconstruct_fixture,
    load_manifest,
    materialize_fixture as materialize_wps_fixture,
)


CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
FIXTURES = load_manifest()


def fixture_bytes(name: str) -> bytes:
    return reconstruct_fixture(name, manifest=FIXTURES)


def materialize_fixture(name: str, directory: Path) -> Path:
    return materialize_wps_fixture(name, directory / name, manifest=FIXTURES)


class WpsNativeFormatTests(unittest.TestCase):
    def test_fixture_payloads_reconstruct_exact_ole_compound_documents(self) -> None:
        for name in FIXTURES:
            with self.subTest(name=name):
                data = fixture_bytes(name)
                self.assertEqual(data[:8], CFB_MAGIC)

    def test_wps_formats_prefer_native_tika_before_vendor_fallback(self) -> None:
        for extension in (".wps", ".et", ".ett", ".etx", ".ettx", ".dps"):
            with self.subTest(extension=extension):
                capability = get_format_capability(extension)
                self.assertIsNotNone(capability)
                assert capability is not None
                self.assertEqual(
                    capability.modes,
                    (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
                )

    def test_native_tika_extracts_real_wps_et_dps_without_wps_automation(self) -> None:
        adapter = TikaNativeAdapter()
        self.assertTrue(adapter.is_available(), "Windows CI installs DocSeek's tika extra")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in FIXTURES:
                with self.subTest(name=name):
                    path = materialize_fixture(name, root)
                    content = "\n".join(chunk.content for chunk in adapter.iter_chunks(path))
                    for expected in FIXTURES[name]["expected_text"]:
                        self.assertIn(str(expected), content)

    def test_tika_accepts_et_container_through_template_and_2007_suffixes(self) -> None:
        """Prove suffix routing does not block Tika while real fixtures are pending.

        The bytes are the repository's genuine ET sample, deliberately copied
        under each newly registered suffix. This is not a claim that every
        ETT/ETX/ETTX producer emits identical bytes; dedicated fixtures should
        replace these aliases when they become available.
        """
        adapter = TikaNativeAdapter()
        self.assertTrue(adapter.is_available(), "Windows CI installs DocSeek's tika extra")
        data = fixture_bytes("sample_sheet.et")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for extension in (".ett", ".etx", ".ettx"):
                with self.subTest(extension=extension):
                    path = root / f"sample{extension}"
                    path.write_bytes(data)
                    content = "\n".join(
                        chunk.content for chunk in adapter.iter_chunks(path)
                    )
                    for expected in FIXTURES["sample_sheet.et"]["expected_text"]:
                        self.assertIn(str(expected), content)

    def test_directory_indexer_makes_each_real_wps_fixture_searchable(self) -> None:
        for name in FIXTURES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                base = Path(temp_dir)
                root = base / "docs"
                root.mkdir()
                materialize_fixture(name, root)

                database = SearchDatabase(base / "docseek.db")
                store = ChunkStore(database.db_path)
                stats = DirectoryIndexer(database).scan(root)

                self.assertEqual(stats.indexed, 1, name)
                self.assertEqual(stats.skipped, 0, name)
                matches = store.search(str(FIXTURES[name]["expected_text"][0]))
                self.assertIn(name, [match.filename for match in matches])

                # The source also contains the mixed token "郑xingyu". Direct
                # extraction above proves it survives parsing; do not assert a
                # substring-only `xingyu` FTS hit because unicode61 may tokenize
                # the adjacent CJK+Latin sequence as one lexical token.


if __name__ == "__main__":
    unittest.main()
