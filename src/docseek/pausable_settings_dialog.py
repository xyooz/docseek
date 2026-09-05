from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout

from .index_health import (
    capture_index_health,
    format_index_health,
    format_reconcile_time,
)
from .index_issues_dialog import IndexIssuesDialog
from .index_retry import build_index_retry_plan, dispatch_index_retry
from .index_root_state import IndexRootStateStore
from .retryable_index_issues_dialog import RetryableIndexIssuesDialog
from .settings_dialog import IndexSettingsDialog


class PausableIndexSettingsDialog(IndexSettingsDialog):
    """Index settings with per-root pause/resume, health and safe retry."""

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

        self.health_summary_label = QLabel()
        self.health_summary_label.setWordWrap(True)
        self.health_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reconcile_time_label = QLabel()
        self.reconcile_time_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reconcile_time_label.setStyleSheet("color: palette(mid);")
        self.health_hint_label = QLabel(
            "占用包含 SQLite 主库及当前 WAL/SHM 文件；暂停目录的现有索引仍计入文件数和空间占用。"
        )
        self.health_hint_label.setWordWrap(True)
        self.health_hint_label.setStyleSheet("color: palette(mid); font-size: 11px;")

        health_group = QGroupBox("索引概况")
        health_layout = QVBoxLayout()
        health_layout.addWidget(self.health_summary_label)
        health_layout.addWidget(self.reconcile_time_label)
        health_layout.addWidget(self.health_hint_label)
        health_group.setLayout(health_layout)

        # Base layout is: note, roots, excludes, performance, issues, buttons.
        # Put health immediately before issue recovery so status and remediation
        # read as one coherent section.
        self.layout().insertWidget(4, health_group)
        self._refresh_health_summary()

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

    def _refresh_health_summary(self) -> None:
        if not hasattr(self, "health_summary_label"):
            return
        snapshot = capture_index_health(self.database)
        self.health_summary_label.setText(format_index_health(snapshot))
        if hasattr(self, "reconcile_time_label"):
            self.reconcile_time_label.setText(
                "最近完整校准："
                + format_reconcile_time(snapshot.last_successful_reconcile_at)
            )

    def _refresh_issue_summary(self) -> None:
        # Base __init__ calls this virtual method before the health widget exists,
        # so the health refresh is deliberately guarded above.
        super()._refresh_issue_summary()
        self._refresh_health_summary()

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
