from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from .index_root_state import IndexRootStateStore
from .settings_dialog import IndexSettingsDialog


class PausableIndexSettingsDialog(IndexSettingsDialog):
    """Index settings with per-root pause/resume controls."""

    def __init__(self, database, parent=None) -> None:
        super().__init__(database, parent)
        self.root_state_store = IndexRootStateStore(database)

        note = QLabel(
            "勾选 = 正常监测和刷新；取消勾选 = 暂停更新，但保留现有索引和搜索结果。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        self.layout().insertWidget(0, note)

        paused = set(self.root_state_store.paused_roots())
        for row in range(self.root_list.count()):
            item = self.root_list.item(row)
            self._make_checkable(item, checked=item.text() not in paused)

        self.root_list.setToolTip(
            "取消勾选目录可暂停 watcher 和手动刷新；重新勾选并保存后会自动进行增量校准。"
        )

    @staticmethod
    def _make_checkable(item, *, checked: bool) -> None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )

    def _add_root(self) -> None:
        previous_count = self.root_list.count()
        super()._add_root()
        for row in range(previous_count, self.root_list.count()):
            self._make_checkable(self.root_list.item(row), checked=True)

    def _paused_roots_from_ui(self) -> list[str]:
        return [
            self.root_list.item(row).text()
            for row in range(self.root_list.count())
            if self.root_list.item(row).checkState() == Qt.CheckState.Unchecked
        ]

    def accept(self) -> None:
        # Write the complete intended root-state set before the base dialog
        # applies root additions/removals. paused_roots() always filters against
        # the roots that actually exist, so a failed root mutation cannot make
        # an unrelated directory appear paused.
        roots = self._items(self.root_list)
        self.root_state_store.replace_paused(
            self._paused_roots_from_ui(),
            known_roots=roots,
        )
        super().accept()
