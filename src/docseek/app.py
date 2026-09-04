from __future__ import annotations

import html
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .indexer import DirectoryIndexer, IndexCancelled, IndexStats
from .search_db import SearchDatabase, SearchResult


APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"


class IndexSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)


class IndexWorker(QRunnable):
    def __init__(self, root: Path, db_path: Path) -> None:
        super().__init__()
        self.root = root
        self.db_path = db_path
        self.signals = IndexSignals()
        self.indexer: DirectoryIndexer | None = None

    def cancel(self) -> None:
        if self.indexer:
            self.indexer.cancel()

    def run(self) -> None:
        try:
            database = SearchDatabase(self.db_path)
            self.indexer = DirectoryIndexer(database)

            def report(path: Path, stats: IndexStats) -> None:
                self.signals.progress.emit(str(path), stats.scanned, stats.indexed)

            stats = self.indexer.scan(self.root, on_progress=report)
            self.signals.finished.emit(stats)
        except IndexCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DocSeek — 本地文档全文检索")
        self.resize(1180, 760)

        self.database = SearchDatabase(DB_PATH)
        self.thread_pool = QThreadPool.globalInstance()
        self.current_worker: IndexWorker | None = None
        self.current_results: list[SearchResult] = []

        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText("搜索文件名或正文，例如：客户经理 信贷")
        self.search_input.setMinimumHeight(38)

        self.choose_button = QPushButton("索引目录")
        self.choose_button.setMinimumHeight(38)
        self.cancel_button = QPushButton("停止索引")
        self.cancel_button.setMinimumHeight(38)
        self.cancel_button.setVisible(False)

        self.scope_label = QLabel()
        self.scope_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["文件名", "类型", "修改时间", "路径"])
        self.results.setSelectionBehavior(QTableWidget.SelectRows)
        self.results.setSelectionMode(QTableWidget.SingleSelection)
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results.setSortingEnabled(False)
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("选择一条结果，这里会显示命中的正文上下文。")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.results)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 460])

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_input, 1)
        top_bar.addWidget(self.choose_button)
        top_bar.addWidget(self.cancel_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(top_bar)
        layout.addWidget(self.scope_label)
        layout.addWidget(splitter, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self._perform_search)

        self.search_input.textChanged.connect(self.search_timer.start)
        self.search_input.returnPressed.connect(self._perform_search)
        self.choose_button.clicked.connect(self._choose_directory)
        self.cancel_button.clicked.connect(self._cancel_index)
        self.results.cellDoubleClicked.connect(lambda _row, _column: self._open_selected())
        self.results.itemSelectionChanged.connect(self._show_preview)
        self.results.customContextMenuRequested.connect(self._show_context_menu)

        open_action = QAction("打开", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open_selected)
        self.addAction(open_action)

        reveal_action = QAction("打开所在位置", self)
        reveal_action.setShortcut("Ctrl+Shift+O")
        reveal_action.triggered.connect(self._reveal_selected)
        self.addAction(reveal_action)

        self._refresh_scope()
        self._refresh_status()
        self.search_input.setFocus()

    def _choose_directory(self) -> None:
        initial = self.database.get_index_root() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择需要索引的目录", initial)
        if not selected:
            return

        self.choose_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.current_worker = IndexWorker(Path(selected), DB_PATH)
        self.current_worker.signals.progress.connect(self._index_progress)
        self.current_worker.signals.finished.connect(self._index_finished)
        self.current_worker.signals.cancelled.connect(self._index_cancelled)
        self.current_worker.signals.failed.connect(self._index_failed)
        self.thread_pool.start(self.current_worker)

    def _cancel_index(self) -> None:
        if self.current_worker:
            self.current_worker.cancel()
            self.statusBar().showMessage("正在停止索引…")

    def _index_progress(self, path: str, scanned: int, indexed: int) -> None:
        filename = Path(path).name
        self.statusBar().showMessage(
            f"正在索引：{filename}  ·  已扫描 {scanned}  ·  更新 {indexed}"
        )

    def _index_finished(self, stats: IndexStats) -> None:
        self._finish_index_ui()
        self._refresh_scope()
        self.statusBar().showMessage(
            f"索引完成：更新 {stats.indexed}，未变化 {stats.unchanged}，"
            f"删除 {stats.removed}，跳过 {stats.skipped}",
            12000,
        )
        self._perform_search()

    def _index_cancelled(self) -> None:
        self._finish_index_ui()
        self.statusBar().showMessage("索引已停止", 6000)

    def _index_failed(self, message: str) -> None:
        self._finish_index_ui()
        self.statusBar().showMessage(f"索引失败：{message}", 10000)

    def _finish_index_ui(self) -> None:
        self.choose_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.current_worker = None
        self._refresh_status()

    def _perform_search(self) -> None:
        query = self.search_input.text().strip()
        self.results.setRowCount(0)
        self.preview.clear()
        self.current_results = []
        if not query:
            self._refresh_status()
            return

        try:
            rows = self.database.search(query, limit=150)
        except Exception as exc:
            self.statusBar().showMessage(f"搜索失败：{exc}", 8000)
            return

        self.current_results = rows
        self.results.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            name_item = QTableWidgetItem(row.filename)
            name_item.setData(Qt.UserRole, row.path)
            ext_item = QTableWidgetItem(row.extension.lstrip(".").upper())
            time_item = QTableWidgetItem(
                datetime.fromtimestamp(row.modified_time).strftime("%Y-%m-%d %H:%M")
            )
            path_item = QTableWidgetItem(row.path)

            self.results.setItem(row_index, 0, name_item)
            self.results.setItem(row_index, 1, ext_item)
            self.results.setItem(row_index, 2, time_item)
            self.results.setItem(row_index, 3, path_item)

        self.statusBar().showMessage(f"找到 {len(rows)} 条结果（最多显示 150 条）")
        if rows:
            self.results.selectRow(0)

    def _show_preview(self) -> None:
        row_index = self.results.currentRow()
        if row_index < 0 or row_index >= len(self.current_results):
            self.preview.clear()
            return

        row = self.current_results[row_index]
        snippet = row.snippet.replace("<b>", "<mark>").replace("</b>", "</mark>")
        size = self._human_size(row.size)
        safe_filename = html.escape(row.filename)
        safe_path = html.escape(row.path)
        self.preview.setHtml(
            f"<h3>{safe_filename}</h3>"
            f"<p><b>{row.extension.lstrip('.').upper()}</b> · {size}</p>"
            f"<p style='color:#666'>{safe_path}</p><hr>"
            f"<p style='line-height:1.7'>{snippet}</p>"
        )

    def _selected_path(self) -> str | None:
        row = self.results.currentRow()
        if row < 0:
            return None
        item = self.results.item(row, 0)
        return str(item.data(Qt.UserRole)) if item else None

    def _open_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            self.statusBar().showMessage(f"无法打开文件：{exc}", 8000)

    def _reveal_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as exc:
            self.statusBar().showMessage(f"无法打开所在位置：{exc}", 8000)

    def _copy_selected_path(self) -> None:
        path = self._selected_path()
        if path:
            QGuiApplication.clipboard().setText(path)
            self.statusBar().showMessage("已复制文件路径", 3000)

    def _show_context_menu(self, point) -> None:
        if self.results.currentRow() < 0:
            return
        menu = QMenu(self)
        menu.addAction("打开", self._open_selected)
        menu.addAction("打开所在位置", self._reveal_selected)
        menu.addSeparator()
        menu.addAction("复制完整路径", self._copy_selected_path)
        menu.exec(self.results.viewport().mapToGlobal(point))

    def _refresh_scope(self) -> None:
        root = self.database.get_index_root()
        self.scope_label.setText(
            f"搜索范围：{root}" if root else "尚未建立索引，请先选择一个工作目录"
        )

    def _refresh_status(self) -> None:
        count = self.database.count_files()
        self.statusBar().showMessage(f"已索引 {count:,} 个文件")

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("DocSeek")
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
