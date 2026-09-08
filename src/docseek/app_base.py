from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableView,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .chunk_store import ChunkStore
from .app_icon import load_app_icon
from .location_preview import preview_kind
from .structure_store import preview_location
from .indexer import DirectoryIndexer, IndexCancelled, IndexStats
from .index_formats import IndexFormatStore
from .query_parser import parse_query, query_filter_chips, remove_query_filter
from .results_model import SearchResultsModel
from .search_db import SearchDatabase
from .search_sort import SORT_FILENAME, SORT_MODIFIED, SORT_RELEVANCE
from .search_worker import SearchRequest, SearchResponse, SearchWorker
from .settings_dialog import IndexSettingsDialog
from .ui_theme import APPLICATION_STYLESHEET
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
    ("PowerPoint 97-2003", ".ppt"),
    ("WPS 文字", ".wps"),
    ("WPS 表格", ".et"),
    ("WPS 表格模板", ".ett"),
    ("WPS 表格 2007/2010", ".etx"),
    ("WPS 表格模板 2007/2010", ".ettx"),
    ("WPS 演示", ".dps"),
    ("HTML", ".html"),
    ("XML", ".xml"),
    ("Markdown", ".md"),
    ("文本", ".txt"),
]

SORT_OPTIONS = [
    ("相关性", SORT_RELEVANCE),
    ("最近修改", SORT_MODIFIED),
    ("文件名", SORT_FILENAME),
]


def split_index_progress_display(path: str) -> tuple[str, str]:
    """Split a normal path or XLSX row-progress display into two UI fields."""
    marker = " · 工作表 "
    if marker in path and " · 已读取 " in path:
        filename, detail = path.split(" · ", 1)
        return filename, detail
    return Path(path).name, ""


def result_entry_row(row_count: int, *, move_down: bool) -> int | None:
    """Choose the row entered when moving from the search box into results."""
    if row_count <= 0:
        return None
    return 0 if move_down else row_count - 1


def empty_result_html(*, filter_only: bool, has_filters: bool = False,
                      indexing: bool = False, issue_count: int = 0,
                      paused_roots: int = 0) -> str:
    """Return actionable empty-result guidance for the preview pane."""
    if filter_only:
        message = (
            "<h3>当前筛选没有匹配文件</h3>"
            "<p>可以移除上方筛选条件，或调整文件类型、日期、路径和大小范围后再试。</p>"
        )
    else:
        message = (
            "<h3>没有找到匹配文档</h3>"
            "<p>可以减少关键词、取消过严的引号短语，或移除部分筛选条件后再试。</p>"
        )
    if has_filters:
        message += '<p><a href="docseek:relax">保留关键词，移除元数据筛选</a></p>'
    if indexing:
        message += "<p>索引任务正在运行，部分新内容可能尚不可搜索。</p>"
    if issue_count:
        message += f"<p>索引中有 {issue_count} 个问题文件，可能影响搜索覆盖。</p>"
    if paused_roots:
        message += f"<p>{paused_roots} 个目录已暂停更新，现有结果可能不是最新内容。</p>"
    message += '<p><a href="docseek:settings">检查索引范围、暂停目录和问题文件</a></p>'
    return message


def idle_preview_html() -> str:
    """Return useful guidance before the user has entered a search."""
    return (
        "<div style='margin: 24px;'>"
        "<h3>从记得的一句话开始</h3>"
        "<p>输入文件名或正文中的关键词，DocSeek 会显示命中片段和文档位置。</p>"
        "<p style='color: #667085;'>也可以只选择文件类型，或按 F1 查看日期、路径、大小等筛选方式。</p>"
        "</div>"
    )


