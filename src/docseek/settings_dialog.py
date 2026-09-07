from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .chunk_store import ChunkStore
from .document_types import FORMAT_CAPABILITIES, KNOWN_DOCUMENT_EXTENSIONS
from .index_cleanup import remove_disabled_extensions
from .index_formats import (
    DEFAULT_ENABLED_INDEX_EXTENSIONS,
    INDEX_FORMAT_GROUPS,
    IndexFormatStore,
)
from .index_issues import IndexIssueStore
from .index_issues_dialog import IndexIssuesDialog
from .index_maintenance import purge_root_index
from .search_db import SearchDatabase
from .storage_location import (
    StorageLocationError,
    pending_index_storage_move,
    stage_index_storage_move,
)


class IndexSettingsDialog(QDialog):
    def __init__(self, database: SearchDatabase, parent=None) -> None:
        super().__init__(parent)
        self.database = database
        self.issue_store = IndexIssueStore(database.db_path)
        self.setWindowTitle("索引设置")
        self.resize(700, 640)

        self.root_list = QListWidget()
        for root in database.get_index_roots():
            self._add_path_item(self.root_list, root)

        self.add_root_button = QPushButton("添加目录")
        self.remove_root_button = QPushButton("移除")
        self.add_root_button.clicked.connect(self._add_root)
        self.remove_root_button.clicked.connect(self._remove_selected_root)

        root_buttons = QHBoxLayout()
        root_buttons.addWidget(self.add_root_button)
        root_buttons.addWidget(self.remove_root_button)
        root_buttons.addStretch(1)

        roots_group = QGroupBox("索引目录")
        roots_layout = QVBoxLayout()
        roots_layout.addWidget(QLabel("DocSeek 只索引你主动添加的目录。"))
        roots_layout.addWidget(self.root_list)
        roots_layout.addLayout(root_buttons)
        roots_group.setLayout(roots_layout)
        self.roots_group = roots_group

        self.exclude_list = QListWidget()
        for path in database.get_excluded_paths():
            self._add_path_item(self.exclude_list, path)

        self.add_exclude_button = QPushButton("添加排除目录")
        self.remove_exclude_button = QPushButton("取消排除")
        self.add_exclude_button.clicked.connect(self._add_excluded)
        self.remove_exclude_button.clicked.connect(self._remove_selected_excluded)

        exclude_buttons = QHBoxLayout()
        exclude_buttons.addWidget(self.add_exclude_button)
        exclude_buttons.addWidget(self.remove_exclude_button)
        exclude_buttons.addStretch(1)

        exclude_group = QGroupBox("排除目录")
        exclude_layout = QVBoxLayout()
        exclude_layout.addWidget(QLabel("适合排除涉敏、临时、缓存或不需要检索的子目录。"))
        exclude_layout.addWidget(self.exclude_list)
        exclude_layout.addLayout(exclude_buttons)
        exclude_group.setLayout(exclude_layout)
        self.exclude_group = exclude_group

        self.format_store = IndexFormatStore(database)
        self._original_enabled_extensions = (
            DEFAULT_ENABLED_INDEX_EXTENSIONS
            if not database.get_index_roots()
            and not self.format_store.has_explicit_setting()
            else self.format_store.enabled_extensions()
        )
        self.format_checkboxes: dict[str, QCheckBox] = {}
        format_grid = QGridLayout()
        format_grid.setColumnStretch(0, 1)
        format_grid.setColumnStretch(1, 1)
        row = 0
        for group in INDEX_FORMAT_GROUPS:
            heading = QLabel(f"<b>{group.label}</b>")
            format_grid.addWidget(heading, row, 0, 1, 2)
            row += 1
            for index, extension in enumerate(group.extensions):
                capability = FORMAT_CAPABILITIES[extension]
                checkbox = QCheckBox(
                    f"{extension.lstrip('.').upper()} · {capability.label}"
                )
                checkbox.setChecked(extension in self._original_enabled_extensions)
                checkbox.setToolTip(f"索引 *{extension} 文件")
                self.format_checkboxes[extension] = checkbox
                format_grid.addWidget(checkbox, row + index // 2, index % 2)
            row += (len(group.extensions) + 1) // 2

        format_container = QWidget()
        format_container.setLayout(format_grid)
        format_scroll = QScrollArea()
        format_scroll.setWidgetResizable(True)
        format_scroll.setWidget(format_container)
        format_scroll.setMaximumHeight(280)

        self.office_formats_button = QPushButton("仅 Office / WPS")
        self.all_formats_button = QPushButton("全选")
        self.clear_formats_button = QPushButton("清空")
        self.office_formats_button.clicked.connect(
            lambda: self._set_format_preset(DEFAULT_ENABLED_INDEX_EXTENSIONS)
        )
        self.all_formats_button.clicked.connect(
            lambda: self._set_format_preset(KNOWN_DOCUMENT_EXTENSIONS)
        )
        self.clear_formats_button.clicked.connect(lambda: self._set_format_preset(set()))

        format_buttons = QHBoxLayout()
        format_buttons.addWidget(self.office_formats_button)
        format_buttons.addWidget(self.all_formats_button)
        format_buttons.addWidget(self.clear_formats_button)
        format_buttons.addStretch(1)

        format_hint = QLabel(
            "只勾选需要全文检索的格式。新建索引默认仅启用 Office / WPS；"
            "XML 等兼容格式默认关闭。取消格式并保存后，会删除其本地索引，绝不删除源文件。"
        )
        format_hint.setWordWrap(True)
        format_hint.setStyleSheet("color: palette(mid); font-size: 11px;")

        format_group = QGroupBox("索引文件格式")
        format_layout = QVBoxLayout()
        format_layout.addLayout(format_buttons)
        format_layout.addWidget(format_scroll)
        format_layout.addWidget(format_hint)
        format_group.setLayout(format_layout)
        self.format_group = format_group

        self.max_size = QSpinBox()
        self.max_size.setRange(1, 4096)
        self.max_size.setSuffix(" MB")
        self.max_size.setValue(database.get_max_file_size_mb())

        advanced_group = QGroupBox("性能保护")
        advanced_layout = QFormLayout()
        advanced_layout.addRow("单文件索引上限", self.max_size)
        advanced_group.setLayout(advanced_layout)
        self.advanced_group = advanced_group

        self.storage_location_label = QLabel()
        self.storage_location_label.setWordWrap(True)
        self.storage_location_label.setTextInteractionFlags(
            self.storage_location_label.textInteractionFlags()
        )
        self.storage_pending_label = QLabel()
        self.storage_pending_label.setWordWrap(True)
        self.storage_pending_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        self.storage_move_button = QPushButton("更改位置…")
        self.storage_move_button.clicked.connect(self._change_storage_location)

        storage_buttons = QHBoxLayout()
        storage_buttons.addWidget(self.storage_move_button)
        storage_buttons.addStretch(1)

        storage_hint = QLabel(
            "索引数据包含提取后的文档正文。可迁移到本机其他磁盘；为保证 SQLite 可靠性，"
            "建议使用本地固定磁盘，不要放在网络共享或同步目录。迁移会在下次启动前完成。"
        )
        storage_hint.setWordWrap(True)
        storage_hint.setStyleSheet("color: palette(mid); font-size: 11px;")

        storage_group = QGroupBox("索引数据位置")
        storage_layout = QVBoxLayout()
        storage_layout.addWidget(self.storage_location_label)
        storage_layout.addWidget(self.storage_pending_label)
        storage_layout.addLayout(storage_buttons)
        storage_layout.addWidget(storage_hint)
        storage_group.setLayout(storage_layout)
        self.storage_group = storage_group
        self._refresh_storage_location()

        self.issue_summary = QLabel()
        self.issue_button = QPushButton()
        self.issue_button.clicked.connect(self._show_index_issues)
        self._refresh_issue_summary()

        issues_group = QGroupBox("索引状态")
        issues_layout = QHBoxLayout()
        issues_layout.addWidget(self.issue_summary, 1)
        issues_layout.addWidget(self.issue_button)
        issues_group.setLayout(issues_layout)
        self.issues_group = issues_group

        self.button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(roots_group)
        layout.addWidget(exclude_group)
        layout.addWidget(format_group)
        layout.addWidget(advanced_group)
        layout.addWidget(storage_group)
        layout.addWidget(issues_group)
        layout.addWidget(self.button_box)
        self.setLayout(layout)

        self._original_roots = set(database.get_index_roots())
        self._original_excluded = set(database.get_excluded_paths())

    @staticmethod
    def _items(widget: QListWidget) -> list[str]:
        return [IndexSettingsDialog._item_path(widget.item(i)) for i in range(widget.count())]

    @staticmethod
    def _item_path(item: QListWidgetItem) -> str:
        stored = item.data(Qt.ItemDataRole.UserRole)
        return str(stored) if stored else item.text()

    @staticmethod
    def _add_path_item(widget: QListWidget, path: str) -> QListWidgetItem:
        item = QListWidgetItem(path)
        item.setData(Qt.ItemDataRole.UserRole, path)
        widget.addItem(item)
        return item

    def _add_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择索引目录")
        if not selected:
            return
        resolved = str(Path(selected).resolve())
        if resolved not in self._items(self.root_list):
            self._add_path_item(self.root_list, resolved)

    def _remove_selected_root(self) -> None:
        row = self.root_list.currentRow()
        if row >= 0:
            self.root_list.takeItem(row)

    def _add_excluded(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择需要排除的目录")
        if not selected:
            return
        resolved = str(Path(selected).resolve())
        if resolved not in self._items(self.exclude_list):
            self._add_path_item(self.exclude_list, resolved)

    def _remove_selected_excluded(self) -> None:
        row = self.exclude_list.currentRow()
        if row >= 0:
            self.exclude_list.takeItem(row)

    def _set_format_preset(self, extensions: set[str] | frozenset[str]) -> None:
        enabled = set(extensions)
        for extension, checkbox in self.format_checkboxes.items():
            checkbox.setChecked(extension in enabled)

    def _enabled_extensions_from_ui(self) -> frozenset[str]:
        return frozenset(
            extension
            for extension, checkbox in self.format_checkboxes.items()
            if checkbox.isChecked()
        )

    def _validate_enabled_extensions(self) -> bool:
        if self._enabled_extensions_from_ui():
            return True
        QMessageBox.warning(
            self,
            "未选择索引格式",
            "请至少勾选一种需要建立索引和搜索的文件格式。",
        )
        return False

    def _refresh_storage_location(self) -> None:
        current = Path(self.database.db_path).expanduser()
        try:
            current = current.resolve()
        except OSError:
            current = current.absolute()
        self.storage_location_label.setText(f"当前：{current.parent}")

        try:
            pending = pending_index_storage_move()
        except StorageLocationError as exc:
            self.storage_pending_label.setText(f"待迁移配置异常：{exc}")
            return

        if pending is None:
            self.storage_pending_label.setText("当前没有待执行的迁移。")
        else:
            self.storage_pending_label.setText(
                f"下次启动前将迁移到：{pending}"
            )

    def _change_storage_location(self) -> None:
        current_dir = Path(self.database.db_path).expanduser().parent
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择索引数据保存位置",
            str(current_dir),
        )
        if not selected:
            return

        answer = QMessageBox.question(
            self,
            "更改索引数据位置",
            "DocSeek 不会在当前运行中直接搬动正在使用的 SQLite 数据库。\n\n"
            "所选位置会先进行可写检查；当前索引将在下次启动、数据库打开前安全迁移并校验。\n"
            "迁移期间不会修改任何源文档。\n\n"
            f"新位置：{selected}\n\n"
            "是否安排迁移？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            destination = stage_index_storage_move(selected)
        except StorageLocationError as exc:
            QMessageBox.warning(self, "无法更改索引位置", str(exc))
            self._refresh_storage_location()
            return

        self._refresh_storage_location()
        QMessageBox.information(
            self,
            "索引迁移已安排",
            "当前索引仍在原位置正常使用。\n\n"
            f"请正常关闭并重新启动 DocSeek；下次启动前会迁移到：\n{destination}\n\n"
            "迁移成功并通过校验后，DocSeek 才会切换到新位置。",
        )

    def _refresh_issue_summary(self) -> None:
        count = self.issue_store.count()
        if count:
            self.issue_summary.setText(f"有 {count} 个文件或目录未正常建立索引。")
            self.issue_button.setText(f"查看问题 ({count})")
        else:
            self.issue_summary.setText("当前没有发现索引问题。")
            self.issue_button.setText("查看问题")
        self.issue_button.setEnabled(count > 0)

    def _show_index_issues(self) -> None:
        dialog = IndexIssuesDialog(self.issue_store, self)
        dialog.exec()
        self._refresh_issue_summary()

    def accept(self) -> None:
        if not self._validate_enabled_extensions():
            return
        roots = set(self._items(self.root_list))
        excluded = set(self._items(self.exclude_list))

        for root in sorted(self._original_roots - roots):
            # Purge through the production chunk index rather than the legacy
            # file-level FTS tables. This also removes extraction state and
            # stale issue records for the removed root, but never source files.
            purge_root_index(self.database.db_path, root)
            self.database.remove_index_root(root)
        for root in sorted(roots - self._original_roots):
            self.database.add_index_root(root)

        for path in sorted(self._original_excluded - excluded):
            self.database.remove_excluded_path(path)
        for path in sorted(excluded - self._original_excluded):
            self.database.add_excluded_path(path)

        self.database.set_max_file_size_mb(self.max_size.value())
        enabled_extensions = self._enabled_extensions_from_ui()
        if enabled_extensions != self._original_enabled_extensions:
            self.format_store.set_enabled_extensions(enabled_extensions)
            remove_disabled_extensions(
                ChunkStore(self.database.db_path),
                enabled_extensions,
            )
        super().accept()
