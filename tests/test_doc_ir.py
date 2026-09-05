from __future__ import annotations

import unittest
from pathlib import Path

from docseek.chunks import DocumentChunk
from docseek.doc_ir import BlockKind, block_from_chunk
from docseek.document_types import DocumentFamily


class DocumentIRTests(unittest.TestCase):
    def test_spreadsheet_location_becomes_structured_locator_without_text_change(self) -> None:
        chunk = DocumentChunk(
            3,
            "工作表 客户明细 · 行 201-400",
            "工作表: 客户明细\n001\t张三\t高风险",
        )

        block = block_from_chunk(Path("客户清单.xlsx"), DocumentFamily.SPREADSHEET, chunk)

        self.assertEqual(block.kind, BlockKind.SHEET_ROWS)
        self.assertEqual(block.locator.sheet, "客户明细")
        self.assertEqual(block.locator.row_start, 201)
        self.assertEqual(block.locator.row_end, 400)
        self.assertEqual(block.title, "客户明细")
        self.assertEqual(block.as_chunk(), chunk)

    def test_pdf_and_presentation_locations_are_structured(self) -> None:
        pdf = block_from_chunk(
            Path("制度.pdf"),
            DocumentFamily.PDF,
            DocumentChunk(0, "第 28 页", "客户经理管理要求"),
        )
        slide = block_from_chunk(
            Path("培训.pptx"),
            DocumentFamily.PRESENTATION,
            DocumentChunk(16, "幻灯片 17 · 标题 风险管理", "风险管理"),
        )

        self.assertEqual(pdf.kind, BlockKind.PAGE)
        self.assertEqual(pdf.locator.page, 28)
        self.assertEqual(slide.kind, BlockKind.SLIDE)
        self.assertEqual(slide.locator.slide, 17)
        self.assertEqual(slide.title, "风险管理")

    def test_text_and_writer_ranges_are_kept(self) -> None:
        text = block_from_chunk(
            Path("notes.txt"),
            DocumentFamily.TEXT,
            DocumentChunk(1, "行 101-180", "正文"),
        )
        writer = block_from_chunk(
            Path("制度.docx"),
            DocumentFamily.WRITER,
            DocumentChunk(2, "文档块 8-15 · 标题 客户经理管理", "正文"),
        )

        self.assertEqual((text.locator.line_start, text.locator.line_end), (101, 180))
        self.assertEqual((writer.locator.block_start, writer.locator.block_end), (8, 15))
        self.assertEqual(writer.title, "客户经理管理")


if __name__ == "__main__":
    unittest.main()