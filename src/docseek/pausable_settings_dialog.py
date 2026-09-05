from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel

from .index_issues_dialog import IndexIssuesDialog
from .index_retry import build_index_retry_plan, dispatch_index_retry
from .index_root_state import IndexRootStateStore
from .retryable_index_issues_dialog import RetryableIndexIssuesDialog
from .settings_dialog import IndexSettingsDialog


class PausableIndexSettingsDialog(IndexSettingsDialog):
    """Index settings with per-root pause/resume controls and safe retry."""

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
        self._original_paused = paused
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

    def _has_unsaved_changes(self) -> bool:
        return (
            set(self._items(self.root_list)) != self._original_roots
            or set(self._items(self.exclude_list)) != self._original_excluded
            or set(self._paused_roots_from_ui()) != self._original_paused
            or self.max_size.value() != self.database.get_max_file_size_mb()
        )

    def _show_index_issues(self) -> None:
        # Retry must use the settings the indexer will actually read. If this
        # dialog contains unsaved edits (for example a larger file-size limit),
        # asking the old configuration to retry would be misleading. Keep the
        # viewer available but require Save before exposing retry actions.
        if self._has_unsaved_changes():
            dialog = IndexIssuesDialog(self.issue_store, self)
            dialog.setWindowTitle("索引问题（保存设置后可重试）")
            hint = QLabel(
                "当前索引设置有未保存修改。请先保存设置，让 DocSeek 按新配置校准索引；"
                "之后仍存在的问题可再使用一键重试。"
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: palette(mid);")
            dialog.layout().insertWidget(1, hint)
            dialog.exec()
            self._refresh_issue_summary()
            return

        dialog = RetryableIndexIssuesDialog(self.issue_store, self)
        if not dialog.exec() or not dialog.retry_paths:
            self._refresh_issue_summary()
            return

        wanted = set(dialog.retry_paths)
        issues = [
            issue
            for issue in self.issue_store.list(limit=1000)
            if issue.path in wanted
        ]
        plan = build_index_retry_plan(
            issues,
            active_roots=self.root_state_store.active_roots(),
        )
        parent = self.parentWidget()

        # Close the settings modal before the main window launches workers.
        # There are no unsaved edits at this point, so nothing is discarded.
        self.reject()
        if parent is not None:
            QTimer.singleShot(
                0,
                lambda target=parent, retry_plan=plan: dispatch_index_retry(
                    target, retry_plan
                ),
            )

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
