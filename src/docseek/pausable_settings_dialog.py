from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .diagnostics import write_diagnostic_report
from .file_exclusions import (
    FileExclusionStore,
    InvalidFileExclusionPattern,
    normalize_file_exclusion_patterns,
)
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
        self.resize(720, 700)
        self.root_state_store = IndexRootStateStore(database)
        self.file_exclusion_store = FileExclusionStore(database)

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

        patterns = self.file_exclusion_store.patterns()
        self._original_file_pattern_text = "\n".join(patterns)
        self.file_pattern_edit = QPlainTextEdit()
        self.file_pattern_edit.setPlainText(self._original_file_pattern_text)
        self.file_pattern_edit.setPlaceholderText("每行一条，例如：*.log   *.bak   测试_*   .tmp")
        self.file_pattern_edit.setMaximumHeight(92)
        self.file_pattern_edit.setToolTip(
            "只匹配文件名，大小写不敏感；.log 会自动按 *.log 处理。"
        )

        pattern_hint = QLabel(
            "每行一条文件名通配规则，例如 *.log、*.bak、测试_*。"
            "只匹配文件名，不支持路径或正则；保存后会校准活动目录并清理已有匹配索引。"
        )
        pattern_hint.setWordWrap(True)
        pattern_hint.setStyleSheet("color: palette(mid); font-size: 11px;")

        pattern_group = QGroupBox("排除文件规则")
        pattern_layout = QVBoxLayout()
        pattern_layout.addWidget(self.file_pattern_edit)
        pattern_layout.addWidget(pattern_hint)
        pattern_group.setLayout(pattern_layout)

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
        self.diagnostic_button = QPushButton("导出脱敏诊断…")
        self.diagnostic_button.setToolTip(
            "导出版本、运行环境和索引健康统计；不包含目录路径、文件名、正文、问题详情或搜索记录。"
        )
        self.diagnostic_button.clicked.connect(self._export_diagnostics)
        self.diagnostic_hint_label = QLabel(
            "诊断文件只包含聚合状态，不包含索引目录路径、问题文件路径、文件名、正文或搜索记录。"
        )
        self.diagnostic_hint_label.setWordWrap(True)
        self.diagnostic_hint_label.setStyleSheet("color: palette(mid); font-size: 11px;")

        health_group = QGroupBox("索引概况")
        health_layout = QVBoxLayout()
        health_layout.addWidget(self.health_summary_label)
        health_layout.addWidget(self.reconcile_time_label)
        health_layout.addWidget(self.health_hint_label)
        health_layout.addWidget(self.diagnostic_button)
        health_layout.addWidget(self.diagnostic_hint_label)
        health_group.setLayout(health_layout)

        # Base layout is: note, roots, excludes, performance, issues, buttons.
        # Keep filename exclusions beside directory exclusions, then status and
        # remediation together below them.
        self.layout().insertWidget(4, pattern_group)
        self.layout().insertWidget(5, health_group)
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

    def _file_patterns_from_ui(self) -> list[str]:
        return normalize_file_exclusion_patterns(
            self.file_pattern_edit.toPlainText().splitlines()
        )

    def _has_unsaved_changes(self) -> bool:
        return (
            set(self._items(self.root_list)) != self._original_roots
            or set(self._items(self.exclude_list)) != self._original_excluded
            or set(self._paused_roots_from_ui()) != self._original_paused
            or self.file_pattern_edit.toPlainText().strip()
            != self._original_file_pattern_text.strip()
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

    def _export_diagnostics(self) -> None:
        default_name = (
            "docseek-diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
        )
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出脱敏诊断",
            str(Path.home() / default_name),
            "JSON 文件 (*.json)",
        )
        if not filename:
            return
        if not filename.lower().endswith(".json"):
            filename += ".json"
        try:
            destination = write_diagnostic_report(self.database, filename)
        except Exception as exc:  # UI boundary: report failure instead of closing settings.
            QMessageBox.warning(self, "导出诊断失败", str(exc))
            return
        QMessageBox.information(
            self,
            "诊断已导出",
            "已保存脱敏诊断：\n"
            f"{destination}\n\n"
            "文件不包含目录路径、文件名、正文、问题详情或搜索记录。",
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
        try:
            file_patterns = self._file_patterns_from_ui()
        except InvalidFileExclusionPattern as exc:
            QMessageBox.warning(self, "排除规则无效", str(exc))
            return

        # Validate the file rules before mutating any persisted settings. This
        # keeps Save atomic from the user's point of view when a rule is invalid.
        self.file_exclusion_store.set_patterns(file_patterns)

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
