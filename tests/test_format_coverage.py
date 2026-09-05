from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.document_types import (
    KNOWN_DOCUMENT_EXTENSIONS,
    TIKA_NATIVE_CANDIDATE_EXTENSIONS,
)
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


class FormatCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "docs"
        self.root.mkdir()
        self.db = SearchDatabase(self.base / "docseek.db")
        self.store = ChunkStore(self.db.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _assert_indexed(self, filename: str, content: str, query: str) -> None:
        target = self.root / filename
        target.write_text(content, encoding="utf-8")
        stats = DirectoryIndexer(self.db).scan(self.root)
        self.assertGreaterEqual(stats.indexed, 1, filename)
        matches = self.store.search(query)
        self.assertTrue(matches, filename)
        self.assertIn(filename, [match.filename for match in matches])

    def test_html_content_is_indexed_through_tika(self) -> None:
        self._assert_indexed(
            "业务说明.html",
            "<html><body><h1>电子渠道</h1><p>跨境汇款操作指引</p></body></html>",
            "跨境汇款",
        )

    def test_xml_content_is_indexed_through_tika(self) -> None:
        self._assert_indexed(
            "配置资料.xml",
            "<?xml version='1.0' encoding='utf-8'?><root><item>客户风险等级维护</item></root>",
            "风险等级",
        )

    def test_tsv_content_is_indexed_through_tika(self) -> None:
        self._assert_indexed(
            "客户清单.tsv",
            "客户号\t状态\n10001\t待核验\n10002\t正常\n",
            "待核验",
        )

    def test_eml_body_is_indexed_through_tika(self) -> None:
        self._assert_indexed(
            "通知.eml",
            "From: sender@example.com\n"
            "To: receiver@example.com\n"
            "Subject: 网点通知\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "Content-Transfer-Encoding: 8bit\n"
            "\n"
            "请核对客户证件有效期并完成更新。\n",
            "证件有效期",
        )

    def test_broad_registered_compatibility_formats(self) -> None:
        expected = {
            ".html",
            ".htm",
            ".xhtml",
            ".xml",
            ".tsv",
            ".epub",
            ".eml",
            ".msg",
            ".mbox",
            ".pst",
            ".pages",
            ".numbers",
            ".key",
        }
        self.assertTrue(expected <= KNOWN_DOCUMENT_EXTENSIONS)
        self.assertTrue(expected <= TIKA_NATIVE_CANDIDATE_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
