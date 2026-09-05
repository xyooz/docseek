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
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .chunk_store import ChunkSearchResult, ChunkStore
from .indexer import DirectoryIndexer, IndexCancelled, IndexStats
from .query_parser import parse_query
from .search_db import SearchDatabase
from .search_worker import SearchRequest, SearchResponse, SearchWorker
from .settings_dialog import IndexSettingsDialog
from .watcher import WatchBatch, WatchManager


APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"
PAGE_SIZE = 100

FILE_FILTERS = [
    ("全部类型", None),
    ("PDF", ".pdf"),
    ("Word", ".docx"),
    ("Excel", ".xlsx"),
    ("PowerPoint", ".pptx"),
    ("文本", ".txt"),
]


def split_index_progress_display(path: str) -> tuple[str, str]:
    """Split a normal path or XLSX row-progress display into two UI fields."""
    marker = " · 工作表 "
    if marker in path and " · 已读取 " in path:
        filename, detail = path.split(" · ", 1)
        return filename, detail
    return Path(path).name, ""


class IndexSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)


class WatchSignals(QObject):
    changed = Signal(object)


class IndexWorker(QRunnable):
    def __init__(
        self,
        *,
        db_path: Path,
        roots: list[Path] | None = None,
        paths: list[Path] | None = None,
    ) -> None:
        super().__init__()
        self.roots = roots
        self.paths = paths
        self.db_path = db_path
        self.signals = IndexSignals()
        self.indexer: DirectoryIndexer | None = None

    @property
    def is_full_scan(self) -> bool:
        return self.paths is None

    def cancel(self) -> None:
        if self.indexer:
            self.indexer.cancel()

    def run(self) -> None:
        try:
            database = SearchDatabase(self.db_path)
            self.indexer = DirectoryIndexer(database)

            if self.paths is not None:
                stats = self.indexer.update_paths(
                    self.paths,
                    on_progress=lambda path, current: self.signals.progress.emit(
                        str(path), current.scanned, current.indexed
                    ),
                )
                self.signals.finished.emit(stats)
                return

            total = IndexStats()
            for root in self.roots or []:
                if not root.exists() or not root.is_dir():
                    continue

                def report(path: Path, stats: IndexStats) -> None:
                    self.signals.progress.emit(
                        str(path), total.scanned + stats.scanned, total.indexed + stats.indexed
                    )

                stats = self.indexer.scan(root, on_progress=report)
                total.merge(stats)

            self.signals.finished.emit(total)
        except IndexCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DocSeek — 本地文档全文检索")
        self.resize(1300, 800)

        self.database = SearchDatabase(DB_PATH)
        self.chunk_store = ChunkStore(DB_PATH)
        self.thread_pool = QThreadPool.globalInstance()
        self.search_thread_pool = QThreadPool(self)
        self.search_thread_pool.setMaxThreadCount(2)
        self.active_search_workers: set[SearchWorker] = set()
        self.search_generation = 0
        self.loading_generation: int | None = None

        self.current_worker: IndexWorker | None = None
        self.current_results: list[ChunkSearchResult] = []
        self.search_offset = 0
        self.has_more_results = False
        self.loading_more = False
        self.seen_result_paths: set[str] = set()
        self.pending_watch_paths: set[str] = set()
        self.watch_full_rescan_pending = False

        self.watch_signals = WatchSignals()
        self.watch_signals.changed.connect(self._on_watch_batch)
        self.watch_manager = WatchManager(self.watch_signals.changed.emit)

        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText(
            '搜索正文或直接筛选，例如：信贷 ext:pdf，或 ext:pdf after:2026-01-01'
        )
        self.search_input.setToolTip(
            "支持：ext:pdf 类型 · path:制度 路径 · after:2026-01-01 / before:2026-09-01 日期 · "
            "size:>10MB 大小 · 引号用于短语；筛选条件可单独使用"
        )
        self.search_input.setMinimumHeight(38)

        self.type_filter = QComboBox()
        for label, extension in FILE_FILTERS:
            self.type_filter.addItem(label, extension)
        self.type_filter.setMinimumHeight(38)
        self.type_filter.setMinimumWidth(105)

        self.choose_button = QPushButton("添加目录")
        self.refresh_button = QPushButton("刷新索引")
        self.settings_button = QPushButton("索引设置")
        self.cancel_button = QPushButton("停止")
        for button in (
            self.choose_button,
            self.refresh_button,
            self.settings_button,
            self.cancel_button,
        ):
            button.setMinimumHeight(38)
        self.cancel_button.setVisible(False)

        self.scope_label = QLabel()
        self.scope_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.index_progress_panel = QWidget()
        self.index_progress_panel.setVisible(False)
        self.index_progress_panel.setObjectName("indexProgressPanel")
        self.index_progress_panel.setStyleSheet(
            "QWidget#indexProgressPanel { border: 1px solid palette(mid); border-radius: 6px; }"
        )
        progress_layout = QHBoxLayout(self.index_progress_panel)
        progress_layout.setContentsMargins(10, 7, 10, 7)
        progress_layout.setSpacing(10)

        self.index_progress_bar = QProgressBar()
        self.index_progress_bar.setRange(0, 0)
        self.index_progress_bar.setTextVisible(False)
        self.index_progress_bar.setFixedWidth(110)
        self.index_progress_bar.setFixedHeight(10)

        progress_text = QVBoxLayout()
        progress_text.setContentsMargins(0, 0, 0, 0)
        progress_text.setSpacing(2)
        self.index_file_label = QLabel("准备建立索引…")
        self.index_detail_label = QLabel("")
        self.index_detail_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        progress_text.addWidget(self.index_file_label)
        progress_text.addWidget(self.index_detail_label)

        self.index_counts_label = QLabel("已处理 0 · 更新 0")
        self.index_counts_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.index_counts_label.setMinimumWidth(150)

        progress_layout.addWidget(self.index_progress_bar)
        progress_layout.addLayout(progress_text, 1)
        progress_layout.addWidget(self.index_counts_label)

        self.results = QTableWidget(0, 6)
        self.results.setHorizontalHeaderLabels(
            ["文件名", "命中位置", "类型", "大小", "修改时间", "路径"]
        )
        self.results.setSelectionBehavior(QTableWidget.SelectRows)
        self.results.setSelectionMode(QTableWidget.SingleSelection)
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results.setSortingEnabled(False)
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.setAlternatingRowColors(True)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("选择一条结果，这里会显示命中的正文上下文和具体位置。")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.results)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 480])

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_input, 1)
        top_bar.addWidget(self.type_filter)
        top_bar.addWidget(self.choose_button)
        top_bar.addWidget(self.refresh_button)
        top_bar.addWidget(self.settings_button)
        top_bar.addWidget(self.cancel_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(top_bar)
        layout.addWidget(self.scope_label)
        layout.addWidget(self.index_progress_panel)
        layout.addWidget(splitter, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._perform_search)

        self.search_input.textChanged.connect(self.search_timer.start)
        self.search_input.returnPressed.connect(self._perform_search)
        self.type_filter.currentIndexChanged.connect(self._perform_search)
        self.choose_button.clicked.connect(self._choose_directory)
        self.refresh_button.clicked.connect(self._refresh_all_roots)
        self.settings_button.clicked.connect(self._open_index_settings)
        self.cancel_button.clicked.connect(self._cancel_index)
        self.results.cellDoubleClicked.connect(lambda _row, _column: self._open_selected())
        self.results.itemSelectionChanged.connect(self._show_preview)
        self.results.customContextMenuRequested.connect(self._show_context_menu)
        self.results.verticalScrollBar().valueChanged.connect(self._on_results_scroll)

        open_action = QAction("打开", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open_selected)
        self.addAction(open_action)

        reveal_action = QAction("打开所在位置", self)
        reveal_action.setShortcut("Ctrl+Shift+O")
        reveal_action.triggered.connect(self._reveal_selected)
        self.addAction(reveal_action)

        focus_action = QAction("聚焦搜索框", self)
        focus_action.setShortcut("Ctrl+L")
        focus_action.triggered.connect(self.search_input.setFocus)
        self.addAction(focus_action)

        self._refresh_scope()
        self._refresh_status()
        self._restart_watcher()
        self.search_input.setFocus()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.search_generation += 1
        self.search_thread_pool.clear()
        self.watch_manager.stop()
        super().closeEvent(event)

    def _choose_directory(self) -> None:
        initial = self.database.get_index_root() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择需要索引的目录", initial)
        if selected:
            self._start_index([Path(selected)])

    def _open_index_settings(self) -> None:
        if self.current_worker is not None:
            self.statusBar().showMessage("请等待当前索引任务完成后再修改设置", 5000)
            return
        dialog = IndexSettingsDialog(self.database, self)
        if dialog.exec():
            self._refresh_scope()
            self._restart_watcher()
            self._refresh_status()
            roots = [Path(root) for root in self.database.get_index_roots()]
            if roots:
                self._start_index(roots, automatic=True)
            else:
                self.results.setRowCount(0)
                self.preview.clear()

    def _refresh_all_roots(self) -> None:
        roots = [Path(root) for root in self.database.get_index_roots()]
        if not roots:
            self.statusBar().showMessage("请先添加一个索引目录", 5000)
            return
        self._start_index(roots)

    def _launch_worker(self, worker: IndexWorker, *, automatic: bool) -> None:
        self.choose_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.cancel_button.setVisible(not automatic)
        self.index_progress_panel.setVisible(True)
        self.index_file_label.setText("准备建立索引…")
        self.index_file_label.setToolTip("")
        self.index_detail_label.setText("")
        self.index_counts_label.setText("已处理 0 · 更新 0")
        self.current_worker = worker
        worker.signals.progress.connect(self._index_progress)
        worker.signals.finished.connect(self._index_finished)
        worker.signals.cancelled.connect(self._index_cancelled)
        worker.signals.failed.connect(self._index_failed)
        self.thread_pool.start(worker)

    def _start_index(self, roots: list[Path], *, automatic: bool = False) -> None:
        if self.current_worker is not None:
            if automatic:
                self.watch_full_rescan_pending = True
                self.pending_watch_paths.clear()
            return

        worker = IndexWorker(db_path=DB_PATH, roots=roots)
        self._launch_worker(worker, automatic=automatic)
        if automatic:
            self.statusBar().showMessage("检测到目录结构变化，正在后台校准索引…")

    def _start_path_update(self, paths: list[Path]) -> None:
        if not paths:
            return
        if self.current_worker is not None:
            self.pending_watch_paths.update(str(path) for path in paths)
            return

        worker = IndexWorker(db_path=DB_PATH, paths=paths)
        self._launch_worker(worker, automatic=True)
        self.statusBar().showMessage(f"检测到文件变化，正在增量更新 {len(paths)} 个文件…")

    def _cancel_index(self) -> None:
        if self.current_worker:
            self.current_worker.cancel()
            self.index_detail_label.setText("正在停止…")
            self.statusBar().showMessage("正在停止索引…")

    def _index_progress(self, path: str, scanned: int, indexed: int) -> None:
        filename, detail = split_index_progress_display(path)
        self.index_file_label.setText(filename)
        if not detail:
            self.index_file_label.setToolTip(path)
        self.index_detail_label.setText(detail)
        self.index_counts_label.setText(f"已处理 {scanned:,} · 更新 {indexed:,}")

    def _index_finished(self, stats: IndexStats) -> None:
        worker = self.current_worker
        was_full_scan = worker.is_full_scan if worker is not None else False
        self._finish_index_ui()
        self._refresh_scope()
        if was_full_scan:
            self._restart_watcher()

        extra = f"，排除 {stats.excluded}" if stats.excluded else ""
        chunk_info = f"，生成 {stats.chunks} 个内容块" if stats.chunks else ""
        self.statusBar().showMessage(
            f"索引完成：更新 {stats.indexed}，未变化 {stats.unchanged}，"
            f"删除 {stats.removed}，跳过 {stats.skipped}{chunk_info}{extra}",
            10000,
        )
        self._perform_search()
        self._drain_watch_queue()

    def _index_cancelled(self) -> None:
        self._finish_index_ui()
        self.statusBar().showMessage("索引已停止", 6000)
        self._drain_watch_queue()

    def _index_failed(self, message: str) -> None:
        self._finish_index_ui()
        self.statusBar().showMessage(f"索引失败：{message}", 10000)
        self._drain_watch_queue()

    def _finish_index_ui(self) -> None:
        self.choose_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.settings_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.index_progress_panel.setVisible(False)
        self.current_worker = None
        self._refresh_status()

    def _on_watch_batch(self, batch: WatchBatch) -> None:
        if batch.full_rescan:
            self.watch_full_rescan_pending = True
            self.pending_watch_paths.clear()
        elif not self.watch_full_rescan_pending:
            self.pending_watch_paths.update(batch.paths)
        self._drain_watch_queue()

    def _drain_watch_queue(self) -> None:
        if self.current_worker is not None:
            return

        if self.watch_full_rescan_pending:
            self.watch_full_rescan_pending = False
            self.pending_watch_paths.clear()
            roots = [Path(root) for root in self.database.get_index_roots()]
            if roots:
                self._start_index(roots, automatic=True)
            return

        if self.pending_watch_paths:
            paths = [Path(path) for path in sorted(self.pending_watch_paths)]
            self.pending_watch_paths.clear()
            self._start_path_update(paths)

    def _restart_watcher(self) -> None:
        self.watch_manager.start(
            self.database.get_index_roots(),
            self.database.get_excluded_paths(),
        )

    def _perform_search(self) -> None:
        # Incrementing the generation invalidates every in-flight response from
        # previous text/filter values. Those SQLite statements may finish in
        # their worker thread, but they can no longer mutate the visible UI.
        self.search_generation += 1
        self.search_offset = 0
        self.has_more_results = False
        self.loading_more = False
        self.loading_generation = None
        self.current_results = []
        self.seen_result_paths.clear()
        self.results.setRowCount(0)
        self.preview.clear()
        self._load_next_page(select_first=True)

    def _load_next_page(self, *, select_first: bool = False) -> None:
        if self.loading_more and self.loading_generation == self.search_generation:
            return

        raw_query = self.search_input.text().strip()
        parsed = parse_query(raw_query)
        extension = parsed.extension or self.type_filter.currentData()
        if not parsed.terms and not parsed.has_filters and extension is None:
            self._refresh_status()
            return

        request = SearchRequest(
            generation=self.search_generation,
            query=parsed.text,
            limit=PAGE_SIZE,
            offset=self.search_offset,
            extension=extension,
            path_contains=parsed.path_contains,
            modified_after=parsed.modified_after,
            modified_before=parsed.modified_before,
            min_size=parsed.min_size,
            max_size=parsed.max_size,
            select_first=select_first,
            is_filter_only=not parsed.terms,
        )
        worker = SearchWorker(self.chunk_store, request)
        worker.signals.finished.connect(
            lambda response, current=worker: self._search_finished(current, response)
        )
        worker.signals.failed.connect(
            lambda generation, message, current=worker: self._search_failed(
                current, generation, message
            )
        )
        self.active_search_workers.add(worker)
        self.loading_more = True
        self.loading_generation = request.generation
        if request.offset == 0:
            self.statusBar().showMessage("正在搜索…")
        self.search_thread_pool.start(worker)

    def _search_finished(self, worker: SearchWorker, response: SearchResponse) -> None:
        self.active_search_workers.discard(worker)
        request = response.request
        if request.generation != self.search_generation:
            return

        if self.loading_generation == request.generation:
            self.loading_more = False
            self.loading_generation = None

        # A page from the current generation is only valid at the offset it was
        # requested for. This also protects against future changes that allow
        # more than one pagination request to be queued at once.
        if request.offset != self.search_offset:
            return

        page = response.page
        rows = page.items
        unique_rows = [row for row in rows if row.path not in self.seen_result_paths]
        for row in unique_rows:
            self.seen_result_paths.add(row.path)

        start_row = len(self.current_results)
        self.current_results.extend(unique_rows)
        self.results.setRowCount(len(self.current_results))
        for offset, row in enumerate(unique_rows):
            self._populate_result_row(start_row + offset, row)

        self.search_offset += len(rows)
        self.has_more_results = self.search_offset < page.total_count
        suffix = " · 向下滚动继续加载" if self.has_more_results else ""
        mode = "筛选结果" if request.is_filter_only else "搜索结果"
        latency = (
            f"{response.elapsed_ms:.1f} ms"
            if response.elapsed_ms < 10
            else f"{response.elapsed_ms:.0f} ms"
        )
        self.statusBar().showMessage(
            f"{mode}：已显示 {len(self.current_results):,} / 共 {page.total_count:,} 个文件"
            f" · {latency}{suffix}"
        )
        if request.select_first and self.current_results:
            self.results.selectRow(0)

    def _search_failed(self, worker: SearchWorker, generation: int, message: str) -> None:
        self.active_search_workers.discard(worker)
        if generation != self.search_generation:
            return
        if self.loading_generation == generation:
            self.loading_more = False
            self.loading_generation = None
        self.statusBar().showMessage(f"搜索失败：{message}", 8000)

    def _populate_result_row(self, row_index: int, row: ChunkSearchResult) -> None:
        name_item = QTableWidgetItem(row.filename)
        name_item.setData(Qt.UserRole, row.path)
        location_item = QTableWidgetItem(row.location or "—")
        ext_item = QTableWidgetItem(row.extension.lstrip(".").upper())
        size_item = QTableWidgetItem(self._human_size(row.size))
        time_item = QTableWidgetItem(
            datetime.fromtimestamp(row.modified_time).strftime("%Y-%m-%d %H:%M")
        )
        path_item = QTableWidgetItem(row.path)

        self.results.setItem(row_index, 0, name_item)
        self.results.setItem(row_index, 1, location_item)
        self.results.setItem(row_index, 2, ext_item)
        self.results.setItem(row_index, 3, size_item)
        self.results.setItem(row_index, 4, time_item)
        self.results.setItem(row_index, 5, path_item)

    def _on_results_scroll(self, value: int) -> None:
        scrollbar = self.results.verticalScrollBar()
        if self.has_more_results and value >= scrollbar.maximum() - 2:
            self._load_next_page()

    def _show_preview(self) -> None:
        row_index = self.results.currentRow()
        if row_index < 0 or row_index >= len(self.current_results):
            self.preview.clear()
            return

        row = self.current_results[row_index]
        safe_filename = html.escape(row.filename)
        safe_path = html.escape(row.path)
        metadata = (
            f"<h3>{safe_filename}</h3>"
            f"<p><b>{html.escape(row.extension.lstrip('.').upper())}</b> · {self._human_size(row.size)}</p>"
            f"<p style='color:#666'>{safe_path}</p>"
        )

        if not row.snippet:
            self.preview.setHtml(
                metadata
                + "<hr><p style='color:#666'>当前为筛选浏览结果。输入正文关键词后，可显示具体命中位置和上下文。</p>"
            )
            return

        safe_snippet = html.escape(row.snippet)
        safe_snippet = safe_snippet.replace("[[HIT]]", "<mark>").replace("[[/HIT]]", "</mark>")
        safe_location = html.escape(row.location)
        self.preview.setHtml(
            metadata
            + f"<p><b>命中位置：</b>{safe_location}</p><hr>"
            + f"<p style='line-height:1.7'>{safe_snippet}</p>"
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
        roots = self.database.get_index_roots()
        excluded = self.database.get_excluded_paths()
        if not roots:
            self.scope_label.setText("尚未建立索引，请先添加一个工作目录")
            self.scope_label.setToolTip("")
            return

        suffix = f" · 排除 {len(excluded)} 个目录" if excluded else ""
        if len(roots) == 1:
            self.scope_label.setText(f"搜索范围：{roots[0]} · 自动监测变化{suffix}")
        else:
            self.scope_label.setText(
                f"搜索范围：{len(roots)} 个目录 · 自动监测变化{suffix} · 最近添加：{roots[-1]}"
            )
        tooltip = ["索引目录：", *roots]
        if excluded:
            tooltip.extend(["", "排除目录：", *excluded])
        self.scope_label.setToolTip("\n".join(tooltip))

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