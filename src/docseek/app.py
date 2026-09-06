from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import app_base
from .index_root_state import IndexRootStateStore
from .pausable_settings_dialog import PausableIndexSettingsDialog
from .results_layout import (
    DEFAULT_COLUMN_WIDTHS,
    RESULTS_HEADER_STATE_KEY,
    decode_header_state,
    encode_header_state,
)
from .search_help import SearchHelpDialog
from .search_presets import SearchState, SearchStateStore, search_state_label
from .search_session import close_persistent_search_stores
from .search_sort import SORT_RELEVANCE
from .storage_location import (
    StorageLocationError,
    pending_index_storage_move,
    stage_index_storage_move,
)

# Keep the established public helpers available from docseek.app. Existing
# tests, scripts and users should not need to know that the stable core window
# now lives in app_base.py.
APP_DIR = app_base.APP_DIR
DB_PATH = app_base.DB_PATH
PAGE_SIZE = app_base.PAGE_SIZE
FILE_FILTERS = app_base.FILE_FILTERS
SORT_OPTIONS = app_base.SORT_OPTIONS
IndexSignals = app_base.IndexSignals
WatchSignals = app_base.WatchSignals
IndexWorker = app_base.IndexWorker
split_index_progress_display = app_base.split_index_progress_display
result_entry_row = app_base.result_entry_row
empty_result_html = app_base.empty_result_html


