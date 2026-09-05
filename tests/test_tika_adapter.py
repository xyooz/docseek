from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.doc_ir import BlockKind
from docseek.document_adapters import TikaNativeAdapter
from docseek.extraction_broker import ContentExtractionBroker
from docseek.document_adapters import DocumentAdapterRegistry


@unittest.skipUnless(
    TikaNativeAdapter().is_available(),
    "iscc-tika optional dependency is not installed",
)
class TikaNativeAdapterTests(unittest.TestCase):
    def test_extracts_real_rtf_without_java_or_vendor_office(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.rtf"
            path.write_text(
                r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Arial;}}"
                r"\f0\fs24 customer manager risk management}",
                encoding="ascii",
            )

            adapter = TikaNativeAdapter()
            chunks = list(adapter.iter_chunks(path, target_chars=32))

        text = "\n".join(chunk.content for chunk in chunks).lower()
        self.assertIn("customer manager", text)
        self.assertIn("risk management", text)
        self.assertTrue(all(chunk.location.startswith("内容块 ") for chunk in chunks))

    def test_flat_tika_output_is_generic_docir_not_fake_writer_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.rtf"
            path.write_text(r"{\rtf1\ansi legacy policy text}", encoding="ascii")

            registry = DocumentAdapterRegistry((TikaNativeAdapter(),))
            broker = ContentExtractionBroker(registry)
            blocks = list(broker.iter_blocks(path))

        self.assertTrue(blocks)
        self.assertTrue(all(block.kind == BlockKind.GENERIC for block in blocks))
        self.assertTrue(all(block.locator.page is None for block in blocks))
        self.assertTrue(all(block.locator.slide is None for block in blocks))
        self.assertTrue(all(block.locator.sheet is None for block in blocks))


if __name__ == "__main__":
    unittest.main()
