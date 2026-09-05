from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from docseek.chunks import iter_document_chunks


class DocumentChunkExtractionTests(unittest.TestCase):
    def test_xlsx_preserves_interior_empty_cells_and_sheet_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "客户清单.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "客户明细"
            worksheet.append(["客户号", "姓名", "风险等级"])
            worksheet.append(["001", None, "高风险"])
            worksheet.append([None, None, None])
            worksheet.append([None, "张三", "正常"])
            workbook.save(path)
            workbook.close()

            chunks = list(iter_document_chunks(path, xlsx_rows_per_chunk=20))

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.location, "工作表 客户明细 · 行 1-4")
        self.assertIn("工作表: 客户明细", chunk.content)
        self.assertIn("客户号\t姓名\t风险等级", chunk.content)
        self.assertIn("001\t\t高风险", chunk.content)
        self.assertIn("\n\t张三\t正常", chunk.content)
        self.assertNotIn("\n\n\n", chunk.content)

    def test_xlsx_repeats_sheet_context_for_each_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "分块.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "逾期清单"
            worksheet.append(["客户", "金额"])
            worksheet.append(["A", 100])
            worksheet.append(["B", 200])
            workbook.save(path)
            workbook.close()

            chunks = list(iter_document_chunks(path, xlsx_rows_per_chunk=1))

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(chunk.content.startswith("工作表: 逾期清单\n") for chunk in chunks))
        self.assertEqual(
            [chunk.location for chunk in chunks],
            [
                "工作表 逾期清单 · 行 1-1",
                "工作表 逾期清单 · 行 2-2",
                "工作表 逾期清单 · 行 3-3",
            ],
        )

    def test_xlsx_progress_is_throttled_and_reports_final_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "大表.xlsx"
            workbook = Workbook(write_only=True)
            worksheet = workbook.create_sheet("客户明细")
            for row_no in range(1, 2_506):
                worksheet.append([row_no, f"客户{row_no}"])
            workbook.save(path)
            workbook.close()

            progress: list[tuple[str, int]] = []
            list(
                iter_document_chunks(
                    path,
                    xlsx_rows_per_chunk=200,
                    on_progress=lambda location, current: progress.append((location, current)),
                )
            )

        self.assertEqual(
            progress,
            [
                ("工作表 客户明细", 1_000),
                ("工作表 客户明细", 2_000),
                ("工作表 客户明细", 2_505),
            ],
        )


if __name__ == "__main__":
    unittest.main()
