from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.search_db import SearchDatabase


class ExactGroupedSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.grouped = ExactGroupedSearchEngine(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(
        self,
        index: int,
        *,
        filename: str | None = None,
        extension: str = ".pdf",
        chunks: list[str],
        locations: list[str] | None = None,
        modified_time: float | None = None,
        size: int | None = None,
        folder: str = "docs",
    ) -> None:
        filename = filename or f"doc_{index:03d}{extension}"
        path = str(Path("C:/") / folder / filename)
        if locations is None:
            locations = [f"位置 {chunk_no + 1}" for chunk_no in range(len(chunks))]
        self.assertEqual(len(locations), len(chunks))
        self.store.replace_document(
            path=path,
            filename=filename,
            extension=extension,
            modified_time=float(index if modified_time is None else modified_time),
            size=size if size is not None else 1000 + index,
            chunks=[
                DocumentChunk(chunk_no, locations[chunk_no], content)
                for chunk_no, content in enumerate(chunks)
            ],
        )

    @staticmethod
    def _signature(page) -> list[tuple[str, str, float]]:
        return [(row.path, row.location, row.score) for row in page.items]

    def test_grouped_engine_matches_exact_order_and_best_chunk(self) -> None:
        self._add(0, chunks=["信贷", "信贷 信贷 信贷"])
        self._add(1, chunks=["普通说明", "客户经理 信贷业务"])
        self._add(2, filename="信贷.pdf", chunks=["普通内容", "信贷"])
        self._add(3, chunks=["客户经理", "信贷业务 信贷业务"])

        expected = self.store.search_page("信贷", limit=10)
        actual = self.grouped.search_page("信贷", limit=10)
        self.assertEqual(actual.total_count, expected.total_count)
        self.assertEqual(self._signature(actual), self._signature(expected))

    def test_grouped_engine_matches_deep_file_pagination(self) -> None:
        for index in range(17):
            self._add(
                index,
                chunks=["客户经理办理信贷业务"] * (1 + index % 5),
                modified_time=float(index),
            )

        for offset in (0, 5, 10, 15, 20):
            expected = self.store.search_page("客户经理", limit=5, offset=offset)
            actual = self.grouped.search_page("客户经理", limit=5, offset=offset)
            self.assertEqual(actual.total_count, expected.total_count)
            self.assertEqual(self._signature(actual), self._signature(expected))

    def test_grouped_engine_matches_filters_and_multiterm_semantics(self) -> None:
        self._add(0, chunks=["客户信息由经理负责"], folder="制度", size=100)
        self._add(1, chunks=["客户信息由经理负责"], folder="培训", size=500)
        self._add(2, extension=".docx", chunks=["客户信息由经理负责"], folder="制度", size=900)
        self._add(3, chunks=["只有客户信息"], folder="制度", size=1300)

        kwargs = {
            "extension": ".pdf",
            "path_contains": "制度",
            "min_size": 50,
            "max_size": 600,
        }
        expected = self.store.search_page("客户 经理", **kwargs)
        actual = self.grouped.search_page("客户 经理", **kwargs)
        self.assertEqual(actual.total_count, expected.total_count)
        self.assertEqual(self._signature(actual), self._signature(expected))

    def test_grouped_engine_matches_quoted_english_phrase(self) -> None:
        self._add(0, chunks=["customer manager handbook"])
        self._add(1, chunks=["customer service notes for branch manager"])
        expected = self.store.search_page('"customer manager"')
        actual = self.grouped.search_page('"customer manager"')
        self.assertEqual(self._signature(actual), self._signature(expected))

    def test_page_hint_prefers_matching_pdf_page(self) -> None:
        self._add(
            0,
            chunks=["信贷业务", "信贷业务"],
            locations=["第 1 页", "第 2 页"],
        )
        plain = self.grouped.search_page("信贷业务")
        hinted = self.grouped.search_page("信贷业务 page:2")
        self.assertEqual(plain.items[0].location, "第 1 页")
        self.assertEqual(hinted.items[0].location, "第 2 页")
        self.assertNotIn("page:2", hinted.items[0].snippet)

    def test_slide_hint_prefers_matching_slide(self) -> None:
        self._add(
            0,
            extension=".pptx",
            chunks=["风险管理", "风险管理"],
            locations=["幻灯片 1", "幻灯片 4"],
        )
        page = self.grouped.search_page("风险管理 slide:4")
        self.assertEqual(page.items[0].location, "幻灯片 4")

    def test_sheet_hint_prefers_matching_sheet(self) -> None:
        self._add(
            0,
            extension=".xlsx",
            chunks=["客户经理汇总", "客户经理汇总"],
            locations=["工作表 汇总 · 行 1-20", "工作表 客户 数据 · 行 1-20"],
        )
        page = self.grouped.search_page('客户经理 sheet:"客户 数据"')
        self.assertEqual(page.items[0].location, "工作表 客户 数据 · 行 1-20")

    def test_structure_hint_does_not_change_file_count(self) -> None:
        for index in range(4):
            self._add(
                index,
                chunks=["信贷业务", "信贷业务"],
                locations=["第 1 页", "第 2 页"],
            )
        self.assertEqual(self.grouped.count_files("信贷业务 page:2"), 4)
        self.assertEqual(self.grouped.search_page("信贷业务 page:2").total_count, 4)

    def test_count_matches_exact_file_total(self) -> None:
        for index in range(8):
            self._add(index, chunks=["信贷业务"] * (index % 3 + 1))
        self._add(99, chunks=["完全无关"])
        self.assertEqual(self.grouped.count_files("信贷"), 8)
        self.assertEqual(self.grouped.search_page("信贷", limit=3).total_count, 8)


if __name__ == "__main__":
    unittest.main()
