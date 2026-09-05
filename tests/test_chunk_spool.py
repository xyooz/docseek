from __future__ import annotations

import unittest

from docseek.chunk_spool import DocumentChunkSpool
from docseek.chunks import DocumentChunk


class DocumentChunkSpoolTests(unittest.TestCase):
    def test_replays_chunks_after_rolling_past_memory_limit(self) -> None:
        expected = [
            DocumentChunk(0, "工作表 A · 行 1-2", "第一块正文" * 100),
            DocumentChunk(1, "工作表 A · 行 3-4", "第二块正文" * 100),
        ]
        with DocumentChunkSpool(max_memory_bytes=64) as spool:
            self.assertEqual(spool.capture(expected), 2)
            self.assertEqual(list(spool.iter_chunks()), expected)
            self.assertEqual(list(spool.iter_chunks()), expected)


if __name__ == "__main__":
    unittest.main()
