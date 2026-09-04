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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .search_db import SearchDatabase


class IndexSettingsDialog(QDialog):
    def __init__(self, database: SearchDatabase, parent=None) -> None:
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("索引设置")
        self.resize(680, 520)

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

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(roots_group)
        layout.addWidget(exclude_group)
        layout.addWidget(advanced_group)
        layout.addWidget(buttons)
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

    def accept(self) -> None:
        roots = set(self._items(self.root_list))
        excluded = set(self._items(self.exclude_list))

        for root in sorted(self._original_roots - roots):
            self.database.remove_index_root(root)
            self.database.purge_root(root)
        for root in sorted(roots - self._original_roots):
            self.database.add_index_root(root)

        for path in sorted(self._original_excluded - excluded):
            self.database.remove_excluded_path(path)
        for path in sorted(excluded - self._original_excluded):
            self.database.add_excluded_path(path)

        self.database.set_max_file_size_mb(self.max_size.value())
        super().accept()
