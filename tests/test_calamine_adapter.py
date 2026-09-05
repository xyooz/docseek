from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from docseek.document_adapters import CalamineSpreadsheetAdapter


@unittest.skipUnless(
    CalamineSpreadsheetAdapter().is_available(),
    "python-calamine optional dependency is not installed",
)
class CalamineSpreadsheetAdapterTests(unittest.TestCase):
    def test_streams_rows_and_preserves_interior_empty_cells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "客户清单.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "客户明细"
            sheet.append(["客户号", "姓名", "风险等级"])
            sheet.append(["001", None, "高风险"])
            workbook.save(path)
            workbook.close()

            progress: list[tuple[str, int]] = []
            adapter = CalamineSpreadsheetAdapter(rows_per_chunk=200)
            chunks = list(
                adapter.iter_chunks(
                    path,
                    on_progress=lambda label, row: progress.append((label, row)),
                )
            )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].location, "工作表 客户明细 · 行 1-2")
        self.assertIn("客户号\t姓名\t风险等级", chunks[0].content)
        self.assertIn("001\t\t高风险", chunks[0].content)
        self.assertEqual(progress[-1], ("工作表 客户明细", 2))


if __name__ == "__main__":
    unittest.main()
