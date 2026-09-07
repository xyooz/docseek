from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from docseek.chunks import DocumentChunk, iter_document_chunks
from docseek.document_adapters import AdapterUnavailable, DocumentAdapterRegistry
from docseek.document_types import DocumentFamily, SupportMode, get_format_capability
from docseek.extraction_broker import ContentExtractionBroker


@dataclass(slots=True, frozen=True)
class FakeAdapter:
    name: str
    priority: int
    extensions: frozenset[str]
    available: bool = True

    def is_available(self) -> bool:
        return self.available

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress=None,
    ) -> Iterator[DocumentChunk]:
        del path, target_chars, spreadsheet_rows_per_chunk, on_progress
        yield DocumentChunk(0, "工作表 客户明细 · 行 1-2", "工作表: 客户明细\n客户号\t姓名")


class FakeBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int, int]] = []

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int,
        spreadsheet_rows_per_chunk: int,
        on_progress=None,
    ) -> Iterator[DocumentChunk]:
        del on_progress
        self.calls.append((path, target_chars, spreadsheet_rows_per_chunk))
        yield DocumentChunk(0, "文档块 1-1", "broker-routed")


class ExtractionBrokerTests(unittest.TestCase):
    def test_format_capabilities_keep_mature_backend_preferences(self) -> None:
        xlsx = get_format_capability(".xlsx")
        xls = get_format_capability(".xls")
        doc = get_format_capability(".doc")
        wps = get_format_capability(".wps")

        self.assertEqual(xlsx.modes, (SupportMode.DIRECT,))
        self.assertEqual(
            xls.modes,
            (SupportMode.CALAMINE, SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        )
        self.assertEqual(doc.modes, (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL))
        self.assertEqual(wps.modes, (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL))

    def test_registry_prefers_higher_priority_available_adapter(self) -> None:
        slow = FakeAdapter("fallback", 10, frozenset({".xls"}))
        fast = FakeAdapter("native-rust", 90, frozenset({".xls"}))
        registry = DocumentAdapterRegistry((slow, fast))

        self.assertEqual(registry.adapter_for(Path("book.xls")).name, "native-rust")

    def test_registry_skips_unavailable_candidate(self) -> None:
        first = FakeAdapter("first", 100, frozenset({".xls"}), available=False)
        second = FakeAdapter("second", 80, frozenset({".xls"}), available=True)
        registry = DocumentAdapterRegistry((first, second))

        self.assertEqual(registry.adapter_for(Path("book.xls")).name, "second")

    def test_broker_lifts_adapter_chunks_into_structured_ir(self) -> None:
        registry = DocumentAdapterRegistry(
            (FakeAdapter("sheet", 100, frozenset({".xlsx"})),)
        )
        broker = ContentExtractionBroker(registry)

        decision = broker.decision_for(Path("客户.xlsx"))
        blocks = list(broker.iter_blocks(Path("客户.xlsx")))

        self.assertEqual(decision.family, DocumentFamily.SPREADSHEET)
        self.assertEqual(decision.adapter_name, "sheet")
        self.assertEqual(blocks[0].locator.sheet, "客户明细")
        self.assertEqual(blocks[0].as_chunk().content, "工作表: 客户明细\n客户号\t姓名")

    def test_known_format_without_available_adapter_is_explicit_error(self) -> None:
        registry = DocumentAdapterRegistry(
            (FakeAdapter("missing", 100, frozenset({".wps"}), available=False),)
        )
        broker = ContentExtractionBroker(registry)

        self.assertTrue(broker.is_known_path(Path("制度.wps")))
        with self.assertRaises(AdapterUnavailable):
            broker.decision_for(Path("制度.wps"))

    def test_public_chunk_entrypoint_always_routes_through_broker(self) -> None:
        broker = FakeBroker()
        path = Path("制度.docx")

        with patch("docseek.extraction_broker.DEFAULT_EXTRACTION_BROKER", broker):
            chunks = list(
                iter_document_chunks(
                    path,
                    target_chars=4321,
                    xlsx_rows_per_chunk=77,
                )
            )

        self.assertEqual(chunks[0].content, "broker-routed")
        self.assertEqual(broker.calls, [(path, 4321, 77)])


if __name__ == "__main__":
    unittest.main()
