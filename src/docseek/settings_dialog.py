from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
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
        self.root_list.addItems(database.get_index_roots())

        add_root_button = QPushButton("添加目录")
        remove_root_button = QPushButton("移除")
        add_root_button.clicked.connect(self._add_root)
        remove_root_button.clicked.connect(self._remove_selected_root)

        root_buttons = QHBoxLayout()
        root_buttons.addWidget(add_root_button)
        root_buttons.addWidget(remove_root_button)
        root_buttons.addStretch(1)

        roots_group = QGroupBox("索引目录")
        roots_layout = QVBoxLayout()
        roots_layout.addWidget(QLabel("DocSeek 只索引你主动添加的目录。"))
        roots_layout.addWidget(self.root_list)
        roots_layout.addLayout(root_buttons)
        roots_group.setLayout(roots_layout)

        self.exclude_list = QListWidget()
        self.exclude_list.addItems(database.get_excluded_paths())

        add_exclude_button = QPushButton("添加排除目录")
        remove_exclude_button = QPushButton("取消排除")
        add_exclude_button.clicked.connect(self._add_excluded)
        remove_exclude_button.clicked.connect(self._remove_selected_excluded)

        exclude_buttons = QHBoxLayout()
        exclude_buttons.addWidget(add_exclude_button)
        exclude_buttons.addWidget(remove_exclude_button)
        exclude_buttons.addStretch(1)

        exclude_group = QGroupBox("排除目录")
        exclude_layout = QVBoxLayout()
        exclude_layout.addWidget(QLabel("适合排除涉敏、临时、缓存或不需要检索的子目录。"))
        exclude_layout.addWidget(self.exclude_list)
        exclude_layout.addLayout(exclude_buttons)
        exclude_group.setLayout(exclude_layout)

        self.max_size = QSpinBox()
        self.max_size.setRange(1, 4096)
        self.max_size.setSuffix(" MB")
        self.max_size.setValue(database.get_max_file_size_mb())

        advanced_group = QGroupBox("性能保护")
        advanced_layout = QFormLayout()
        advanced_layout.addRow("单文件索引上限", self.max_size)
        advanced_group.setLayout(advanced_layout)

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

        self.button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(roots_group)
        layout.addWidget(exclude_group)
        layout.addWidget(advanced_group)
        layout.addWidget(storage_group)
        layout.addWidget(issues_group)
        layout.addWidget(self.button_box)
        self.setLayout(layout)

        self._original_roots = set(database.get_index_roots())
        self._original_excluded = set(database.get_excluded_paths())

    @staticmethod
    def _items(widget: QListWidget) -> list[str]:
        return [widget.item(i).text() for i in range(widget.count())]

    def _add_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择索引目录")
        if not selected:
            return
        resolved = str(Path(selected).resolve())
        if resolved not in self._items(self.root_list):
            self.root_list.addItem(resolved)

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
            self.exclude_list.addItem(resolved)

    def _remove_selected_excluded(self) -> None:
        row = self.exclude_list.currentRow()
        if row >= 0:
            self.exclude_list.takeItem(row)

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
        super().accept()
