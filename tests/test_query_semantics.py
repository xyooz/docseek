from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.query_parser import parse_query
from docseek.search_db import SearchDatabase


class QuerySemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "docseek.db"
        SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add(self, name: str, content: str) -> None:
        path = str(Path("C:/docs") / name)
        self.store.replace_document(
            path=path,
            filename=name,
            extension=".txt",
            modified_time=1.0,
            size=len(content.encode("utf-8")),
            chunks=[DocumentChunk(0, "行 1", content)],
        )

    def test_separate_chinese_terms_use_and_not_contiguous_phrase(self) -> None:
        self._add("separated.txt", "客户需要由资深经理跟进")
        self._add("missing.txt", "这里只有客户信息")
        rows = self.store.search("客户 经理")
        self.assertEqual([row.filename for row in rows], ["separated.txt"])

    def test_contiguous_chinese_term_still_requires_contiguous_substring(self) -> None:
        self._add("contiguous.txt", "客户经理负责办理业务")
        self._add("separated.txt", "客户需要由经理办理业务")
        rows = self.store.search("客户经理")
        self.assertEqual([row.filename for row in rows], ["contiguous.txt"])

    def test_quoted_english_phrase_does_not_match_separated_words(self) -> None:
        self._add("phrase.txt", "the customer manager handbook")
        self._add("separated.txt", "customer service notes for a branch manager")
        parsed = parse_query('"customer manager"')
        rows = self.store.search(parsed.text)
        self.assertEqual([row.filename for row in rows], ["phrase.txt"])

    def test_unquoted_english_terms_match_when_separated(self) -> None:
        self._add("phrase.txt", "the customer manager handbook")
        self._add("separated.txt", "customer service notes for a branch manager")
        parsed = parse_query("customer manager")
        rows = self.store.search(parsed.text)
        self.assertEqual({row.filename for row in rows}, {"phrase.txt", "separated.txt"})


if __name__ == "__main__":
    unittest.main()
