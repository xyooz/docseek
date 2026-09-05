from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from .index_issues_dialog import IndexIssuesDialog


class RetryableIndexIssuesDialog(IndexIssuesDialog):
    """Index-problem viewer that can return selected paths for safe retry."""

    def __init__(self, store, parent=None) -> None:
        super().__init__(store, parent)
        self.retry_paths: list[str] = []
        self.setWindowTitle("索引问题与重试")

        self.retry_hint = QLabel(
            "修复文件、权限或大小限制后可直接重试。暂停目录不会被重试操作自动恢复。"
        )
        self.retry_hint.setWordWrap(True)
        self.retry_hint.setStyleSheet("color: palette(mid);")

        self.retry_selected_button = QPushButton("重试选中")
        issue_count = self.store.count()
        self.retry_all_button = QPushButton(
            f"重试全部 ({issue_count})" if issue_count else "重试全部"
        )
        self.retry_selected_button.setEnabled(False)
        self.retry_all_button.setEnabled(issue_count > 0)

        self.retry_selected_button.clicked.connect(self._retry_selected)
        self.retry_all_button.clicked.connect(self._retry_all)
        self.table.itemSelectionChanged.connect(self._refresh_retry_selection)

        retry_row = QHBoxLayout()
        retry_row.addWidget(self.retry_selected_button)
        retry_row.addWidget(self.retry_all_button)
        retry_row.addStretch(1)

        # Keep the inherited close button last. This avoids duplicating the
        # established issue table/viewer while still making retry a first-class
        # action in the dialog.
        layout = self.layout()
        insert_at = max(0, layout.count() - 1)
        layout.insertWidget(insert_at, self.retry_hint)
        layout.insertLayout(insert_at + 1, retry_row)

    def _refresh_retry_selection(self) -> None:
        self.retry_selected_button.setEnabled(self._selected_path() is not None)

    def _retry_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self.retry_paths = [path]
        self.accept()

    def _retry_all(self) -> None:
        self.retry_paths = [issue.path for issue in self.store.list(limit=1000)]
        if self.retry_paths:
            self.accept()
