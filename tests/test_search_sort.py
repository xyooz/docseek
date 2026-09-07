from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.search_db import SearchDatabase
from docseek.search_sort import SORT_FILENAME, SORT_MODIFIED, SORT_RELEVANCE


class SearchSortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.engine = ExactGroupedSearchEngine(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(
        self,
        filename: str,
        modified_time: float,
        *,
        extension: str = ".pdf",
        chunks: list[str] | None = None,
        locations: list[str] | None = None,
    ) -> None:
        chunks = chunks or ["客户经理办理信贷业务"]
        locations = locations or [f"位置 {index + 1}" for index in range(len(chunks))]
        self.store.replace_document(
            path=str(Path("C:/docs") / filename),
            filename=filename,
            extension=extension,
            modified_time=modified_time,
            size=100,
            chunks=[
                DocumentChunk(index, locations[index], content)
                for index, content in enumerate(chunks)
            ],
        )

    def test_modified_sort_orders_keyword_hits_newest_first(self) -> None:
        self._add("alpha.pdf", 10.0)
        self._add("zeta.pdf", 30.0)
        self._add("beta.pdf", 20.0)

        page = self.engine.search_page("信贷", sort_mode=SORT_MODIFIED)

        self.assertEqual(page.total_count, 3)
        self.assertEqual(
            [item.filename for item in page.items],
            ["zeta.pdf", "beta.pdf", "alpha.pdf"],
        )

    def test_filename_sort_is_case_insensitive_and_stable(self) -> None:
        self._add("Zulu.pdf", 30.0)
        self._add("alpha.pdf", 10.0)
        self._add("Beta.pdf", 20.0)

        page = self.engine.search_page("信贷", sort_mode=SORT_FILENAME)

        self.assertEqual(
            [item.filename for item in page.items],
            ["alpha.pdf", "Beta.pdf", "Zulu.pdf"],
        )

    def test_sorted_deep_pagination_has_no_gaps_or_duplicates(self) -> None:
        names = ["g.pdf", "a.pdf", "f.pdf", "b.pdf", "e.pdf", "c.pdf", "d.pdf"]
        for index, name in enumerate(names):
            self._add(name, float(index))

        collected: list[str] = []
        for offset in (0, 2, 4, 6, 8):
            page = self.engine.search_page(
                "信贷", sort_mode=SORT_FILENAME, limit=2, offset=offset
            )
            self.assertEqual(page.total_count, 7)
            collected.extend(item.filename for item in page.items)

        self.assertEqual(
            collected,
            ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf", "f.pdf", "g.pdf"],
        )
        self.assertEqual(len(collected), len(set(collected)))

    def test_file_sort_does_not_change_structure_aware_best_chunk(self) -> None:
        self._add(
            "manual.pdf",
            1.0,
            chunks=["信贷业务", "信贷业务"],
            locations=["第 1 页", "第 2 页"],
        )
        self._add("other.pdf", 2.0, chunks=["信贷业务"])

        page = self.engine.search_page(
            "信贷业务 page:2", sort_mode=SORT_FILENAME
        )
        manual = next(item for item in page.items if item.filename == "manual.pdf")
        self.assertEqual(manual.location, "第 2 页")

    def test_filter_only_filename_sort_uses_same_file_order_contract(self) -> None:
        self._add("c.pdf", 30.0)
        self._add("a.pdf", 10.0)
        self._add("b.pdf", 20.0)
        self._add("ignore.docx", 40.0, extension=".docx")

        page = self.engine.search_page(
            "", extension=".pdf", sort_mode=SORT_FILENAME, limit=2, offset=1
        )

        self.assertEqual(page.total_count, 3)
        self.assertEqual([item.filename for item in page.items], ["b.pdf", "c.pdf"])
        self.assertTrue(all(item.snippet == "" for item in page.items))

    def test_relevance_remains_default_sort_mode(self) -> None:
        self._add("ordinary.pdf", 30.0, chunks=["信贷"])
        self._add("信贷.pdf", 10.0, chunks=["普通内容", "信贷"])

        implicit = self.engine.search_page("信贷")
        explicit = self.engine.search_page("信贷", sort_mode=SORT_RELEVANCE)
        self.assertEqual(
            [(item.path, item.location, item.score) for item in implicit.items],
            [(item.path, item.location, item.score) for item in explicit.items],
        )

    def test_unknown_sort_mode_is_rejected(self) -> None:
        self._add("manual.pdf", 1.0)
        with self.assertRaises(ValueError):
            self.engine.search_page("信贷", sort_mode="raw sql please")


if __name__ == "__main__":
    unittest.main()
