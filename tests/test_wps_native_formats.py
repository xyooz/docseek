from __future__ import annotations

import base64
import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.document_adapters import TikaNativeAdapter
from docseek.document_types import SupportMode, get_format_capability
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wps"
CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

FIXTURES = {
    "sample_writer.wps": {
        "payload": "sample_writer.wps.gz.b64",
        "size": 10_240,
        "sha256": "882b16b7a5e97a06e37b100457eb3141d9fb6f8ef751a88da52a99144ab17bd1",
    },
    "sample_sheet.et": {
        "payload_parts": "sample_sheet.et.gz.b64.part*",
        "size": 19_968,
        "sha256": "d9fb29635a52a02776a270d4f4407201ecf7c70f646f9f2c0a6969dbf071dcea",
    },
    "sample_slides.dps": {
        "payload_parts": "sample_slides.dps.gz.b64.part*",
        "size": 50_176,
        "sha256": "5ccaa90fd0b472f8c8e2474cc9ea89ce1b6da10d7571b8338ffa9940ce4fbd35",
    },
}


def fixture_bytes(name: str) -> bytes:
    spec = FIXTURES[name]
    if "payload" in spec:
        encoded = (FIXTURE_ROOT / str(spec["payload"])).read_text(encoding="ascii")
    else:
        parts = sorted(FIXTURE_ROOT.glob(str(spec["payload_parts"])))
        if not parts:
            raise AssertionError(f"missing fixture payload parts for {name}")
        encoded = "".join(part.read_text(encoding="ascii") for part in parts)

    data = gzip.decompress(base64.b64decode(encoded))
    if len(data) != int(spec["size"]):
        raise AssertionError(f"fixture size mismatch for {name}: {len(data)}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec["sha256"]:
        raise AssertionError(f"fixture SHA-256 mismatch for {name}: {digest}")
    return data


def materialize_fixture(name: str, directory: Path) -> Path:
    target = directory / name
    target.write_bytes(fixture_bytes(name))
    return target


class WpsNativeFormatTests(unittest.TestCase):
    def test_fixture_payloads_reconstruct_exact_ole_compound_documents(self) -> None:
        for name in FIXTURES:
            with self.subTest(name=name):
                data = fixture_bytes(name)
                self.assertEqual(data[:8], CFB_MAGIC)

    def test_wps_et_dps_prefer_native_tika_before_vendor_fallback(self) -> None:
        for extension in (".wps", ".et", ".dps"):
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
                    self.assertIn("测试测试", content)
                    self.assertIn("xingyu", content)

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
                matches = store.search("测试测试")
                self.assertIn(name, [match.filename for match in matches])

                # The source also contains the mixed token "郑xingyu". Direct
                # extraction above proves it survives parsing; do not assert a
                # substring-only `xingyu` FTS hit because unicode61 may tokenize
                # the adjacent CJK+Latin sequence as one lexical token.


if __name__ == "__main__":
    unittest.main()
