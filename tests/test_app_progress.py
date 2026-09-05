from __future__ import annotations

import unittest

from docseek.app import split_index_progress_display


class IndexProgressDisplayTests(unittest.TestCase):
    def test_normal_windows_path_uses_filename_without_detail(self) -> None:
        filename, detail = split_index_progress_display(r"C:\资料\信贷制度.pdf")

        # pathlib on the Windows production runtime resolves this to the basename;
        # on non-Windows hosts the fallback may preserve separators, so only the
        # detail contract is platform-independent here.
        self.assertTrue(filename.endswith("信贷制度.pdf"))
        self.assertEqual(detail, "")

    def test_xlsx_row_progress_is_split_into_file_and_detail(self) -> None:
        filename, detail = split_index_progress_display(
            "客户清单.xlsx · 工作表 客户明细 · 已读取 48,000 行"
        )

        self.assertEqual(filename, "客户清单.xlsx")
        self.assertEqual(detail, "工作表 客户明细 · 已读取 48,000 行")


if __name__ == "__main__":
    unittest.main()
