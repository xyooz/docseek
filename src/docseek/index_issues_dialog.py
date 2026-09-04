from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from PySide6.QtCore import Qt

from .index_issues import IndexIssueStore, issue_label


class IndexIssuesDialog(QDialog):
    def __init__(self, store: IndexIssueStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("索引问题")
        self.resize(900, 520)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["问题", "文件 / 目录", "详情", "最近发生"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._reveal_selected())

        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh)

        button_row = QHBoxLayout()
        button_row.addWidget(refresh_button)
        button_row.addStretch(1)

        close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("这些文件没有被正常建立索引。修复权限、文件格式或大小限制后，重新索引即可自动清除。"))
        layout.addWidget(self.summary_label)
        layout.addLayout(button_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(close_buttons)
        self.setLayout(layout)

        self.refresh()

    def refresh(self) -> None:
        issues = self.store.list(limit=1000)
        summary = self.store.summary()
        if not issues:
            self.summary_label.setText("当前没有索引问题。")
        else:
            parts = [f"{issue_label(code)} {count}" for code, count in sorted(summary.items())]
            self.summary_label.setText(f"共 {len(issues)} 项：" + " · ".join(parts))

        self.table.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            issue_item = QTableWidgetItem(issue_label(issue.error_code))
            path_item = QTableWidgetItem(issue.path)
            path_item.setData(Qt.UserRole, issue.path)
            detail_item = QTableWidgetItem(issue.detail)
            time_item = QTableWidgetItem(
                datetime.fromtimestamp(issue.updated_at).strftime("%Y-%m-%d %H:%M")
            )
            self.table.setItem(row, 0, issue_item)
            self.table.setItem(row, 1, path_item)
            self.table.setItem(row, 2, detail_item)
            self.table.setItem(row, 3, time_item)

    def _selected_path(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 1)
        return str(item.data(Qt.UserRole)) if item else None

    def _reveal_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        target = Path(path)
        try:
            if target.exists() and target.is_file():
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif target.exists():
                os.startfile(path)  # type: ignore[attr-defined]
            elif target.parent.exists():
                os.startfile(str(target.parent))  # type: ignore[attr-defined]
        except OSError:
            return

    def _copy_selected_path(self) -> None:
        path = self._selected_path()
        if path:
            QGuiApplication.clipboard().setText(path)

    def _show_context_menu(self, point) -> None:
        if self.table.currentRow() < 0:
            return
        menu = QMenu(self)
        menu.addAction("打开所在位置", self._reveal_selected)
        menu.addAction("复制路径", self._copy_selected_path)
        menu.exec(self.table.viewport().mapToGlobal(point))
