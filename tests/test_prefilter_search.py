from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.prefilter_search import MetadataPrefilterSearchEngine
from docseek.search_db import SearchDatabase


class MetadataPrefilterSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)
        self.engine = MetadataPrefilterSearchEngine(self.store)

        for index in range(24):
            extension = ".pdf" if index % 3 == 0 else ".docx"
            folder = "制度" if index % 4 == 0 else "培训"
            path = fr"C:\{folder}\file_{index:02d}{extension}"
            chunks = [
                DocumentChunk(0, "块 1", f"客户经理 信贷业务 文件 {index}"),
                DocumentChunk(1, "块 2", "普通办公说明"),
            ]
            self.store.replace_document(
                path=path,
                filename=f"file_{index:02d}{extension}",
                extension=extension,
                modified_time=float(index),
                size=(index + 1) * 1024,
                chunks=chunks,
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _signature(page) -> list[tuple[str, str, float]]:
        return [(row.path, row.location, row.score) for row in page.items]

    def test_prefilter_matches_production_exact_search(self) -> None:
        kwargs = dict(
            extension=".pdf",
            path_contains="制度",
            modified_after=5.0,
            min_size=5 * 1024,
        )
        expected = self.store.search_page("客户经理", limit=10, **kwargs)
        actual = self.engine.search_page("客户经理", limit=10, **kwargs)

        self.assertEqual(actual.total_count, expected.total_count)
        self.assertEqual(self._signature(actual), self._signature(expected))

    def test_prefilter_matches_deep_pagination(self) -> None:
        kwargs = dict(extension=".docx", modified_after=2.0, max_size=24 * 1024)
        expected = self.store.search_page("信贷业务", limit=4, offset=4, **kwargs)
        actual = self.engine.search_page("信贷业务", limit=4, offset=4, **kwargs)

        self.assertEqual(actual.total_count, expected.total_count)
        self.assertEqual(self._signature(actual), self._signature(expected))

    def test_without_metadata_filter_falls_back_to_production_path(self) -> None:
        expected = self.store.search_page("客户经理", limit=7)
        actual = self.engine.search_page("客户经理", limit=7)
        self.assertEqual(actual.total_count, expected.total_count)
        self.assertEqual(self._signature(actual), self._signature(expected))


if __name__ == "__main__":
    unittest.main()
