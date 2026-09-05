from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


def _bigram_phrase(store: ChunkStore, text: str) -> str:
    tokens = store._cjk_bigrams(text).split()
    escaped = " ".join(token.replace('"', '""') for token in tokens)
    return f'"{escaped}"'


class CjkBigramPhraseTests(unittest.TestCase):
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

    def _matched_files(self, query: str) -> list[str]:
        phrase = _bigram_phrase(self.store, query)
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT f.filename
                FROM chunk_index_cjk2 idx
                JOIN chunks c ON c.id = idx.rowid
                JOIN files f ON f.id = c.file_id
                WHERE chunk_index_cjk2 MATCH ?
                ORDER BY f.filename
                """,
                (phrase,),
            ).fetchall()
        return [str(row["filename"]) for row in rows]

    def test_long_contiguous_chinese_substring_matches(self) -> None:
        self._add("credit.txt", "农业银行客户经理办理信贷业务")
        self.assertEqual(self._matched_files("客户经理"), ["credit.txt"])
        self.assertEqual(self._matched_files("经理办理信贷"), ["credit.txt"])

    def test_long_query_does_not_match_non_contiguous_text(self) -> None:
        self._add("contiguous.txt", "客户经理")
        self._add("separated.txt", "客户与经理")
        self.assertEqual(self._matched_files("客户经理"), ["contiguous.txt"])

    def test_longer_business_phrase_matches_as_overlapping_bigrams(self) -> None:
        self._add("identity.txt", "请及时更新身份证有效期并确认预留手机号")
        self.assertEqual(self._matched_files("身份证有效期"), ["identity.txt"])


if __name__ == "__main__":
    unittest.main()