class IndexSignals(QObject):
    progress = Signal(str, int, int)
    plan = Signal(str, int, int, int)
    detailed_progress = Signal(str, int, int, int, str)
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
                candidate_total = len(self.paths)
                self.signals.plan.emit("", candidate_total, 1, 1)

                def report_path(path: Path, current: IndexStats) -> None:
                    self.signals.progress.emit(
                        str(path), current.scanned, current.indexed
                    )
                    self.signals.detailed_progress.emit(
                        str(path), current.scanned, current.indexed,
                        candidate_total, "",
                    )

                stats = self.indexer.update_paths(
                    self.paths,
                    on_progress=report_path,
                )
                self.signals.finished.emit(stats)
                return

            total = IndexStats()
            valid_roots = [
                root for root in (self.roots or [])
                if root.exists() and root.is_dir()
            ]
            for root_position, root in enumerate(valid_roots, start=1):
                candidate_total = 0

                def candidates_ready(count: int) -> None:
                    nonlocal candidate_total
                    candidate_total = count
                    self.signals.plan.emit(
                        str(root), count, root_position, len(valid_roots)
                    )

                def report(path: Path, stats: IndexStats) -> None:
                    self.signals.progress.emit(
                        str(path), total.scanned + stats.scanned, total.indexed + stats.indexed
                    )
                    self.signals.detailed_progress.emit(
                        str(path), stats.scanned, stats.indexed,
                        candidate_total, str(root),
                    )

                stats = self.indexer.scan(
                    root,
                    on_progress=report,
                    on_candidates_ready=candidates_ready,
                )
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
        self.setWindowIcon(load_app_icon())
        self.resize(1300, 800)
        self.setMinimumSize(1040, 640)
        self.setStyleSheet(APPLICATION_STYLESHEET)

        self.database = SearchDatabase(DB_PATH)
        self.chunk_store = ChunkStore(DB_PATH)
        # Keep indexing ownership local to this window. This lets shutdown stop
        # exactly DocSeek's index job without waiting on unrelated global Qt
        # tasks, while current_worker still enforces one active job at a time.
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self.search_thread_pool = QThreadPool(self)
        self.search_thread_pool.setMaxThreadCount(2)
        self.active_search_workers: set[SearchWorker] = set()
        self.search_generation = 0
        self.loading_generation: int | None = None

        self.current_worker: IndexWorker | None = None
        self.index_root_progress: dict[str, tuple[int, int]] = {}
        self.index_root_position = 1
        self.index_root_count = 1
        self.index_candidate_total = 0
        self.index_current_root = ""
        self.search_offset = 0
        self.has_more_results = False
        self.loading_more = False
        self.seen_result_paths: set[str] = set()
        self.pending_watch_paths: set[str] = set()
        self.watch_full_rescan_pending = False
        self._close_when_index_stops = False

        self.watch_signals = WatchSignals()
        self.watch_signals.changed.connect(self._on_watch_batch)
        self.watch_manager = WatchManager(self.watch_signals.changed.emit)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchInput")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText(
            "搜索文件名或正文关键词"
        )
        self.search_input.setToolTip(
            "支持：ext:pdf 类型 · path:制度 路径 · after:2026-01-01 / before:2026-09-01 日期 · "
            "size:>10MB 大小 · 引号用于短语；筛选条件可单独使用\n"
            "快捷键：↑/↓ 进入结果 · Enter 搜索/打开 · Esc 清空搜索"
        )
        self.search_input.setMinimumHeight(44)

        self.search_button = QPushButton("搜索")
        self.search_button.setProperty("primary", True)
        self.search_button.setMinimumHeight(44)
        self.search_button.setMinimumWidth(76)
        self.search_button.setToolTip("立即搜索（也可以按 Enter）")

        self.type_filter = QComboBox()
        for label, extension in FILE_FILTERS:
            self.type_filter.addItem(label, extension)
        self.type_filter.setMinimumHeight(44)
        self.type_filter.setMinimumWidth(105)

        self.sort_filter = QComboBox()
        for label, sort_mode in SORT_OPTIONS:
            self.sort_filter.addItem(label, sort_mode)
        self.sort_filter.setMinimumHeight(44)
        self.sort_filter.setMinimumWidth(105)
        self.sort_filter.setToolTip(
            "排序方式：有正文关键词时默认按相关性；无关键词纯筛选时“相关性”按最近修改显示"
        )

        self.filter_chip_panel = QWidget()
        self.filter_chip_panel.setVisible(False)
        self.filter_chip_panel.setObjectName("filterChipPanel")
        self.filter_chip_layout = QHBoxLayout(self.filter_chip_panel)
        self.filter_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_chip_layout.setSpacing(6)
        self.filter_chip_label = QLabel("当前筛选：")
        self.filter_chip_label.setStyleSheet("color: palette(mid);")
        self.filter_chip_layout.addWidget(self.filter_chip_label)
        self.filter_chip_layout.addStretch(1)
        self.filter_chip_buttons: list[QToolButton] = []

        self.choose_button = QPushButton("添加目录")
        self.refresh_button = QPushButton("刷新索引")
        self.settings_button = QPushButton("索引设置")
        self.more_button = QToolButton()
        self.more_button.setText("更多")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        workspace_menu = QMenu(self.more_button)
        refresh_action = workspace_menu.addAction("重新扫描全部目录")
        refresh_action.triggered.connect(self._refresh_all_roots)
        self.more_button.setMenu(workspace_menu)
        self.more_button.setToolTip("不常用的索引操作")
        self.refresh_button.setVisible(False)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setProperty("danger", True)
        for button in (
            self.choose_button,
            self.refresh_button,
            self.settings_button,
            self.cancel_button,
            self.more_button,
        ):
            button.setMinimumHeight(38)
        self.cancel_button.setVisible(False)

        self.scope_label = QLabel()
        self.scope_label.setObjectName("scopeLabel")
        self.scope_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scope_label.setMinimumWidth(0)
        self.scope_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.index_progress_panel = QWidget()
        self.index_progress_panel.setVisible(False)
        self.index_progress_panel.setObjectName("indexProgressPanel")
        progress_layout = QHBoxLayout(self.index_progress_panel)
        progress_layout.setContentsMargins(14, 10, 14, 10)
        progress_layout.setSpacing(12)

        self.index_progress_bar = QProgressBar()
        self.index_progress_bar.setRange(0, 0)
        self.index_progress_bar.setTextVisible(False)
        self.index_progress_bar.setFormat("%p%")
        self.index_progress_bar.setFixedWidth(180)
        self.index_progress_bar.setFixedHeight(16)

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

        self.results = QTableView()
        self.results.setObjectName("resultsTable")
        self.results_model = SearchResultsModel(self.results)
        self.results.setModel(self.results_model)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.setSortingEnabled(False)
        self.results.setWordWrap(False)
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
        self.preview.setObjectName("previewPane")
        self.preview.setOpenExternalLinks(False)
        self.preview.setOpenLinks(False)
        self.preview.anchorClicked.connect(self._preview_action)
        self.preview.setPlaceholderText(
            "输入记得的正文关键词开始搜索；选中结果后，这里会显示命中上下文和具体位置。"
        )
        self.preview.setHtml(idle_preview_html())

        self.results_panel = QFrame()
        self.results_panel.setObjectName("surfacePanel")
        results_layout = QVBoxLayout(self.results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)
        results_heading = QWidget()
        results_heading_layout = QHBoxLayout(results_heading)
        results_heading_layout.setContentsMargins(14, 11, 14, 9)
        self.results_title_label = QLabel("搜索结果")
        self.results_title_label.setObjectName("sectionTitle")
        self.results_meta_label = QLabel("输入关键词开始搜索")
        self.results_meta_label.setObjectName("sectionMeta")
        self.results_meta_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        results_heading_layout.addWidget(self.results_title_label)
        results_heading_layout.addStretch(1)
        results_heading_layout.addWidget(self.results_meta_label)
        results_layout.addWidget(results_heading)
        results_layout.addWidget(self.results, 1)

        self.preview_panel = QFrame()
        self.preview_panel.setObjectName("surfacePanel")
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        preview_heading = QWidget()
        preview_heading_layout = QHBoxLayout(preview_heading)
        preview_heading_layout.setContentsMargins(14, 11, 14, 9)
        preview_title = QLabel("内容预览")
        preview_title.setObjectName("sectionTitle")
        preview_hint = QLabel("双击结果打开文件")
        preview_hint.setObjectName("sectionMeta")
        preview_heading_layout.addWidget(preview_title)
        preview_heading_layout.addStretch(1)
        preview_heading_layout.addWidget(preview_hint)
        preview_layout.addWidget(preview_heading)
        preview_layout.addWidget(self.preview, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(8)
        splitter.addWidget(self.results_panel)
        splitter.addWidget(self.preview_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 480])

        self.search_bar_panel = QWidget()
        self.search_bar_layout = QHBoxLayout(self.search_bar_panel)
        self.search_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.search_bar_layout.setSpacing(8)
        self.search_bar_layout.addWidget(self.search_input, 1)
        self.search_bar_layout.addWidget(self.search_button)
        self.search_bar_layout.addWidget(self.type_filter)
        self.search_bar_layout.addWidget(self.sort_filter)

        self.workspace_bar_panel = QFrame()
        self.workspace_bar_panel.setObjectName("workspaceBar")
        self.workspace_bar_layout = QHBoxLayout(self.workspace_bar_panel)
        self.workspace_bar_layout.setContentsMargins(12, 7, 8, 7)
        self.workspace_bar_layout.setSpacing(6)
        self.workspace_bar_layout.addWidget(self.scope_label, 1)
        self.workspace_bar_layout.addWidget(self.choose_button)
        self.workspace_bar_layout.addWidget(self.settings_button)
        self.workspace_bar_layout.addWidget(self.more_button)
        self.workspace_bar_layout.addWidget(self.cancel_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 10)
        layout.setSpacing(12)
        layout.addWidget(self.search_bar_panel)
        layout.addWidget(self.filter_chip_panel)
        layout.addWidget(self.workspace_bar_panel)
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
        self.search_input.textChanged.connect(self._refresh_filter_chips)
        self.search_input.returnPressed.connect(self._perform_search)
        self.search_button.clicked.connect(self._perform_search)
        self.type_filter.currentIndexChanged.connect(self._perform_search)
        self.sort_filter.currentIndexChanged.connect(self._perform_search)
        self.choose_button.clicked.connect(self._choose_directory)
        self.refresh_button.clicked.connect(self._refresh_all_roots)
        self.settings_button.clicked.connect(self._open_index_settings)
        self.cancel_button.clicked.connect(self._cancel_index)
        self.results.doubleClicked.connect(lambda _index: self._open_selected())
        self.results.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._show_preview()
        )
        self.results.customContextMenuRequested.connect(self._show_context_menu)
        self.results.verticalScrollBar().valueChanged.connect(self._on_results_scroll)
        self.search_input.installEventFilter(self)
        self.results.installEventFilter(self)

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

        self._refresh_filter_chips()
        self._refresh_scope()
        self._refresh_status()
        self._restart_watcher()
        self.search_input.setFocus()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.KeyPress:
            key = event.key()

            if watched is self.search_input and key in (Qt.Key_Down, Qt.Key_Up):
                row = result_entry_row(
                    self.results_model.rowCount(), move_down=key == Qt.Key_Down
                )
                if row is not None:
                    index = self.results_model.index(row, 0)
                    self.results.setCurrentIndex(index)
                    self.results.selectRow(row)
                    self.results.scrollTo(index)
                    self.results.setFocus()
                    return True

            if watched is self.results and key in (Qt.Key_Return, Qt.Key_Enter):
                self._open_selected()
                return True

            if (
                watched is self.results
                and key == Qt.Key_Up
                and self.results.currentIndex().row() <= 0
            ):
                self.search_input.setFocus()
                self.search_input.setCursorPosition(len(self.search_input.text()))
                return True

            if watched in (self.search_input, self.results) and key == Qt.Key_Escape:
                self._clear_search()
                return True

        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._defer_close_until_index_stops(event):
            return
        self.search_generation += 1
        self.search_thread_pool.clear()
        self.thread_pool.clear()
        self.watch_manager.stop()
        super().closeEvent(event)

    def _defer_close_until_index_stops(self, event) -> bool:
        """Cancel an active index job and close after its worker has unwound."""
        if self.current_worker is None:
            return False
        self._close_when_index_stops = True
        self.current_worker.cancel()
        self.watch_manager.stop()
        self.index_detail_label.setText("正在安全停止索引…")
        self.statusBar().showMessage("正在安全停止索引，完成后将关闭 DocSeek…")
        event.ignore()
        return True

    def _choose_directory(self) -> None:
        initial = self.database.get_index_root() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择需要索引的目录", initial)
        if selected:
            if not self.database.get_index_roots():
                IndexFormatStore(self.database).ensure_new_index_default()
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
                self.results_model.clear()

    def _refresh_all_roots(self) -> None:
        roots = [Path(root) for root in self.database.get_index_roots()]
        if not roots:
            self.statusBar().showMessage("请先添加一个索引目录", 5000)
            return
        self._start_index(roots)

    def _launch_worker(self, worker: IndexWorker, *, automatic: bool) -> None:
        self.choose_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        # Settings remains available as a live, read-only progress view while
        # an index job owns the database writer.
        self.settings_button.setEnabled(True)
        # Full scans can take long enough that users must retain control even
        # when the scan was started automatically during startup recovery.
        self.cancel_button.setVisible(worker.is_full_scan)
        self.index_progress_panel.setVisible(True)
        self.index_file_label.setText("准备建立索引…")
        self.index_file_label.setToolTip("")
        self.index_detail_label.setText("已写入的内容可以继续搜索")
        self.index_counts_label.setText("已处理 0 · 更新 0")
        self.index_candidate_total = 0
        self.index_current_root = ""
        self.index_progress_bar.setRange(0, 0)
        self.index_progress_bar.setTextVisible(False)
        if worker.is_full_scan:
            self.index_root_progress = {}
        self.current_worker = worker
        worker.signals.plan.connect(self._index_plan)
        worker.signals.detailed_progress.connect(self._index_progress_detailed)
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
        self.index_detail_label.setText(detail or "已写入的内容可以继续搜索")
        self.index_counts_label.setText(f"已处理 {scanned:,} · 更新 {indexed:,}")

    def _index_plan(
        self,
        root: str,
        candidate_total: int,
        root_position: int,
        root_count: int,
    ) -> None:
        self.index_current_root = root
        self.index_candidate_total = candidate_total
        self.index_root_position = root_position
        self.index_root_count = root_count
        if root:
            self.index_root_progress[root] = (0, candidate_total)
        if candidate_total > 0:
            self.index_progress_bar.setRange(0, candidate_total)
            self.index_progress_bar.setValue(0)
            self.index_progress_bar.setTextVisible(True)
        else:
            self.index_progress_bar.setRange(0, 0)
            self.index_progress_bar.setTextVisible(False)

    def _index_progress_detailed(
        self,
        path: str,
        scanned: int,
        indexed: int,
        candidate_total: int,
        root: str,
    ) -> None:
        filename, detail = split_index_progress_display(path)
        self.index_file_label.setText(filename)
        if not detail:
            self.index_file_label.setToolTip(path)
        self.index_detail_label.setText(detail or "已写入的内容可以继续搜索")

        if candidate_total > 0:
            completed = min(scanned, candidate_total)
            self.index_progress_bar.setRange(0, candidate_total)
            self.index_progress_bar.setValue(completed)
            self.index_progress_bar.setTextVisible(True)
            if root:
                self.index_root_progress[root] = (completed, candidate_total)
            root_prefix = (
                f"目录 {self.index_root_position}/{self.index_root_count} · "
                if self.index_root_count > 1
                else ""
            )
            self.index_counts_label.setText(
                f"{root_prefix}完成 {completed:,}/{candidate_total:,} · 更新 {indexed:,}"
            )
        else:
            self.index_progress_bar.setRange(0, 0)
            self.index_progress_bar.setTextVisible(False)
            self.index_counts_label.setText(f"已发现并处理 {scanned:,} · 更新 {indexed:,}")

    def _index_finished(self, stats: IndexStats) -> None:
        worker = self.current_worker
        was_full_scan = worker.is_full_scan if worker is not None else False
        closing = self._close_when_index_stops
        self._finish_index_ui()
        if closing:
            return
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
        closing = self._close_when_index_stops
        self._finish_index_ui()
        if closing:
            return
        self.statusBar().showMessage("索引已停止", 6000)
        self._drain_watch_queue()

    def _index_failed(self, message: str) -> None:
        closing = self._close_when_index_stops
        self._finish_index_ui()
        if closing:
            return
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
        if self._close_when_index_stops:
            QTimer.singleShot(0, self.close)

    def _on_watch_batch(self, batch: WatchBatch) -> None:
        if batch.full_rescan:
            self.watch_full_rescan_pending = True
            self.pending_watch_paths.clear()
        elif not self.watch_full_rescan_pending:
            self.pending_watch_paths.update(batch.paths)
        self._drain_watch_queue()

    def _drain_watch_queue(self) -> None:
        if self.current_worker is not None or self._close_when_index_stops:
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
            IndexFormatStore(self.database).enabled_extensions(),
        )

    def _refresh_filter_chips(self, *_args) -> None:
        for button in self.filter_chip_buttons:
            self.filter_chip_layout.removeWidget(button)
            button.deleteLater()
        self.filter_chip_buttons.clear()

        chips = query_filter_chips(self.search_input.text())
        for chip in chips:
            button = QToolButton(self.filter_chip_panel)
            label = chip.label if len(chip.label) <= 42 else chip.label[:39] + "…"
            button.setText(f"{label}  ×")
            button.setToolTip(f"{chip.label}\n点击移除此筛选")
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QToolButton { border: 1px solid palette(mid); border-radius: 10px; "
                "padding: 3px 8px; background: palette(base); } "
                "QToolButton:hover { background: palette(alternate-base); }"
            )
            button.clicked.connect(
                lambda _checked=False, key=chip.key: self._remove_filter_chip(key)
            )
            self.filter_chip_layout.insertWidget(
                self.filter_chip_layout.count() - 1, button
            )
            self.filter_chip_buttons.append(button)

        self.filter_chip_panel.setVisible(bool(chips))

    def _remove_filter_chip(self, key: str) -> None:
        updated = remove_query_filter(self.search_input.text(), key)
        self.search_input.setText(updated)
        self.search_input.setCursorPosition(len(updated))
        self.search_input.setFocus()

    def _clear_search(self) -> None:
        """Clear text and the explicit type selector without firing duplicate searches."""
        self.search_timer.stop()
        search_signals_were_blocked = self.search_input.blockSignals(True)
        type_signals_were_blocked = self.type_filter.blockSignals(True)
        try:
            self.search_input.clear()
            self.type_filter.setCurrentIndex(0)
        finally:
            self.search_input.blockSignals(search_signals_were_blocked)
            self.type_filter.blockSignals(type_signals_were_blocked)

        self._refresh_filter_chips()
        self._perform_search()
        self.search_input.setFocus()

    def _perform_search(self) -> None:
        # Incrementing the generation invalidates every in-flight response from
        # previous text/filter values. Those SQLite statements may finish in
        # their worker thread, but they can no longer mutate the visible UI.
        self.search_generation += 1
        self.search_offset = 0
        self.has_more_results = False
        self.loading_more = False
        self.loading_generation = None
        self.seen_result_paths.clear()
        self.results_model.clear()
        self.results_meta_label.setText("正在搜索…")
        self.preview.clear()
        self._load_next_page(select_first=True)

    def _load_next_page(self, *, select_first: bool = False) -> None:
        if self.loading_more and self.loading_generation == self.search_generation:
            return

        raw_query = self.search_input.text().strip()
        parsed = parse_query(raw_query)
        extension = parsed.extension or self.type_filter.currentData()
        if not parsed.terms and not parsed.has_filters and extension is None:
            self.results_meta_label.setText("输入关键词开始搜索")
            self.preview.setHtml(idle_preview_html())
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
            sort_mode=self.sort_filter.currentData() or SORT_RELEVANCE,
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
        self._displayed_query = request.query
        rows = page.items
        unique_rows = [row for row in rows if row.path not in self.seen_result_paths]
        for row in unique_rows:
            self.seen_result_paths.add(row.path)
        self.results_model.append_items(unique_rows)

        self.search_offset += len(rows)
        self.has_more_results = self.search_offset < page.total_count
        suffix = " · 向下滚动继续加载" if self.has_more_results else ""
        mode = "筛选结果" if request.is_filter_only else "搜索结果"
        latency = (
            f"{response.elapsed_ms:.1f} ms"
            if response.elapsed_ms < 10
            else f"{response.elapsed_ms:.0f} ms"
        )
        displayed = self.results_model.rowCount()
        self.results_meta_label.setText(f"{displayed:,} / {page.total_count:,} 个文件")
        self.statusBar().showMessage(
            f"{mode}：已显示 {displayed:,} / 共 {page.total_count:,} 个文件"
            f" · {latency}{suffix}"
        )
        if page.total_count == 0:
            from .index_issues import IndexIssueStore
            from .index_root_state import IndexRootStateStore
            try:
                issues = IndexIssueStore(self.database.db_path).count()
                paused = len(IndexRootStateStore(self.database).paused_roots())
            except Exception:
                issues = paused = 0
            self.preview.setHtml(empty_result_html(
                filter_only=request.is_filter_only,
                has_filters=parse_query(self.search_input.text()).has_filters or self.type_filter.currentData() is not None,
                indexing=self.current_worker is not None,
                issue_count=issues, paused_roots=paused,
            ))
        elif request.select_first and displayed:
            index = self.results_model.index(0, 0)
            self.results.setCurrentIndex(index)
            self.results.selectRow(0)

    def _search_failed(self, worker: SearchWorker, generation: int, message: str) -> None:
        self.active_search_workers.discard(worker)
        if generation != self.search_generation:
            return
        if self.loading_generation == generation:
            self.loading_more = False
            self.loading_generation = None
        self.results_meta_label.setText("搜索失败")
        self.statusBar().showMessage(f"搜索失败：{message}", 8000)

    def _on_results_scroll(self, value: int) -> None:
        scrollbar = self.results.verticalScrollBar()
        if self.has_more_results and value >= scrollbar.maximum() - 2:
            self._load_next_page()

    def _show_preview(self) -> None:
        row_index = self.results.currentIndex().row()
        row = self.results_model.result_at(row_index)
        if row is None:
            if self.database.get_index_roots():
                self.preview.clear()
            return

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
            + '<p><a href="docseek:hits">查看此文件的全部命中位置</a></p>'
            + ('<p><a href="docseek:location">查看原文件命中页 / 行</a></p>'
               if preview_kind(row.extension, preview_location(row)) else "")
        )

    def _preview_action(self, url) -> None:
        action = url.toString()
        if action == "docseek:settings":
            self._open_index_settings()
        elif action == "docseek:relax":
            text = parse_query(self.search_input.text()).text
            self.search_timer.stop()
            old_text = self.search_input.blockSignals(True)
            old_type = self.type_filter.blockSignals(True)
            try:
                self.search_input.setText(text)
                self.type_filter.setCurrentIndex(0)
            finally:
                self.search_input.blockSignals(old_text)
                self.type_filter.blockSignals(old_type)
            self._refresh_filter_chips()
            self._perform_search()
            self.search_input.setFocus()
        elif action == "docseek:hits":
            row = self.results_model.result_at(self.results.currentIndex().row())
            if row and row.snippet:
                from .document_hits_dialog import DocumentHitsDialog
                dialog = DocumentHitsDialog(self.chunk_store, row, getattr(self, "_displayed_query", ""), self)
                try:
                    dialog.exec()
                finally:
                    dialog.deleteLater()
        elif action == "docseek:location":
            row = self.results_model.result_at(self.results.currentIndex().row())
            if row and preview_kind(row.extension, preview_location(row)):
                from .preview_dialog import LocationPreviewDialog
                dialog = LocationPreviewDialog(row, self)
                try:
                    dialog.exec()
                finally:
                    dialog.deleteLater()

    def _selected_path(self) -> str | None:
        row = self.results_model.result_at(self.results.currentIndex().row())
        return row.path if row is not None else None

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
        if not self.results.currentIndex().isValid():
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
            self.preview.setHtml(
                "<h3>开始使用 DocSeek</h3>"
                "<p>点击上方“添加目录”，选择需要检索的工作资料目录。建立索引后，"
                "直接输入记得的正文内容即可查找文档，不需要记住文件名。</p>"
            )
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
