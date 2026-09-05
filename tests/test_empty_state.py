from __future__ import annotations

import unittest

from docseek.app import empty_result_html


class EmptyStateTests(unittest.TestCase):
    def test_keyword_no_match_guidance_is_actionable(self) -> None:
        message = empty_result_html(filter_only=False)
        self.assertIn("没有找到匹配文档", message)
        self.assertIn("减少关键词", message)
        self.assertIn("引号短语", message)
        self.assertIn("筛选条件", message)

    def test_filter_only_no_match_guidance_focuses_on_filters(self) -> None:
        message = empty_result_html(filter_only=True)
        self.assertIn("当前筛选没有匹配文件", message)
        self.assertIn("移除上方筛选条件", message)
        self.assertIn("文件类型", message)
        self.assertIn("日期", message)
        self.assertIn("路径", message)
        self.assertIn("大小", message)


if __name__ == "__main__":
    unittest.main()
