from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout


def search_help_html() -> str:
    """Return concise, user-facing help for DocSeek's current search surface."""
    return """
    <h2>DocSeek 搜索帮助</h2>
    <p>直接输入你记得的正文内容即可搜索，不需要记住文件名。多个关键词会一起参与匹配。</p>

    <h3>常用搜索</h3>
    <table cellspacing="5" cellpadding="2">
      <tr><td><code>信贷 客户经理</code></td><td>搜索同时包含这些关键词的文档</td></tr>
      <tr><td><code>&quot;身份证有效期&quot;</code></td><td>按连续短语搜索</td></tr>
      <tr><td><code>信贷 ext:pdf</code></td><td>只看 PDF</td></tr>
      <tr><td><code>制度 path:&quot;业务 文件&quot;</code></td><td>限定文件路径</td></tr>
      <tr><td><code>通知 after:2026-01-01</code></td><td>只看指定日期之后修改的文件</td></tr>
      <tr><td><code>报表 size:&gt;10MB</code></td><td>按文件大小筛选</td></tr>
    </table>

    <h3>结构定位</h3>
    <p>这些是可选的精确提示；普通搜索不写也可以使用标题、工作表名等结构信号排序。</p>
    <table cellspacing="5" cellpadding="2">
      <tr><td><code>信贷 page:12</code></td><td>优先 PDF 第 12 页</td></tr>
      <tr><td><code>年度总结 slide:4</code></td><td>优先 PowerPoint 第 4 页</td></tr>
      <tr><td><code>客户 sheet:&quot;客户 数据&quot;</code></td><td>优先指定 Excel 工作表</td></tr>
    </table>

    <h3>筛选、排序与常用搜索</h3>
    <ul>
      <li>搜索框右侧的类型下拉框可以快速限定 PDF、Word、Excel 等格式。</li>
      <li>排序可切换为“相关性”“最近修改”或“文件名”。</li>
      <li>有效的高级筛选会显示成可点击移除的筛选 Chips。</li>
      <li>“历史”保存最近成功搜索；“收藏”会记住当前查询、类型筛选和排序方式。</li>
    </ul>

    <h3>快捷键</h3>
    <table cellspacing="5" cellpadding="2">
      <tr><td><b>Ctrl+L</b></td><td>聚焦搜索框</td></tr>
      <tr><td><b>↑ / ↓</b></td><td>从搜索框进入结果并上下选择</td></tr>
      <tr><td><b>Enter</b></td><td>立即搜索；在结果中打开选中文件</td></tr>
      <tr><td><b>Esc</b></td><td>清空搜索和显式类型筛选</td></tr>
      <tr><td><b>Ctrl+O</b></td><td>打开选中文件</td></tr>
      <tr><td><b>Ctrl+Shift+O</b></td><td>在资源管理器中定位选中文件</td></tr>
      <tr><td><b>F1</b></td><td>打开本帮助</td></tr>
    </table>

    <p style="color:#666; margin-top:14px;">提示：筛选条件也可以单独使用，例如只输入 <code>ext:pdf after:2026-01-01</code> 浏览符合条件的文件。</p>
    """.strip()


class SearchHelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DocSeek 搜索帮助")
        self.resize(720, 620)

        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(False)
        browser.setHtml(search_help_html())

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(browser, 1)
        layout.addWidget(buttons)