class MainWindow(app_base.MainWindow):
    """Desktop window with persisted product-level search preferences."""

    HISTORY_DELAY_MS = 1200
    SEARCH_SHUTDOWN_TIMEOUT_MS = 10_000
    RESULTS_LAYOUT_SAVE_DELAY_MS = 350

    def __init__(self) -> None:
        self._history_ui_ready = False
        self._history_candidate_generation: int | None = None
        self._explicit_history_generation: int | None = None

        # Preserve the long-standing test/customization contract where callers
        # patch docseek.app.DB_PATH before constructing MainWindow.
        app_base.DB_PATH = DB_PATH
        super().__init__()

        self.search_state_store = SearchStateStore(self.database)
        self.history_record_timer = QTimer(self)
        self.history_record_timer.setSingleShot(True)
        self.history_record_timer.setInterval(self.HISTORY_DELAY_MS)
        self.history_record_timer.timeout.connect(self._record_history_candidate)

        self._install_search_state_controls()
        self._install_results_layout()
        self._install_first_run_panel()
        self.search_input.setPlaceholderText(
            "输入文件名或正文关键词，例如：客户经理管理办法"
        )
        self.search_input.textChanged.connect(self._refresh_saved_button)
        self.type_filter.currentIndexChanged.connect(self._refresh_saved_button)
        self.sort_filter.currentIndexChanged.connect(self._refresh_saved_button)

        self._history_ui_ready = True
        self._refresh_saved_button()
        self._refresh_first_run_state()

        # A watcher only sees changes that happen while DocSeek is running.
        # Snapshot the already-configured active roots at startup and reconcile
        # them once the event loop can paint the window. This catches files that
        # were created, modified, renamed or deleted while DocSeek was closed.
        # Roots added later in this session are intentionally not captured here;
        # their explicit first scan already performs the same reconciliation.
        startup_roots = [Path(root) for root in self._active_index_roots()]
        if startup_roots:
            QTimer.singleShot(
                0,
                lambda roots=startup_roots: self._reconcile_startup_roots(roots),
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        """Persist UI state, finish searches and release SQLite handles."""
        if self._defer_close_until_index_stops(event):
            return
        if hasattr(self, "results_layout_timer"):
            self.results_layout_timer.stop()
            self._save_results_layout()
        if hasattr(self, "history_record_timer"):
            self.history_record_timer.stop()
        self.search_timer.stop()
        self.search_generation += 1
        self.search_thread_pool.clear()
        finished = self.search_thread_pool.waitForDone(self.SEARCH_SHUTDOWN_TIMEOUT_MS)
        if finished:
            close_persistent_search_stores(self.chunk_store.db_path)
        super().closeEvent(event)

    def _install_search_state_controls(self) -> None:
        self.history_button = QToolButton()
        self.history_button.setText("历史 ▾")
        self.history_button.setMinimumHeight(38)
        self.history_button.setMinimumWidth(72)
        self.history_button.setToolTip("打开常用搜索和最近搜索")
        self.history_button.clicked.connect(self._show_search_history_menu)

        self.favorite_button = QToolButton()
        self.favorite_button.setMinimumHeight(38)
        self.favorite_button.setMinimumWidth(84)
        self.favorite_button.setToolTip("收藏当前关键词、筛选和排序，方便以后直接恢复")
        self.favorite_button.clicked.connect(self._toggle_saved_search)

        self.help_button = QToolButton()
        self.help_button.setText("帮助")
        self.help_button.setMinimumHeight(38)
        self.help_button.setMinimumWidth(58)
        self.help_button.setToolTip("查看搜索语法、结构定位和快捷键（F1）")
        self.help_button.clicked.connect(self._show_search_help)

        top_bar = self.workspace_bar_layout
        insert_at = top_bar.indexOf(self.choose_button)
        if insert_at < 0:
            insert_at = top_bar.count()
        top_bar.insertWidget(insert_at, self.history_button)
        top_bar.insertWidget(insert_at + 1, self.favorite_button)
        top_bar.insertWidget(top_bar.indexOf(self.cancel_button), self.help_button)

        help_action = QAction("搜索帮助", self)
        help_action.setShortcut("F1")
        help_action.triggered.connect(self._show_search_help)
        self.addAction(help_action)
        self.search_help_action = help_action

    def _install_results_layout(self) -> None:
        """Make result columns user-adjustable and restore the last layout."""
        header = self.results.horizontalHeader()
        header.setSectionsMovable(True)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(44)

        for section, width in enumerate(DEFAULT_COLUMN_WIDTHS):
            header.setSectionResizeMode(section, QHeaderView.Interactive)
            header.resizeSection(section, width)

        saved = decode_header_state(self.database._get_setting(RESULTS_HEADER_STATE_KEY))
        if saved is not None:
            header.restoreState(saved)
            # Saved states from Qt also include resize modes. Keep all columns
            # interactive so restored widths remain user-adjustable.
            for section in range(header.count()):
                header.setSectionResizeMode(section, QHeaderView.Interactive)

        # The filename column is the one invariant: a result table without it
        # is too easy to make unusable by accident.
        header.setSectionHidden(0, False)

        self.results_layout_timer = QTimer(self)
        self.results_layout_timer.setSingleShot(True)
        self.results_layout_timer.setInterval(self.RESULTS_LAYOUT_SAVE_DELAY_MS)
        self.results_layout_timer.timeout.connect(self._save_results_layout)
        header.sectionResized.connect(lambda *_args: self.results_layout_timer.start())
        header.sectionMoved.connect(lambda *_args: self.results_layout_timer.start())

        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_results_header_menu)

    def _install_first_run_panel(self) -> None:
        """Keep the first launch focused on one user action: choose a directory."""
        root_layout = self.centralWidget().layout()
        if root_layout is None or root_layout.count() <= 0:
            raise RuntimeError("DocSeek main layout is unavailable")

        last_item = root_layout.itemAt(root_layout.count() - 1)
        self.content_splitter = last_item.widget() if last_item is not None else None
        if self.content_splitter is None:
            raise RuntimeError("DocSeek result area is unavailable")

        self.first_run_panel = QWidget(self)
        self.first_run_panel.setObjectName("welcomeCanvas")
        welcome_layout = QVBoxLayout(self.first_run_panel)
        welcome_layout.setContentsMargins(48, 32, 48, 32)
        welcome_layout.addStretch(1)

        self.first_run_card = QFrame()
        self.first_run_card.setObjectName("welcomeCard")
        self.first_run_card.setMaximumWidth(720)
        card_layout = QVBoxLayout(self.first_run_card)
        card_layout.setContentsMargins(48, 38, 48, 36)
        card_layout.setSpacing(14)

        eyebrow = QLabel("DOCSEEK · 本地全文检索")
        eyebrow.setObjectName("eyebrowLabel")
        eyebrow.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(eyebrow)

        title = QLabel("开始使用 DocSeek")
        title.setAlignment(Qt.AlignCenter)
        title.setObjectName("welcomeTitle")
        card_layout.addWidget(title)

        description = QLabel(
            "选择你的办公资料目录，DocSeek 会在本机建立全文索引。\n"
            "文档内容不会上传；索引会分批写入，已写入的内容即可搜索。"
        )
        description.setAlignment(Qt.AlignCenter)
        description.setWordWrap(True)
        description.setObjectName("welcomeDescription")
        card_layout.addWidget(description)

        info_panel = QFrame()
        info_panel.setObjectName("welcomeInfo")
        info_layout = QHBoxLayout(info_panel)
        info_layout.setContentsMargins(16, 11, 16, 11)
        info_layout.setSpacing(18)
        for text in (
            "✓ 内容仅保存在本机",
            "✓ 自动监测文件变化",
            "✓ 支持常用办公文档",
        ):
            item = QLabel(text)
            item.setAlignment(Qt.AlignCenter)
            info_layout.addWidget(item, 1)
        card_layout.addWidget(info_panel)

        self.first_run_button = QPushButton("选择资料目录")
        self.first_run_button.setMinimumHeight(42)
        self.first_run_button.setMinimumWidth(180)
        self.first_run_button.setProperty("primary", True)
        self.first_run_button.clicked.connect(self._choose_directory)
        card_layout.addWidget(self.first_run_button)

        self.first_run_storage_button = QPushButton("先设置索引保存位置…")
        self.first_run_storage_button.setToolTip(
            "如果不希望索引占用系统盘，可在添加资料目录前选择其他本地磁盘"
        )
        self.first_run_storage_button.clicked.connect(
            self._choose_first_run_storage_location
        )
        self.first_run_storage_button.setProperty("quiet", True)
        self.first_run_storage_button.setMinimumHeight(36)
        card_layout.addWidget(self.first_run_storage_button)

        self.first_run_storage_status = QLabel("")
        self.first_run_storage_status.setAlignment(Qt.AlignCenter)
        self.first_run_storage_status.setWordWrap(True)
        self.first_run_storage_status.setObjectName("mutedLabel")
        card_layout.addWidget(self.first_run_storage_status)

        steps = QLabel("1. 选择目录   →   2. 建立索引   →   3. 输入关键词搜索并打开文件")
        steps.setAlignment(Qt.AlignCenter)
        steps.setObjectName("mutedLabel")
        card_layout.addWidget(steps)

        welcome_layout.addWidget(self.first_run_card, 0, Qt.AlignHCenter)
        welcome_layout.addStretch(1)

        root_layout.insertWidget(root_layout.count() - 1, self.first_run_panel, 1)

    def _refresh_first_run_state(self) -> None:
        has_roots = bool(self.database.get_index_roots())
        self.first_run_panel.setVisible(not has_roots)
        self.content_splitter.setVisible(has_roots)
        self.search_bar_panel.setVisible(has_roots)
        self.workspace_bar_panel.setVisible(has_roots)
        self.search_input.setEnabled(has_roots)

        for widget in (
            self.type_filter,
            self.sort_filter,
            self.search_button,
            self.history_button,
            self.favorite_button,
            self.help_button,
            self.refresh_button,
            self.settings_button,
        ):
            widget.setVisible(has_roots)

        if not has_roots:
            self.filter_chip_panel.setVisible(False)
            self.scope_label.setText("尚未选择资料目录")
            try:
                pending_storage = pending_index_storage_move()
            except StorageLocationError as exc:
                self.first_run_button.setEnabled(False)
                self.first_run_storage_status.setText(f"索引位置配置异常：{exc}")
            else:
                self.first_run_button.setEnabled(pending_storage is None)
                if pending_storage is None:
                    self.first_run_storage_status.setText(
                        f"当前索引保存位置：{Path(self.database.db_path).parent}"
                    )
                else:
                    self.first_run_storage_status.setText(
                        f"已安排迁移到：{pending_storage}\n"
                        "请关闭并重新启动 DocSeek，然后再选择资料目录。"
                    )
        else:
            self._refresh_scope()

    def _choose_first_run_storage_location(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择索引数据保存位置",
            str(Path(self.database.db_path).parent),
        )
        if not selected:
            return
        try:
            destination = stage_index_storage_move(selected)
        except StorageLocationError as exc:
            QMessageBox.warning(self, "无法更改索引位置", str(exc))
            self._refresh_first_run_state()
            return

        self._refresh_first_run_state()
        try:
            current_directory = Path(self.database.db_path).parent.resolve()
        except OSError:
            current_directory = Path(self.database.db_path).parent.absolute()
        if destination == current_directory:
            QMessageBox.information(
                self,
                "索引位置未变化",
                f"DocSeek 已使用该位置：\n{destination}",
            )
            return
        QMessageBox.information(
            self,
            "索引位置已安排",
            f"索引将在下次启动前迁移到：\n{destination}\n\n"
            "请关闭并重新启动 DocSeek，然后再选择资料目录。",
        )

    def _save_results_layout(self) -> None:
        state = encode_header_state(self.results.horizontalHeader().saveState())
        self.database._set_setting(RESULTS_HEADER_STATE_KEY, state)

    def _set_result_column_visible(self, section: int, visible: bool) -> None:
        if section == 0:
            visible = True
        self.results.horizontalHeader().setSectionHidden(section, not visible)
        self.results_layout_timer.stop()
        self._save_results_layout()

    def _show_results_header_menu(self, point) -> None:
        header = self.results.horizontalHeader()
        menu = QMenu(self)
        menu.addSection("显示列")
        for section, label in enumerate(self.results_model.HEADERS):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(section))
            if section == 0:
                action.setEnabled(False)
            else:
                action.toggled.connect(
                    lambda checked, current=section: self._set_result_column_visible(
                        current, checked
                    )
                )
        menu.addSeparator()
        menu.addAction("恢复默认列布局", self._reset_results_layout)
        menu.exec(header.mapToGlobal(point))

    def _reset_results_layout(self) -> None:
        header = self.results.horizontalHeader()
        self.results_layout_timer.stop()
        for section in range(header.count()):
            header.setSectionHidden(section, False)
            header.setSectionResizeMode(section, QHeaderView.Interactive)

        # Restore logical column order even after arbitrary user moves.
        for logical_index in range(header.count()):
            visual_index = header.visualIndex(logical_index)
            if visual_index != logical_index:
                header.moveSection(visual_index, logical_index)

        for section, width in enumerate(DEFAULT_COLUMN_WIDTHS):
            header.resizeSection(section, width)
        self._save_results_layout()
        self.statusBar().showMessage("已恢复默认列布局", 3000)

    def _root_state_store(self) -> IndexRootStateStore:
        # This helper is intentionally constructed on demand: app_base invokes
        # _refresh_scope/_restart_watcher during its own __init__, before this
        # subclass can safely attach additional instance attributes.
        return IndexRootStateStore(self.database)

    def _active_index_roots(self) -> list[str]:
        return self._root_state_store().active_roots()

    @staticmethod
    def _path_is_under_roots(path: Path, roots: list[str]) -> bool:
        try:
            candidate = path.resolve()
        except OSError:
            candidate = path.absolute()
        for root in roots:
            try:
                root_path = Path(root).resolve()
            except OSError:
                root_path = Path(root).absolute()
            if candidate == root_path:
                return True
            try:
                candidate.relative_to(root_path)
                return True
            except ValueError:
                continue
        return False

    def _reconcile_startup_roots(self, roots: list[Path]) -> None:
        """Catch up changes made while DocSeek was closed without blocking paint."""
        if self.current_worker is not None:
            # A user-initiated index task already provides stronger freshness
            # than launching a duplicate startup reconciliation behind it.
            return

        active_roots = set(self._active_index_roots())
        candidates: list[Path] = []
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                resolved = root.absolute()
            if str(resolved) not in active_roots:
                continue
            if not resolved.exists() or not resolved.is_dir():
                continue
            candidates.append(resolved)

        if not candidates:
            return

        self._start_index(candidates, automatic=True)
        self.statusBar().showMessage(
            "正在检查关闭期间的文件变化；已完成的索引内容可继续搜索"
        )

    def _choose_directory(self) -> None:
        initial = self.database.get_index_root() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择需要索引的目录", initial)
        if selected:
            try:
                root = Path(selected).resolve()
            except OSError:
                root = Path(selected).absolute()

            # Persist the user's intent before starting any asynchronous work.
            # A cancelled scan, crash or power loss can therefore recover on the
            # next launch through startup reconciliation instead of forgetting
            # the directory after partially writing its index.
            self.database.add_index_root(str(root))
            self._root_state_store().set_paused(root, False)
            self._refresh_scope()
            self._restart_watcher()
            self._refresh_status()
            self._refresh_first_run_state()
            self._start_index([root])

    def _open_index_settings(self) -> None:
        if self.current_worker is not None:
            self.statusBar().showMessage("请等待当前索引任务完成后再修改设置", 5000)
            return

        dialog = PausableIndexSettingsDialog(self.database, self)
        if not dialog.exec():
            return

        # A full active-root reconciliation below subsumes any watcher events
        # that arrived just before the settings change. Clearing the queue also
        # guarantees a newly paused root cannot be updated by stale events.
        self.pending_watch_paths.clear()
        self.watch_full_rescan_pending = False
        self._refresh_scope()
        self._restart_watcher()
        self._refresh_status()
        self._refresh_first_run_state()

        roots = self.database.get_index_roots()
        active_roots = [Path(root) for root in self._active_index_roots()]
        if active_roots:
            self._start_index(active_roots, automatic=True)
        elif roots:
            self.statusBar().showMessage(
                "所有索引目录已暂停更新；现有索引仍可正常搜索",
                6000,
            )
        else:
            self.results_model.clear()

    def _refresh_all_roots(self) -> None:
        roots = self.database.get_index_roots()
        active_roots = [Path(root) for root in self._active_index_roots()]
        if not roots:
            self.statusBar().showMessage("请先添加一个索引目录", 5000)
            return
        if not active_roots:
            self.statusBar().showMessage(
                "所有索引目录已暂停；可在“索引设置”中重新勾选后恢复更新",
                6000,
            )
            return
        self._start_index(active_roots)

    def _drain_watch_queue(self) -> None:
        if self.current_worker is not None or self._close_when_index_stops:
            return

        active_roots = self._active_index_roots()
        if self.watch_full_rescan_pending:
            self.watch_full_rescan_pending = False
            self.pending_watch_paths.clear()
            if active_roots:
                self._start_index([Path(root) for root in active_roots], automatic=True)
            return

        if self.pending_watch_paths:
            pending = [Path(path) for path in sorted(self.pending_watch_paths)]
            self.pending_watch_paths.clear()
            paths = [
                path
                for path in pending
                if self._path_is_under_roots(path, active_roots)
            ]
            if paths:
                self._start_path_update(paths)

    def _restart_watcher(self) -> None:
        self.watch_manager.start(
            self._active_index_roots(),
            self.database.get_excluded_paths(),
        )

    def _refresh_scope(self) -> None:
        roots = self.database.get_index_roots()
        if not roots:
            super()._refresh_scope()
            return

        active = self._active_index_roots()
        paused = set(roots) - set(active)
        excluded = self.database.get_excluded_paths()
        suffix = f" · 排除 {len(excluded)} 个目录" if excluded else ""

        if len(roots) == 1:
            if paused:
                self.scope_label.setText(
                    f"搜索范围：{roots[0]} · 已暂停更新 · 现有索引仍可搜索{suffix}"
                )
            else:
                self.scope_label.setText(f"搜索范围：{roots[0]} · 自动监测变化{suffix}")
        elif not active:
            self.scope_label.setText(
                f"搜索范围：{len(roots)} 个目录 · 全部暂停更新 · 现有索引仍可搜索{suffix}"
            )
        elif paused:
            self.scope_label.setText(
                f"搜索范围：{len(roots)} 个目录 · 自动监测 {len(active)} 个 · "
                f"暂停 {len(paused)} 个{suffix}"
            )
        else:
            self.scope_label.setText(
                f"搜索范围：{len(roots)} 个目录 · 自动监测变化{suffix} · 最近添加：{roots[-1]}"
            )

        tooltip = ["索引目录："]
        tooltip.extend(
            f"[暂停] {root}" if root in paused else f"[监测] {root}"
            for root in roots
        )
        if excluded:
            tooltip.extend(["", "排除目录：", *excluded])
        self.scope_label.setToolTip("\n".join(tooltip))

    def _show_search_help(self, *_args) -> None:
        SearchHelpDialog(self).exec()

    def _current_search_state(self) -> SearchState:
        return SearchState(
            query=self.search_input.text(),
            extension=self.type_filter.currentData(),
            sort_mode=self.sort_filter.currentData() or SORT_RELEVANCE,
        ).normalized()

    def _refresh_saved_button(self, *_args) -> None:
        if not self._history_ui_ready:
            return
        state = self._current_search_state()
        if not state.meaningful:
            self.favorite_button.setEnabled(False)
            self.favorite_button.setText("☆ 收藏")
            return
        self.favorite_button.setEnabled(True)
        if self.search_state_store.is_saved(state):
            self.favorite_button.setText("★ 已收藏")
            self.favorite_button.setToolTip("当前搜索已收藏；点击取消收藏")
        else:
            self.favorite_button.setText("☆ 收藏")
            self.favorite_button.setToolTip("收藏当前关键词、筛选和排序，方便以后直接恢复")

    def _toggle_saved_search(self, *_args) -> None:
        state = self._current_search_state()
        if not state.meaningful:
            self.statusBar().showMessage("请先输入关键词或选择一个文件类型", 4000)
            return
        was_saved = self.search_state_store.is_saved(state)
        saved_now = self.search_state_store.toggle_saved(state)
        self._refresh_saved_button()
        if saved_now and not was_saved:
            self.statusBar().showMessage("已收藏当前搜索", 3000)
        else:
            self.statusBar().showMessage("已取消收藏", 3000)

    def _show_search_history_menu(self, *_args) -> None:
        menu = QMenu(self)
        saved = self.search_state_store.saved()
        history = self.search_state_store.history()

        if saved:
            menu.addSection("常用搜索")
            for state in saved[:10]:
                action = menu.addAction(f"★ {search_state_label(state)}")
                action.triggered.connect(
                    lambda _checked=False, selected=state: self._apply_search_state(selected)
                )

        if history:
            if saved:
                menu.addSeparator()
            menu.addSection("最近搜索")
            for state in history[:15]:
                action = menu.addAction(search_state_label(state))
                action.triggered.connect(
                    lambda _checked=False, selected=state: self._apply_search_state(selected)
                )
            menu.addSeparator()
            clear_action = menu.addAction("清除最近搜索")
            clear_action.triggered.connect(self._clear_recent_history)

        if not saved and not history:
            empty_action = menu.addAction("暂无搜索历史")
            empty_action.setEnabled(False)

        menu.exec(self.history_button.mapToGlobal(self.history_button.rect().bottomLeft()))

    def _clear_recent_history(self, *_args) -> None:
        self.search_state_store.clear_history()
        self.statusBar().showMessage("已清除最近搜索；收藏内容仍保留", 4000)

    def _apply_search_state(self, state: SearchState) -> None:
        state = state.normalized()
        self.search_timer.stop()
        self.history_record_timer.stop()
        self._history_candidate_generation = None

        search_blocked = self.search_input.blockSignals(True)
        type_blocked = self.type_filter.blockSignals(True)
        sort_blocked = self.sort_filter.blockSignals(True)
        try:
            self.search_input.setText(state.query)
            type_index = self.type_filter.findData(state.extension)
            self.type_filter.setCurrentIndex(type_index if type_index >= 0 else 0)
            sort_index = self.sort_filter.findData(state.sort_mode)
            self.sort_filter.setCurrentIndex(sort_index if sort_index >= 0 else 0)
        finally:
            self.search_input.blockSignals(search_blocked)
            self.type_filter.blockSignals(type_blocked)
            self.sort_filter.blockSignals(sort_blocked)

        self._refresh_filter_chips()
        self._refresh_saved_button()
        self._perform_search()
        # A deliberate history/saved-menu selection should become recent as
        # soon as its first page succeeds, just like pressing Enter.
        self._explicit_history_generation = self.search_generation
        self.search_input.setFocus()
        self.search_input.setCursorPosition(len(self.search_input.text()))

    def _perform_search(self, *_args) -> None:
        if self._history_ui_ready:
            self.history_record_timer.stop()
            self._history_candidate_generation = None
            explicit = self.sender() in (self.search_input, self.search_button)
            if explicit:
                # Avoid the pending 180 ms debounce issuing the same Enter
                # search a second time.
                self.search_timer.stop()
        else:
            explicit = False

        super()._perform_search()

        if self._history_ui_ready:
            self._explicit_history_generation = self.search_generation if explicit else None
            self._refresh_saved_button()

    def _search_finished(self, worker, response) -> None:
        super()._search_finished(worker, response)
        if not self._history_ui_ready:
            return

        request = response.request
        if request.generation != self.search_generation or request.offset != 0:
            return
        if response.page.total_count <= 0:
            if self._explicit_history_generation == request.generation:
                self._explicit_history_generation = None
            return

        if self._explicit_history_generation == request.generation:
            self.search_state_store.record_history(self._current_search_state())
            self._explicit_history_generation = None
            self._history_candidate_generation = None
        else:
            self._history_candidate_generation = request.generation
            self.history_record_timer.start()

    def _record_history_candidate(self) -> None:
        generation = self._history_candidate_generation
        self._history_candidate_generation = None
        if generation != self.search_generation:
            return
        if self.results_model.rowCount() <= 0:
            return
        self.search_state_store.record_history(self._current_search_state())


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("DocSeek")
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
