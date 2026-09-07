from __future__ import annotations

import io
import unittest

from docseek.chunks import _iter_text_chunks


class _NoRewindPath:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.open_count = 0
        self.stream: io.BytesIO | None = None

    def open(self, mode: str) -> io.BytesIO:
        if mode != "rb":
            raise AssertionError(f"unexpected mode: {mode}")
        self.open_count += 1
        outer = self

        class _Stream(io.BytesIO):
            def seek(self, *args, **kwargs):
                raise AssertionError("small text path must not rewind")

        self.stream = _Stream(self.payload)
        return self.stream


class SmallTextChunkFastPathTests(unittest.TestCase):
    def test_crlf_multichunk_keeps_previous_line_boundaries(self) -> None:
        path = _NoRewindPath(b"aa\r\nbb\r\ncc\r\n")

        chunks = list(_iter_text_chunks(path, target_chars=4))  # type: ignore[arg-type]

        self.assertEqual(path.open_count, 1)
        self.assertEqual(
            [(chunk.location, chunk.content) for chunk in chunks],
            [("行 1-2", "aa\nbb"), ("行 3-3", "cc")],
        )

    def test_single_chunk_fast_path_preserves_blank_lines_and_location(self) -> None:
        path = _NoRewindPath("第一行\r\n\r\n第三行\r\n".encode("utf-8"))

        chunks = list(_iter_text_chunks(path, target_chars=12_000))  # type: ignore[arg-type]

        self.assertEqual(path.open_count, 1)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].location, "行 1-3")
        self.assertEqual(chunks[0].content, "第一行\n\n第三行")


if __name__ == "__main__":
    unittest.main()
